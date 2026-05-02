"""CLI for local Chipcoin v2 diagnostics and control."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import secrets
import sys
import time
from dataclasses import replace
from pathlib import Path
from urllib import error, request
from urllib.parse import urlparse

from ..config import DEFAULT_NETWORK, NETWORK_CONFIGS, get_network_config, resolve_data_path
from ..consensus.epoch_settlement import RewardAttestation
from ..consensus.models import Transaction
from ..consensus.pow import verify_proof_of_work
from ..consensus.serialization import serialize_transaction
from ..crypto.addresses import is_valid_address
from ..crypto.keys import parse_private_key_hex, serialize_private_key_hex, serialize_public_key_hex
from ..miner.config import MinerWorkerConfig
from ..miner.worker import MinerWorker
from ..node.messages import MessageEnvelope, TransactionMessage
from ..node.p2p.protocol import LocalPeerIdentity, PeerProtocol
from ..node.service import NodeService
from ..node.runtime import NodeRuntime, OutboundPeer, load_reward_node_automation_config_from_env
from ..node.snapshots import (
    ed25519_public_key_hex_from_private_key,
    parse_ed25519_private_key_hex,
    parse_ed25519_public_key_hex,
    read_snapshot_payload,
    sign_snapshot_payload,
    write_snapshot_file,
)
from ..node.sync import SyncManager
from ..wallet.models import WalletKey
from ..wallet.signer import TransactionSigner, generate_wallet_key, wallet_key_from_private_key
from .presenters import format_amount_chc, format_tip, format_transaction_lookup


def main(argv: list[str] | None = None) -> int:
    """Run the Chipcoin CLI."""

    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
        configure_runtime = args.command in {"run", "mine"}
        if configure_runtime:
            from ..utils.logging import configure_logging

            configure_logging(args.log_level)
        data_path = resolve_data_path(args.data, args.network)
        service = None if args.command in {"wallet-generate", "wallet-import", "wallet-address", "mine", "submit-raw-tx", "snapshot-sign"} else NodeService.open_sqlite(data_path, network=args.network)

        if service is not None and getattr(args, "snapshot_file", None) and args.command in {"run", "start"}:
            if getattr(args, "snapshot_reset", False) or service.chain_tip() is None:
                snapshot_metadata = service.import_snapshot_file(
                    Path(args.snapshot_file),
                    reset_existing=getattr(args, "snapshot_reset", False),
                    trust_mode=getattr(args, "snapshot_trust_mode", "off"),
                    trusted_keys=_load_snapshot_trusted_keys(
                        getattr(args, "snapshot_trusted_key", []),
                        getattr(args, "snapshot_trusted_keys_file", []),
                    ),
                )
                _emit_snapshot_warnings(snapshot_metadata, trust_mode=getattr(args, "snapshot_trust_mode", "off"))

        if args.command == "start":
            assert service is not None
            service.start()
            _print_json({"started": True, "status": service.status()})
            return 0

        if args.command == "operator-check":
            assert service is not None
            payload = service.operator_check(reward_node_id=args.reward_node_id)
            _enrich_operator_check_payload(
                payload,
                node_url=args.node_url,
                snapshot_manifest_urls=_operator_snapshot_manifest_urls(args.snapshot_manifest_url),
                network=args.network,
                timeout_seconds=args.request_timeout_seconds,
                miner_payout_address=args.miner_payout_address,
            )
            if args.json:
                _print_json(payload)
            else:
                _print_operator_check(payload)
            return 1 if payload["status"] == "fail" else 0

        if args.command == "run":
            assert service is not None
            asyncio.run(_run_runtime(service, args))
            return 0

        if args.command == "mine":
            payload = _run_miner_worker(args)
            _print_json(payload)
            return 0

        if args.command == "status":
            assert service is not None
            _print_json(service.status())
            return 0

        if args.command == "tip":
            assert service is not None
            _print_json(service.tip_diagnostics())
            return 0

        if args.command == "mine-local-block":
            assert service is not None
            mined_block = _mine_local_candidate_block(service, args.payout_address)
            _print_json(
                {
                    "accepted": True,
                    "block_hash": mined_block.block_hash(),
                    "height": service.chain_tip().height if service.chain_tip() is not None else None,
                    "tx_count": len(mined_block.transactions),
                }
            )
            return 0

        if args.command == "block":
            assert service is not None
            _print_json(service.inspect_block(block_hash=args.hash, height=args.height))
            return 0

        if args.command == "tx":
            assert service is not None
            _print_json(format_transaction_lookup(service.find_transaction(args.txid)))
            return 0

        if args.command == "submit-raw-tx":
            _print_json(_submit_raw_transaction_via_http(args))
            return 0

        if args.command == "add-peer":
            assert service is not None
            peer = service.add_peer(args.host, args.port, source="manual")
            _print_json({"host": peer.host, "port": peer.port, "network": peer.network})
            return 0

        if args.command == "list-peers":
            assert service is not None
            _print_json(service.peer_diagnostics())
            return 0

        if args.command == "peer-detail":
            assert service is not None
            detail = service.peer_detail(args.node_id)
            if detail is None:
                raise ValueError(f"Unknown peer node_id: {args.node_id}")
            _print_json(detail)
            return 0

        if args.command == "peer-summary":
            assert service is not None
            _print_json(service.peer_summary())
            return 0

        if args.command == "peerbook-clean":
            assert service is not None
            _print_json(service.peerbook_clean(reset_penalties=args.reset_penalties, dry_run=args.dry_run))
            return 0

        if args.command == "mempool":
            assert service is not None
            _print_json(service.mempool_diagnostics())
            return 0

        if args.command == "utxos":
            assert service is not None
            _print_json(
                [
                    {
                        **utxo,
                        "amount_chc": format_amount_chc(int(utxo["amount_chipbits"])),
                    }
                    for utxo in service.utxo_diagnostics(args.address)
                ]
            )
            return 0

        if args.command == "balance":
            assert service is not None
            payload = service.balance_diagnostics(args.address)
            _print_json(
                {
                    **payload,
                    "confirmed_balance_chc": format_amount_chc(int(payload["confirmed_balance_chipbits"])),
                    "immature_balance_chc": format_amount_chc(int(payload["immature_balance_chipbits"])),
                    "spendable_balance_chc": format_amount_chc(int(payload["spendable_balance_chipbits"])),
                }
            )
            return 0

        if args.command == "node-registry":
            assert service is not None
            _print_json(service.node_registry_diagnostics())
            return 0

        if args.command == "next-winners":
            assert service is not None
            payload = service.next_winners_diagnostics()
            _print_json(
                {
                    **payload,
                    "miner_subsidy_chc": format_amount_chc(int(payload["miner_subsidy_chipbits"])),
                    "node_reward_chc": format_amount_chc(int(payload["node_reward_chipbits"])),
                    "rewarded_recipients": [
                        {
                            **recipient,
                            "reward_chc": format_amount_chc(int(recipient["reward_chipbits"])),
                        }
                        for recipient in payload["rewarded_recipients"]
                    ],
                }
            )
            return 0

        if args.command == "reward-history":
            assert service is not None
            _print_json(
                [
                    {
                        **entry,
                        "amount_chc": format_amount_chc(int(entry["amount_chipbits"])),
                    }
                    for entry in service.reward_history(args.address, limit=args.limit, descending=not args.ascending)
                ]
            )
            return 0

        if args.command == "address-history":
            assert service is not None
            _print_json(
                [
                    {
                        **entry,
                        "incoming_chc": format_amount_chc(int(entry["incoming_chipbits"])),
                        "outgoing_chc": format_amount_chc(int(entry["outgoing_chipbits"])),
                        "net_chc": format_amount_chc(int(entry["net_chipbits"])),
                    }
                    for entry in service.address_history(args.address, limit=args.limit, descending=not args.ascending)
                ]
            )
            return 0

        if args.command == "reward-summary":
            assert service is not None
            payload = service.reward_summary(args.address, start_height=args.start_height, end_height=args.end_height)
            _print_json(
                {
                    **payload,
                    "total_rewards_chc": format_amount_chc(int(payload["total_rewards_chipbits"])),
                    "total_miner_subsidy_chc": format_amount_chc(int(payload["total_miner_subsidy_chipbits"])),
                    "total_node_rewards_chc": format_amount_chc(int(payload["total_node_rewards_chipbits"])),
                    "total_fees_chc": format_amount_chc(int(payload["total_fees_chipbits"])),
                    "mature_rewards_chc": format_amount_chc(int(payload["mature_rewards_chipbits"])),
                    "immature_rewards_chc": format_amount_chc(int(payload["immature_rewards_chipbits"])),
                }
            )
            return 0

        if args.command == "node-income-summary":
            assert service is not None
            _print_json(
                [
                    {
                        **row,
                        "total_node_rewards_chc": format_amount_chc(int(row["total_node_rewards_chipbits"])),
                    }
                    for row in service.node_income_summary(node_id=args.node_id, address=args.address)
                ]
            )
            return 0

        if args.command == "mining-history":
            assert service is not None
            _print_json(
                [
                    {
                        **row,
                        "miner_subsidy_chc": format_amount_chc(int(row["miner_subsidy_chipbits"])),
                        "fees_chc": format_amount_chc(int(row["fees_chipbits"])),
                        "node_reward_chc": format_amount_chc(int(row["node_reward_chipbits"])),
                    }
                    for row in service.mining_history(args.address, limit=args.limit, descending=not args.ascending)
                ]
            )
            return 0

        if args.command == "economy-summary":
            assert service is not None
            payload = service.economy_summary()
            _print_json(
                {
                    **payload,
                    "next_block_miner_subsidy_chc": format_amount_chc(int(payload["next_block_miner_subsidy_chipbits"])),
                    "next_block_node_reward_chc": format_amount_chc(int(payload["next_block_node_reward_chipbits"])),
                    "scheduled_supply_chc": format_amount_chc(int(payload["scheduled_supply_chipbits"])),
                    "scheduled_miner_supply_chc": format_amount_chc(int(payload["scheduled_miner_supply_chipbits"])),
                    "scheduled_node_reward_supply_chc": format_amount_chc(int(payload["scheduled_node_reward_supply_chipbits"])),
                    "scheduled_remaining_supply_chc": format_amount_chc(int(payload["scheduled_remaining_supply_chipbits"])),
                    "materialized_supply_chc": format_amount_chc(int(payload["materialized_supply_chipbits"])),
                    "materialized_miner_supply_chc": format_amount_chc(int(payload["materialized_miner_supply_chipbits"])),
                    "materialized_node_reward_supply_chc": format_amount_chc(int(payload["materialized_node_reward_supply_chipbits"])),
                    "undistributed_node_reward_supply_chc": format_amount_chc(int(payload["undistributed_node_reward_supply_chipbits"])),
                    "minted_supply_chc": format_amount_chc(int(payload["minted_supply_chipbits"])),
                    "miner_minted_supply_chc": format_amount_chc(int(payload["miner_minted_supply_chipbits"])),
                    "node_minted_supply_chc": format_amount_chc(int(payload["node_minted_supply_chipbits"])),
                    "circulating_supply_chc": format_amount_chc(int(payload["circulating_supply_chipbits"])),
                    "immature_supply_chc": format_amount_chc(int(payload["immature_supply_chipbits"])),
                    "max_supply_chc": format_amount_chc(int(payload["max_supply_chipbits"])),
                    "remaining_supply_chc": format_amount_chc(int(payload["remaining_supply_chipbits"])),
                }
            )
            return 0

        if args.command == "supply":
            assert service is not None
            payload = service.supply_snapshot()
            _print_json(
                {
                    **payload,
                    "max_supply_chc": format_amount_chc(int(payload["max_supply_chipbits"])),
                    "scheduled_supply_chc": format_amount_chc(int(payload["scheduled_supply_chipbits"])),
                    "scheduled_miner_supply_chc": format_amount_chc(int(payload["scheduled_miner_supply_chipbits"])),
                    "scheduled_node_reward_supply_chc": format_amount_chc(int(payload["scheduled_node_reward_supply_chipbits"])),
                    "scheduled_remaining_supply_chc": format_amount_chc(int(payload["scheduled_remaining_supply_chipbits"])),
                    "materialized_supply_chc": format_amount_chc(int(payload["materialized_supply_chipbits"])),
                    "materialized_miner_supply_chc": format_amount_chc(int(payload["materialized_miner_supply_chipbits"])),
                    "materialized_node_reward_supply_chc": format_amount_chc(int(payload["materialized_node_reward_supply_chipbits"])),
                    "undistributed_node_reward_supply_chc": format_amount_chc(int(payload["undistributed_node_reward_supply_chipbits"])),
                    "minted_supply_chc": format_amount_chc(int(payload["minted_supply_chipbits"])),
                    "miner_minted_supply_chc": format_amount_chc(int(payload["miner_minted_supply_chipbits"])),
                    "node_minted_supply_chc": format_amount_chc(int(payload["node_minted_supply_chipbits"])),
                    "burned_supply_chc": format_amount_chc(int(payload["burned_supply_chipbits"])),
                    "immature_supply_chc": format_amount_chc(int(payload["immature_supply_chipbits"])),
                    "circulating_supply_chc": format_amount_chc(int(payload["circulating_supply_chipbits"])),
                    "remaining_supply_chc": format_amount_chc(int(payload["remaining_supply_chipbits"])),
                }
            )
            return 0

        if args.command == "top-miners":
            assert service is not None
            _print_json(
                [
                    {
                        **row,
                        "total_miner_subsidy_chc": format_amount_chc(int(row["total_miner_subsidy_chipbits"])),
                        "total_fees_chc": format_amount_chc(int(row["total_fees_chipbits"])),
                        "total_node_reward_chc": format_amount_chc(int(row["total_node_reward_chipbits"])),
                    }
                    for row in service.top_miners(limit=args.limit)
                ]
            )
            return 0

        if args.command == "top-nodes":
            assert service is not None
            _print_json(
                [
                    {
                        **row,
                        "total_node_rewards_chc": format_amount_chc(int(row["total_node_rewards_chipbits"])),
                    }
                    for row in service.top_nodes(limit=args.limit)
                ]
            )
            return 0

        if args.command == "top-recipients":
            assert service is not None
            _print_json(
                [
                    {
                        **row,
                        "total_rewards_chc": format_amount_chc(int(row["total_rewards_chipbits"])),
                        "total_miner_subsidy_chc": format_amount_chc(int(row["total_miner_subsidy_chipbits"])),
                        "total_node_rewards_chc": format_amount_chc(int(row["total_node_rewards_chipbits"])),
                        "total_fees_chc": format_amount_chc(int(row["total_fees_chipbits"])),
                    }
                    for row in service.top_recipients(limit=args.limit)
                ]
            )
            return 0

        if args.command == "supply-diagnostics":
            assert service is not None
            payload = service.supply_diagnostics()
            _print_json(
                {
                    **payload,
                    "next_block_miner_subsidy_chc": format_amount_chc(int(payload["next_block_miner_subsidy_chipbits"])),
                    "next_block_node_reward_chc": format_amount_chc(int(payload["next_block_node_reward_chipbits"])),
                    "scheduled_supply_chc": format_amount_chc(int(payload["scheduled_supply_chipbits"])),
                    "scheduled_miner_supply_chc": format_amount_chc(int(payload["scheduled_miner_supply_chipbits"])),
                    "scheduled_node_reward_supply_chc": format_amount_chc(int(payload["scheduled_node_reward_supply_chipbits"])),
                    "scheduled_remaining_supply_chc": format_amount_chc(int(payload["scheduled_remaining_supply_chipbits"])),
                    "materialized_supply_chc": format_amount_chc(int(payload["materialized_supply_chipbits"])),
                    "materialized_miner_supply_chc": format_amount_chc(int(payload["materialized_miner_supply_chipbits"])),
                    "materialized_node_reward_supply_chc": format_amount_chc(int(payload["materialized_node_reward_supply_chipbits"])),
                    "undistributed_node_reward_supply_chc": format_amount_chc(int(payload["undistributed_node_reward_supply_chipbits"])),
                    "minted_supply_chc": format_amount_chc(int(payload["minted_supply_chipbits"])),
                    "miner_minted_supply_chc": format_amount_chc(int(payload["miner_minted_supply_chipbits"])),
                    "node_minted_supply_chc": format_amount_chc(int(payload["node_minted_supply_chipbits"])),
                    "burned_supply_chc": format_amount_chc(int(payload["burned_supply_chipbits"])),
                    "circulating_supply_chc": format_amount_chc(int(payload["circulating_supply_chipbits"])),
                    "immature_supply_chc": format_amount_chc(int(payload["immature_supply_chipbits"])),
                    "max_supply_chc": format_amount_chc(int(payload["max_supply_chipbits"])),
                    "remaining_supply_chc": format_amount_chc(int(payload["remaining_supply_chipbits"])),
                    "confirmed_unspent_supply_chc": format_amount_chc(int(payload["confirmed_unspent_supply_chipbits"])),
                }
            )
            return 0

        if args.command == "difficulty":
            assert service is not None
            _print_json(service.difficulty_diagnostics())
            return 0

        if args.command == "retarget-info":
            assert service is not None
            _print_json(service.retarget_diagnostics())
            return 0

        if args.command == "chain-window":
            assert service is not None
            _print_json(service.chain_window(args.start, args.end))
            return 0

        if args.command == "sync":
            assert service is not None
            peer_service = NodeService.open_sqlite(resolve_data_path(args.peer_data, args.network), network=args.network)
            result = SyncManager(node=service).synchronize(peer_service)
            _print_json(
                {
                    "headers_received": result.headers_received,
                    "blocks_fetched": result.blocks_fetched,
                    "activated_tip": result.activated_tip,
                    "parent_unknown": result.parent_unknown,
                    "reorged": result.reorged,
                }
            )
            return 0

        if args.command == "snapshot-export":
            assert service is not None
            _print_json(
                service.export_snapshot_file(
                    Path(args.snapshot_file),
                    format_version=1 if args.snapshot_format == "v1" else 2,
                )
            )
            return 0

        if args.command == "snapshot-import":
            assert service is not None
            payload = service.import_snapshot_file(
                Path(args.snapshot_file),
                reset_existing=args.snapshot_reset,
                trust_mode=args.snapshot_trust_mode,
                trusted_keys=_load_snapshot_trusted_keys(args.snapshot_trusted_key, args.snapshot_trusted_keys_file),
            )
            _emit_snapshot_warnings(payload, trust_mode=args.snapshot_trust_mode)
            _print_json(payload)
            return 0

        if args.command == "snapshot-sign":
            payload = read_snapshot_payload(Path(args.snapshot_file))
            private_key = parse_ed25519_private_key_hex(args.private_key_hex)
            signed_payload = sign_snapshot_payload(payload, private_key=private_key)
            write_snapshot_file(Path(args.snapshot_file), signed_payload)
            signatures = signed_payload["metadata"].get("signatures", [])
            _print_json(
                {
                    "snapshot_file": str(Path(args.snapshot_file)),
                    "signer_public_key_hex": ed25519_public_key_hex_from_private_key(private_key),
                    "signature_count": len(signatures) if isinstance(signatures, list) else 0,
                }
            )
            return 0

        if args.command == "wallet-generate":
            wallet_key = generate_wallet_key()
            _save_wallet_key(Path(args.wallet_file), wallet_key)
            _print_json(_format_wallet_key(wallet_key))
            return 0

        if args.command == "wallet-import":
            wallet_key = wallet_key_from_private_key(parse_private_key_hex(args.private_key_hex))
            _save_wallet_key(Path(args.wallet_file), wallet_key)
            _print_json(_format_wallet_key(wallet_key))
            return 0

        if args.command == "wallet-address":
            wallet_key = _load_wallet_key(Path(args.wallet_file))
            _print_json(_format_wallet_key(wallet_key))
            return 0

        if args.command == "wallet-utxos":
            assert service is not None
            wallet_key = _load_wallet_key(Path(args.wallet_file))
            _print_json(
                [
                    {
                        **utxo,
                        "amount_chc": format_amount_chc(int(utxo["amount_chipbits"])),
                    }
                    for utxo in service.utxo_diagnostics(wallet_key.address)
                ]
            )
            return 0

        if args.command == "wallet-balance":
            assert service is not None
            wallet_key = _load_wallet_key(Path(args.wallet_file))
            payload = service.balance_diagnostics(wallet_key.address)
            _print_json(
                {
                    **payload,
                    "confirmed_balance_chc": format_amount_chc(int(payload["confirmed_balance_chipbits"])),
                    "immature_balance_chc": format_amount_chc(int(payload["immature_balance_chipbits"])),
                    "spendable_balance_chc": format_amount_chc(int(payload["spendable_balance_chipbits"])),
                }
            )
            return 0

        if args.command == "register-node":
            assert service is not None
            wallet_key = _load_wallet_key(Path(args.wallet_file))
            transaction = _build_register_node_transaction(service, wallet_key, args)
            submitted = _submit_special_transaction(service, transaction, args.connect)
            _print_json(
                {
                    "txid": transaction.txid(),
                    "node_id": args.node_id,
                    "payout_address": args.payout_address,
                    "owner_pubkey": serialize_public_key_hex(wallet_key.public_key),
                    "submitted": submitted,
                }
            )
            return 0

        if args.command == "renew-node":
            assert service is not None
            wallet_key = _load_wallet_key(Path(args.wallet_file))
            current_epoch = service.next_block_epoch()
            transaction = _build_renew_node_transaction(service, wallet_key, args, current_epoch=current_epoch)
            submitted = _submit_special_transaction(service, transaction, args.connect)
            _print_json(
                {
                    "txid": transaction.txid(),
                    "node_id": args.node_id,
                    "current_epoch": current_epoch,
                    "submitted": submitted,
                }
            )
            return 0

        if args.command == "register-reward-node":
            assert service is not None
            wallet_key = _load_wallet_key(Path(args.wallet_file))
            transaction = _build_register_reward_node_transaction(service, wallet_key, args)
            submitted = _submit_special_transaction(service, transaction, args.connect)
            _print_json(
                {
                    "txid": transaction.txid(),
                    "node_id": args.node_id,
                    "payout_address": args.payout_address,
                    "node_pubkey_hex": args.node_pubkey_hex,
                    "declared_host": args.declared_host,
                    "declared_port": args.declared_port,
                    "submitted": submitted,
                }
            )
            return 0

        if args.command == "renew-reward-node":
            assert service is not None
            wallet_key = _load_wallet_key(Path(args.wallet_file))
            current_epoch = service.next_block_epoch()
            transaction = _build_renew_reward_node_transaction(service, wallet_key, args, current_epoch=current_epoch)
            submitted = _submit_special_transaction(service, transaction, args.connect)
            _print_json(
                {
                    "txid": transaction.txid(),
                    "node_id": args.node_id,
                    "current_epoch": current_epoch,
                    "declared_host": args.declared_host,
                    "declared_port": args.declared_port,
                    "submitted": submitted,
                }
            )
            return 0

        if args.command == "reward-epoch-seed":
            assert service is not None
            _print_json(service.native_reward_epoch_seed_diagnostics(epoch_index=args.epoch_index))
            return 0

        if args.command == "reward-epoch-state":
            assert service is not None
            _print_json(service.native_reward_epoch_state(epoch_index=args.epoch_index, node_id=args.node_id))
            return 0

        if args.command == "reward-assignments":
            assert service is not None
            _print_json(service.native_reward_assignments(epoch_index=args.epoch_index, node_id=args.node_id))
            return 0

        if args.command == "reward-node-status":
            assert service is not None
            _print_json(service.reward_node_status(node_id=args.node_id, epoch_index=args.epoch_index))
            return 0

        if args.command == "reward-node-fees":
            assert service is not None
            _print_json(service.reward_node_fee_schedule())
            return 0

        if args.command == "reward-epoch-summary":
            assert service is not None
            _print_json(service.reward_epoch_summary(epoch_index=args.epoch_index))
            return 0

        if args.command == "reward-attestations":
            assert service is not None
            _print_json(service.native_reward_attestation_diagnostics(epoch_index=args.epoch_index))
            return 0

        if args.command == "reward-settlements":
            assert service is not None
            _print_json(service.native_reward_settlement_diagnostics(epoch_index=args.epoch_index))
            return 0

        if args.command == "reward-settlement-preview":
            assert service is not None
            _print_json(service.native_reward_settlement_preview(epoch_index=args.epoch_index))
            return 0

        if args.command == "reward-settlement-report":
            assert service is not None
            _print_json(service.native_reward_settlement_report(epoch_index=args.epoch_index))
            return 0

        if args.command == "submit-reward-attestation-bundle":
            assert service is not None
            wallet_key = _load_wallet_key(Path(args.wallet_file)) if args.wallet_file else None
            transaction = _build_reward_attestation_bundle_transaction(service, args, wallet_key)
            submitted = _submit_special_transaction(service, transaction, args.connect)
            _print_json({"txid": transaction.txid(), "submitted": submitted, "raw_hex": serialize_transaction(transaction).hex()})
            return 0

        if args.command == "submit-reward-settle-epoch":
            assert service is not None
            transaction = _build_reward_settle_epoch_transaction(args)
            submitted = _submit_special_transaction(service, transaction, args.connect)
            _print_json({"txid": transaction.txid(), "submitted": submitted, "raw_hex": serialize_transaction(transaction).hex()})
            return 0

        if args.command == "wallet-build":
            assert service is not None
            wallet_key = _load_wallet_key(Path(args.wallet_file))
            built = _build_wallet_transaction(service, wallet_key, args)
            _print_json(
                {
                    "txid": built.transaction.txid(),
                    "raw_hex": serialize_transaction(built.transaction).hex(),
                    "fee_chipbits": built.fee_chipbits,
                    "change_chipbits": built.change_chipbits,
                }
            )
            return 0

        if args.command == "wallet-send":
            assert service is not None
            wallet_key = _load_wallet_key(Path(args.wallet_file))
            built = _build_wallet_transaction(service, wallet_key, args)
            if args.connect:
                asyncio.run(_send_transaction_to_peer(built.transaction, _parse_peer(args.connect), network=service.network))
                mode = "p2p"
            else:
                service.receive_transaction(built.transaction)
                mode = "local"
            _print_json(
                {
                    "sent": True,
                    "mode": mode,
                    "txid": built.transaction.txid(),
                    "fee_chipbits": built.fee_chipbits,
                    "change_chipbits": built.change_chipbits,
                }
            )
            return 0
    except Exception as exc:
        _print_error(str(exc))
        return 1

    parser.error("unsupported command")
    return 2


def _build_parser() -> argparse.ArgumentParser:
    """Build the CLI parser."""

    parser = argparse.ArgumentParser(prog="chipcoin")
    parser.add_argument("--data", default="chipcoin.sqlite3", help="Path to the local SQLite data file.")
    parser.add_argument("--network", choices=sorted(NETWORK_CONFIGS), default=DEFAULT_NETWORK)
    parser.add_argument("--log-level", choices=("DEBUG", "INFO", "WARNING", "ERROR"), default="INFO")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("start")
    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("--listen-host", default="127.0.0.1")
    run_parser.add_argument("--listen-port", type=int, default=None)
    run_parser.add_argument("--http-host", default=None)
    run_parser.add_argument("--http-port", type=int, default=None)
    run_parser.add_argument("--snapshot-file", default=None, help="Optional local snapshot file for fast bootstrap.")
    run_parser.add_argument("--snapshot-reset", action="store_true", help="Replace existing local chain state when importing a snapshot.")
    run_parser.add_argument("--snapshot-trust-mode", choices=("off", "warn", "enforce"), default="off")
    run_parser.add_argument("--snapshot-trusted-key", action="append", default=[], help="Trusted Ed25519 signer public key hex for snapshot verification.")
    run_parser.add_argument("--snapshot-trusted-keys-file", action="append", default=[], help="Path to a text or JSON file containing trusted Ed25519 signer public keys.")
    run_parser.add_argument("--peer", action="append", default=[], help="Outbound peer in host:port form.")
    run_parser.add_argument("--peer-source", choices=("manual", "seed"), default="manual")
    run_parser.add_argument("--run-seconds", type=float, default=None, help="Stop automatically after N seconds.")
    run_parser.add_argument("--ping-interval-seconds", type=float, default=2.0)
    run_parser.add_argument("--read-timeout-seconds", type=float, default=15.0)
    run_parser.add_argument("--write-timeout-seconds", type=float, default=15.0)
    run_parser.add_argument("--handshake-timeout-seconds", type=float, default=5.0)
    run_parser.add_argument("--peer-discovery-enabled", type=_parse_bool, default=True)
    run_parser.add_argument("--peerbook-max-size", type=int, default=1024)
    run_parser.add_argument("--peer-addr-max-per-message", type=int, default=250)
    run_parser.add_argument("--peer-addr-relay-limit-per-interval", type=int, default=250)
    run_parser.add_argument("--peer-addr-relay-interval-seconds", type=int, default=30)
    run_parser.add_argument("--peer-stale-after-seconds", type=int, default=604800)
    run_parser.add_argument("--peer-retry-backoff-base-seconds", type=float, default=1.0)
    run_parser.add_argument("--peer-retry-backoff-max-seconds", type=float, default=30.0)
    run_parser.add_argument("--max-outbound-sessions", type=int, default=8)
    run_parser.add_argument("--max-inbound-sessions", type=int, default=32)
    run_parser.add_argument("--inbound-handshake-rate-limit-per-minute", type=int, default=12)
    run_parser.add_argument("--min-stable-session-seconds", type=float, default=30.0)
    run_parser.add_argument("--peer-discovery-startup-prefer-persisted", type=_parse_bool, default=True)
    run_parser.add_argument("--headers-sync-enabled", type=_parse_bool, default=True)
    run_parser.add_argument("--headers-max-per-message", type=int, default=2000)
    run_parser.add_argument("--block-download-window-size", type=int, default=128)
    run_parser.add_argument("--block-max-inflight-per-peer", type=int, default=16)
    run_parser.add_argument("--block-request-timeout-seconds", type=float, default=15.0)
    run_parser.add_argument("--headers-sync-parallel-peers", type=int, default=2)
    run_parser.add_argument("--headers-sync-start-height-gap-threshold", type=int, default=1)
    run_parser.add_argument("--misbehavior-warning-threshold", type=int, default=25)
    run_parser.add_argument("--misbehavior-disconnect-threshold", type=int, default=50)
    run_parser.add_argument("--misbehavior-ban-threshold", type=int, default=100)
    run_parser.add_argument("--misbehavior-ban-duration-seconds", type=int, default=1800)
    run_parser.add_argument("--misbehavior-decay-interval-seconds", type=int, default=300)
    run_parser.add_argument("--misbehavior-decay-step", type=int, default=5)
    mine_parser = subparsers.add_parser("mine")
    mine_parser.add_argument(
        "--node-url",
        action="append",
        default=[],
        help="Mining node HTTP endpoint. Repeat the flag to configure failover nodes.",
    )
    mine_parser.add_argument("--run-seconds", type=float, default=None, help="Stop automatically after N seconds.")
    mine_parser.add_argument("--miner-address", required=True, help="Mining reward payout address.")
    mine_parser.add_argument("--miner-id", default=None, help="Stable miner identifier used in template requests.")
    mine_parser.add_argument("--polling-interval-seconds", type=float, default=2.0)
    mine_parser.add_argument("--request-timeout-seconds", type=float, default=10.0)
    mine_parser.add_argument("--nonce-batch-size", type=int, default=250000)
    mine_parser.add_argument("--template-refresh-skew-seconds", type=int, default=1)
    mine_parser.add_argument(
        "--mining-min-interval-seconds",
        type=float,
        default=0.0,
        help="Optional local throttle between mined blocks. Does not change consensus.",
    )
    subparsers.add_parser("status")
    operator_check = subparsers.add_parser("operator-check")
    operator_check.add_argument("--data", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    operator_check.add_argument("--network", choices=sorted(NETWORK_CONFIGS), default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    operator_check.add_argument("--json", action="store_true", help="Emit stable machine-readable JSON.")
    operator_check.add_argument("--reward-node-id", help="Require a specific reward node to be registered and eligible.")
    operator_check.add_argument("--node-url", help="Optional HTTP node API URL for remote mining template checks.")
    operator_check.add_argument("--miner-payout-address", help="Optional payout address used for mining template checks.")
    operator_check.add_argument("--snapshot-manifest-url", action="append", default=[], help="Optional snapshot manifest URL to verify. Defaults to NODE_SNAPSHOT_MANIFEST_URLS when set.")
    operator_check.add_argument("--request-timeout-seconds", type=float, default=5.0)
    subparsers.add_parser("tip")
    mine_local_block = subparsers.add_parser("mine-local-block")
    mine_local_block.add_argument("--payout-address", required=True)

    block_parser = subparsers.add_parser("block")
    group = block_parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--height", type=int)
    group.add_argument("--hash")

    tx_parser = subparsers.add_parser("tx")
    tx_parser.add_argument("txid")

    submit_parser = subparsers.add_parser("submit-raw-tx")
    submit_parser.add_argument("--node-url", required=True, help="Node HTTP endpoint, for example http://127.0.0.1:8081")
    submit_parser.add_argument("raw_hex")

    add_peer_parser = subparsers.add_parser("add-peer")
    add_peer_parser.add_argument("host")
    add_peer_parser.add_argument("port", type=int)

    subparsers.add_parser("list-peers")
    peer_detail_parser = subparsers.add_parser("peer-detail")
    peer_detail_parser.add_argument("--node-id", required=True)
    subparsers.add_parser("peer-summary")
    peerbook_clean_parser = subparsers.add_parser("peerbook-clean")
    peerbook_clean_parser.add_argument("--reset-penalties", action="store_true")
    peerbook_clean_parser.add_argument("--dry-run", action="store_true")
    subparsers.add_parser("mempool")
    utxos_parser = subparsers.add_parser("utxos")
    utxos_parser.add_argument("--address", required=True)
    balance_parser = subparsers.add_parser("balance")
    balance_parser.add_argument("--address", required=True)
    subparsers.add_parser("node-registry")
    subparsers.add_parser("next-winners")
    reward_history_parser = subparsers.add_parser("reward-history")
    reward_history_parser.add_argument("--address", required=True)
    reward_history_parser.add_argument("--limit", type=int, default=50)
    reward_history_parser.add_argument("--ascending", action="store_true")
    reward_summary_parser = subparsers.add_parser("reward-summary")
    reward_summary_parser.add_argument("--address", required=True)
    reward_summary_parser.add_argument("--start-height", type=int)
    reward_summary_parser.add_argument("--end-height", type=int)
    node_income_summary_parser = subparsers.add_parser("node-income-summary")
    node_income_summary_parser.add_argument("--node-id")
    node_income_summary_parser.add_argument("--address")
    mining_history_parser = subparsers.add_parser("mining-history")
    mining_history_parser.add_argument("--address", required=True)
    mining_history_parser.add_argument("--limit", type=int, default=50)
    mining_history_parser.add_argument("--ascending", action="store_true")
    subparsers.add_parser("economy-summary")
    subparsers.add_parser("supply")
    top_miners_parser = subparsers.add_parser("top-miners")
    top_miners_parser.add_argument("--limit", type=int, default=10)
    top_nodes_parser = subparsers.add_parser("top-nodes")
    top_nodes_parser.add_argument("--limit", type=int, default=10)
    top_recipients_parser = subparsers.add_parser("top-recipients")
    top_recipients_parser.add_argument("--limit", type=int, default=10)
    subparsers.add_parser("supply-diagnostics")
    address_history_parser = subparsers.add_parser("address-history")
    address_history_parser.add_argument("--address", required=True)
    address_history_parser.add_argument("--limit", type=int, default=50)
    address_history_parser.add_argument("--ascending", action="store_true")
    subparsers.add_parser("difficulty")
    subparsers.add_parser("retarget-info")

    chain_window_parser = subparsers.add_parser("chain-window")
    chain_window_parser.add_argument("--start", type=int, required=True)
    chain_window_parser.add_argument("--end", type=int, required=True)

    sync_parser = subparsers.add_parser("sync")
    sync_parser.add_argument("--peer-data", required=True, help="Path to a peer SQLite data file.")

    snapshot_export_parser = subparsers.add_parser("snapshot-export")
    snapshot_export_parser.add_argument("--snapshot-file", required=True)
    snapshot_export_parser.add_argument("--snapshot-format", choices=("v1", "v2"), default="v2")

    snapshot_import_parser = subparsers.add_parser("snapshot-import")
    snapshot_import_parser.add_argument("--snapshot-file", required=True)
    snapshot_import_parser.add_argument("--snapshot-reset", action="store_true")
    snapshot_import_parser.add_argument("--snapshot-trust-mode", choices=("off", "warn", "enforce"), default="off")
    snapshot_import_parser.add_argument("--snapshot-trusted-key", action="append", default=[], help="Trusted Ed25519 signer public key hex for snapshot verification.")
    snapshot_import_parser.add_argument("--snapshot-trusted-keys-file", action="append", default=[], help="Path to a text or JSON file containing trusted Ed25519 signer public keys.")

    snapshot_sign_parser = subparsers.add_parser("snapshot-sign")
    snapshot_sign_parser.add_argument("--snapshot-file", required=True)
    snapshot_sign_parser.add_argument("--private-key-hex", required=True, help="Raw 32-byte Ed25519 private key hex.")

    wallet_generate = subparsers.add_parser("wallet-generate")
    wallet_generate.add_argument("--wallet-file", required=True)

    wallet_import = subparsers.add_parser("wallet-import")
    wallet_import.add_argument("--wallet-file", required=True)
    wallet_import.add_argument("--private-key-hex", required=True)

    wallet_address = subparsers.add_parser("wallet-address")
    wallet_address.add_argument("--wallet-file", required=True)
    wallet_utxos = subparsers.add_parser("wallet-utxos")
    wallet_utxos.add_argument("--wallet-file", required=True)
    wallet_balance = subparsers.add_parser("wallet-balance")
    wallet_balance.add_argument("--wallet-file", required=True)
    register_node = subparsers.add_parser("register-node")
    register_node.add_argument("--wallet-file", required=True)
    register_node.add_argument("--node-id", required=True)
    register_node.add_argument("--payout-address", required=True)
    register_node.add_argument("--connect")
    renew_node = subparsers.add_parser("renew-node")
    renew_node.add_argument("--wallet-file", required=True)
    renew_node.add_argument("--node-id", required=True)
    renew_node.add_argument("--connect")
    register_reward_node = subparsers.add_parser("register-reward-node")
    register_reward_node.add_argument("--wallet-file", required=True)
    register_reward_node.add_argument("--node-id", required=True)
    register_reward_node.add_argument("--payout-address", required=True)
    register_reward_node.add_argument("--node-pubkey-hex", required=True)
    register_reward_node.add_argument("--declared-host", required=True)
    register_reward_node.add_argument("--declared-port", required=True, type=int)
    register_reward_node.add_argument("--connect")
    renew_reward_node = subparsers.add_parser("renew-reward-node")
    renew_reward_node.add_argument("--wallet-file", required=True)
    renew_reward_node.add_argument("--node-id", required=True)
    renew_reward_node.add_argument("--declared-host", required=True)
    renew_reward_node.add_argument("--declared-port", required=True, type=int)
    renew_reward_node.add_argument("--connect")
    reward_epoch_seed = subparsers.add_parser("reward-epoch-seed")
    reward_epoch_seed.add_argument("--epoch-index", type=int)
    reward_epoch_state = subparsers.add_parser("reward-epoch-state")
    reward_epoch_state.add_argument("--epoch-index", type=int)
    reward_epoch_state.add_argument("--node-id")
    reward_assignments = subparsers.add_parser("reward-assignments")
    reward_assignments.add_argument("--epoch-index", type=int)
    reward_assignments.add_argument("--node-id")
    reward_node_status = subparsers.add_parser("reward-node-status")
    reward_node_status.add_argument("--node-id", required=True)
    reward_node_status.add_argument("--epoch-index", type=int)
    subparsers.add_parser("reward-node-fees")
    reward_epoch_summary = subparsers.add_parser("reward-epoch-summary")
    reward_epoch_summary.add_argument("--epoch-index", required=True, type=int)
    reward_attestations = subparsers.add_parser("reward-attestations")
    reward_attestations.add_argument("--epoch-index", type=int)
    reward_settlements = subparsers.add_parser("reward-settlements")
    reward_settlements.add_argument("--epoch-index", type=int)
    reward_settlement_preview = subparsers.add_parser("reward-settlement-preview")
    reward_settlement_preview.add_argument("--epoch-index", type=int)
    reward_settlement_report = subparsers.add_parser("reward-settlement-report")
    reward_settlement_report.add_argument("--epoch-index", type=int)
    submit_reward_attestation_bundle = subparsers.add_parser("submit-reward-attestation-bundle")
    submit_reward_attestation_bundle.add_argument("--bundle-file", required=True)
    submit_reward_attestation_bundle.add_argument("--wallet-file")
    submit_reward_attestation_bundle.add_argument("--connect")
    submit_reward_settlement = subparsers.add_parser("submit-reward-settle-epoch")
    submit_reward_settlement.add_argument("--settlement-file", required=True)
    submit_reward_settlement.add_argument("--connect")

    wallet_build = subparsers.add_parser("wallet-build")
    wallet_build.add_argument("--wallet-file", required=True)
    wallet_build.add_argument("--to", required=True)
    wallet_build.add_argument("--amount", type=int, required=True)
    wallet_build.add_argument("--fee", type=int, required=True)
    wallet_build.add_argument("--change-address")

    wallet_send = subparsers.add_parser("wallet-send")
    wallet_send.add_argument("--wallet-file", required=True)
    wallet_send.add_argument("--to", required=True)
    wallet_send.add_argument("--amount", type=int, required=True)
    wallet_send.add_argument("--fee", type=int, required=True)
    wallet_send.add_argument("--change-address")
    wallet_send.add_argument("--connect", help="Optional node endpoint in host:port form for P2P submission.")

    return parser

def _print_json(payload) -> None:
    """Print JSON deterministically for CLI output."""

    json.dump(payload, sys.stdout, sort_keys=True)
    sys.stdout.write("\n")


def _print_operator_check(payload: dict[str, object]) -> None:
    """Print a compact human-readable operator readiness report."""

    print(f"operator-check: {payload['status']} - {payload['message']}")
    print(f"network: {payload['network']}")
    sections = payload["sections"]
    assert isinstance(sections, dict)
    for name in ("chain", "peers", "sync", "supply", "rewards", "reward_node", "mining", "snapshot"):
        section = sections[name]
        assert isinstance(section, dict)
        print(f"[{str(section['status']).upper()}] {name}: {section['message']}")
        fields = section.get("fields", {})
        if isinstance(fields, dict):
            for key in sorted(fields):
                value = fields[key]
                if isinstance(value, (dict, list, tuple)):
                    rendered = json.dumps(value, sort_keys=True)
                else:
                    rendered = str(value)
                print(f"  {key}: {rendered}")


def _operator_snapshot_manifest_urls(values: list[str]) -> list[str]:
    """Return configured snapshot manifest URLs from CLI or environment."""

    if values:
        return values
    raw = os.environ.get("NODE_SNAPSHOT_MANIFEST_URLS", "")
    return [item.strip() for item in raw.split(",") if item.strip()]


def _enrich_operator_check_payload(
    payload: dict[str, object],
    *,
    node_url: str | None,
    snapshot_manifest_urls: list[str],
    network: str,
    timeout_seconds: float,
    miner_payout_address: str | None,
) -> None:
    """Add optional external checks and recompute aggregate status."""

    sections = payload["sections"]
    assert isinstance(sections, dict)
    if node_url:
        sections["mining"] = _operator_http_mining_section(
            node_url=node_url,
            timeout_seconds=timeout_seconds,
            miner_payout_address=miner_payout_address,
        )
    if snapshot_manifest_urls:
        sections["snapshot"] = _operator_snapshot_manifest_section(
            existing_section=sections["snapshot"],
            manifest_urls=snapshot_manifest_urls,
            network=network,
            timeout_seconds=timeout_seconds,
        )
    payload["status"] = _operator_worst_status(str(section["status"]) for section in sections.values() if isinstance(section, dict))
    payload["message"] = _operator_status_message(str(payload["status"]))


def _operator_http_mining_section(
    *,
    node_url: str,
    timeout_seconds: float,
    miner_payout_address: str | None,
) -> dict[str, object]:
    """Check remote mining status and template acquisition over HTTP."""

    base_url = node_url.rstrip("/")
    if miner_payout_address is not None and not is_valid_address(miner_payout_address):
        return {
            "status": "fail",
            "message": "mining template check failed: invalid --miner-payout-address",
            "fields": {
                "node_url": base_url,
                "miner_payout_address": miner_payout_address,
                "status_available": None,
                "template_available": False,
                "error": "invalid --miner-payout-address",
            },
        }
    try:
        status_payload = _operator_http_json(
            f"{base_url}/mining/status",
            method="GET",
            timeout_seconds=timeout_seconds,
        )
        if miner_payout_address is None:
            return {
                "status": "warn",
                "message": "mining template check skipped: missing --miner-payout-address",
                "fields": {
                    "node_url": base_url,
                    "best_height": status_payload.get("best_height"),
                    "sync_phase": status_payload.get("sync_phase"),
                    "status_available": True,
                    "template_available": None,
                    "template_check_skipped": True,
                    "skip_reason": "missing --miner-payout-address",
                },
            }
        template_payload = _operator_http_json(
            f"{base_url}/mining/get-block-template",
            method="POST",
            payload={
                "payout_address": miner_payout_address,
                "miner_id": "operator-check",
                "template_mode": "header_and_coinbase_data",
            },
            timeout_seconds=timeout_seconds,
        )
        return {
            "status": "ok",
            "message": "HTTP mining status and template acquisition are available.",
            "fields": {
                "node_url": base_url,
                "miner_payout_address": miner_payout_address,
                "best_height": status_payload.get("best_height"),
                "sync_phase": status_payload.get("sync_phase"),
                "status_available": True,
                "template_available": True,
                "template_height": template_payload.get("height"),
                "previous_block_hash": template_payload.get("previous_block_hash"),
                "template_id_present": bool(template_payload.get("template_id")),
            },
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "status": "fail",
            "message": f"HTTP mining template check failed: {exc}",
            "fields": {
                "node_url": base_url,
                "miner_payout_address": miner_payout_address,
                "status_available": False,
                "template_available": False,
                "error": str(exc),
            },
        }


def _operator_snapshot_manifest_section(
    *,
    existing_section: object,
    manifest_urls: list[str],
    network: str,
    timeout_seconds: float,
) -> dict[str, object]:
    """Check configured snapshot manifests and preserve local snapshot fields."""

    existing_fields = {}
    existing_status = "ok"
    if isinstance(existing_section, dict):
        existing_status = str(existing_section.get("status", "ok"))
        fields = existing_section.get("fields", {})
        if isinstance(fields, dict):
            existing_fields = dict(fields)
    errors = []
    selected = None
    for manifest_url in manifest_urls:
        try:
            payload = _operator_http_json(manifest_url, method="GET", timeout_seconds=timeout_seconds)
            entries = _operator_parse_snapshot_manifest(payload, manifest_url=manifest_url)
            compatible = [entry for entry in entries if entry["network"] == network and int(entry["format_version"]) in {1, 2}]
            if compatible:
                compatible.sort(key=lambda entry: (int(entry["snapshot_height"]), int(entry["created_at"]), int(entry["format_version"])), reverse=True)
                selected = compatible[0]
                break
            errors.append(f"{manifest_url}: no compatible snapshot entries")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{manifest_url}: {exc}")
    if selected is None:
        status = "warn" if existing_status != "fail" else "fail"
        message = "Configured snapshot manifest is not readable or has no compatible entries."
        manifest_fields = {
            "manifest_urls": manifest_urls,
            "manifest_readable": False,
            "manifest_errors": errors,
        }
    else:
        age_seconds = max(0, int(time.time()) - int(selected["created_at"]))
        status = "warn" if age_seconds >= 7 * 24 * 60 * 60 else existing_status
        message = "Snapshot manifest is readable and compatible."
        if status == "warn":
            message = "Snapshot manifest is readable but old."
        manifest_fields = {
            "manifest_urls": manifest_urls,
            "manifest_readable": True,
            "manifest_snapshot_url": selected["snapshot_url"],
            "manifest_snapshot_height": selected["snapshot_height"],
            "manifest_snapshot_block_hash": selected["snapshot_block_hash"],
            "manifest_created_at": selected["created_at"],
            "manifest_age_seconds": age_seconds,
            "manifest_errors": errors,
        }
    return {
        "status": status,
        "message": message,
        "fields": {**existing_fields, **manifest_fields},
    }


def _operator_parse_snapshot_manifest(payload: object, *, manifest_url: str) -> list[dict[str, object]]:
    """Parse supported snapshot manifest shapes for operator-check."""

    if isinstance(payload, dict):
        raw_entries = payload.get("snapshots", [])
    elif isinstance(payload, list):
        raw_entries = payload
    else:
        raise ValueError(f"unsupported snapshot manifest format from {manifest_url}")
    if not isinstance(raw_entries, list):
        raise ValueError(f"snapshot manifest entries must be a list: {manifest_url}")
    entries = []
    for raw_entry in raw_entries:
        if not isinstance(raw_entry, dict):
            raise ValueError(f"snapshot manifest entry must be an object: {manifest_url}")
        entries.append(
            {
                "network": str(raw_entry["network"]),
                "snapshot_url": str(raw_entry["snapshot_url"]),
                "format_version": int(raw_entry["format_version"]),
                "snapshot_height": int(raw_entry["snapshot_height"]),
                "snapshot_block_hash": str(raw_entry["snapshot_block_hash"]),
                "created_at": int(raw_entry["created_at"]),
            }
        )
    return entries


def _operator_http_json(
    url: str,
    *,
    method: str,
    timeout_seconds: float,
    payload: dict[str, object] | None = None,
) -> dict[str, object] | list[object]:
    """Fetch one JSON payload for optional operator checks."""

    body = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {} if payload is None else {"Content-Type": "application/json"}
    req = request.Request(url, data=body, method=method, headers=headers)
    with request.urlopen(req, timeout=timeout_seconds) as response:
        return json.loads(response.read().decode("utf-8"))


def _operator_worst_status(statuses) -> str:
    """Return the most severe operator status."""

    rank = {"ok": 0, "warn": 1, "fail": 2}
    worst = "ok"
    for status in statuses:
        value = str(status)
        if rank.get(value, 2) > rank[worst]:
            worst = value if value in rank else "fail"
    return worst


def _operator_status_message(status: str) -> str:
    """Return a concise operator-check status message."""

    if status == "ok":
        return "Node is ready for public testnet operation."
    if status == "warn":
        return "Node is operational but needs operator attention before public testnet use."
    return "Node is not ready for public testnet operation."


def _print_error(message: str) -> None:
    """Print a readable JSON error payload to stderr."""

    json.dump({"error": message}, sys.stderr, sort_keys=True)
    sys.stderr.write("\n")


def _print_warning(message: str) -> None:
    """Print a readable JSON warning payload to stderr."""

    json.dump({"warning": message}, sys.stderr, sort_keys=True)
    sys.stderr.write("\n")


def _parse_bool(raw: str) -> bool:
    """Parse one shell-friendly boolean CLI value."""

    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise argparse.ArgumentTypeError(f"invalid boolean value: {raw}")


def _parse_snapshot_trusted_keys(values: list[str]) -> tuple[bytes, ...]:
    """Decode all trusted snapshot signer public keys from CLI input."""

    return tuple(parse_ed25519_public_key_hex(value) for value in values)


def _load_snapshot_trusted_keys(values: list[str], files: list[str]) -> tuple[bytes, ...]:
    """Load trusted snapshot signer public keys from CLI flags and files."""

    loaded_keys = list(_parse_snapshot_trusted_keys(values))
    for raw_path in files:
        path = Path(raw_path)
        text = path.read_text(encoding="utf-8").strip()
        if not text:
            continue
        if text.startswith("{") or text.startswith("["):
            payload = json.loads(text)
            if isinstance(payload, dict):
                raw_values = payload.get("trusted_keys", [])
            elif isinstance(payload, list):
                raw_values = payload
            else:
                raise ValueError(f"Unsupported snapshot trusted keys file format: {path}")
            if not isinstance(raw_values, list):
                raise ValueError(f"snapshot trusted keys file must contain a list: {path}")
            loaded_keys.extend(parse_ed25519_public_key_hex(str(value)) for value in raw_values)
            continue
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            loaded_keys.append(parse_ed25519_public_key_hex(stripped))
    deduped: dict[str, bytes] = {}
    for key in loaded_keys:
        deduped[key.hex()] = key
    return tuple(deduped.values())


def _emit_snapshot_warnings(metadata: dict[str, object], *, trust_mode: str) -> None:
    """Emit operator-facing warnings when warn mode accepted a weak snapshot."""

    if trust_mode != "warn":
        return
    warnings = metadata.get("warnings", [])
    if not isinstance(warnings, list):
        return
    for warning in warnings:
        _print_warning(f"{warning}; snapshot import continued only because --snapshot-trust-mode=warn")


def _normalize_node_url(raw: str) -> str:
    """Validate one mining node URL."""

    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"invalid node URL: {raw}")
    return raw.rstrip("/")


def _run_miner_worker(args) -> dict[str, object]:
    """Run the lightweight template-based miner worker."""

    if not args.node_url:
        raise ValueError("mine requires at least one --node-url endpoint")
    config = MinerWorkerConfig(
        network=args.network,
        payout_address=args.miner_address,
        node_urls=tuple(_normalize_node_url(url) for url in args.node_url),
        miner_id=args.miner_id or f"miner-{secrets.token_hex(6)}",
        polling_interval_seconds=float(args.polling_interval_seconds),
        request_timeout_seconds=float(args.request_timeout_seconds),
        nonce_batch_size=int(args.nonce_batch_size),
        template_refresh_skew_seconds=int(args.template_refresh_skew_seconds),
        mining_min_interval_seconds=float(args.mining_min_interval_seconds),
        run_seconds=args.run_seconds,
    )
    worker = MinerWorker(config)
    return worker.run()


def _mine_local_candidate_block(service: NodeService, payout_address: str):
    """Build, solve, and apply one local candidate block."""

    candidate = service.build_candidate_block(payout_address).block
    for nonce in range(2_000_000):
        header = replace(candidate.header, nonce=nonce)
        if verify_proof_of_work(header):
            solved = replace(candidate, header=header)
            service.apply_block(solved)
            return solved
    raise ValueError("unable to find a valid nonce for the local candidate block")


def _submit_raw_transaction_via_http(args) -> dict[str, object]:
    """Submit one raw transaction through the runtime-owned HTTP API."""

    node_url = _normalize_node_url(args.node_url)
    encoded = json.dumps({"raw_hex": args.raw_hex}, sort_keys=True).encode("utf-8")
    req = request.Request(
        f"{node_url}/v1/tx/submit",
        method="POST",
        data=encoded,
        headers={"Accept": "application/json", "Content-Type": "application/json"},
    )
    try:
        with request.urlopen(req, timeout=10.0) as response:
            payload = response.read()
    except error.HTTPError as exc:
        try:
            message = json.loads(exc.read().decode("utf-8")).get("error", {}).get("message")
        except Exception:
            message = None
        raise ValueError(message or f"transaction submit failed with HTTP {exc.code}") from exc
    except error.URLError as exc:
        raise ValueError(f"unable to reach node API at {node_url}: {exc.reason}") from exc
    return {} if not payload else json.loads(payload.decode("utf-8"))


async def _run_runtime(service: NodeService, args) -> None:
    """Run the persistent node runtime."""

    peers = [_parse_peer(peer) for peer in args.peer]
    _emit_runtime_warnings(service, args, peers)
    network_config = get_network_config(service.network)
    runtime = NodeRuntime(
        service=service,
        listen_host=args.listen_host,
        listen_port=network_config.default_p2p_port if args.listen_port is None else args.listen_port,
        outbound_peers=peers,
        ping_interval=float(getattr(args, "ping_interval_seconds", 2.0)),
        read_timeout=float(getattr(args, "read_timeout_seconds", 15.0)),
        write_timeout=float(getattr(args, "write_timeout_seconds", 15.0)),
        handshake_timeout=float(getattr(args, "handshake_timeout_seconds", 5.0)),
        peer_discovery_enabled=getattr(args, "peer_discovery_enabled", True),
        peerbook_max_size=getattr(args, "peerbook_max_size", 1024),
        peer_addr_max_per_message=getattr(args, "peer_addr_max_per_message", 250),
        peer_addr_relay_limit_per_interval=getattr(args, "peer_addr_relay_limit_per_interval", 250),
        peer_addr_relay_interval_seconds=getattr(args, "peer_addr_relay_interval_seconds", 30),
        peer_stale_after_seconds=getattr(args, "peer_stale_after_seconds", 604800),
        peer_retry_backoff_base_seconds=getattr(args, "peer_retry_backoff_base_seconds", 1.0),
        peer_retry_backoff_max_seconds=getattr(args, "peer_retry_backoff_max_seconds", 30.0),
        max_outbound_sessions=getattr(args, "max_outbound_sessions", 8),
        max_inbound_sessions=getattr(args, "max_inbound_sessions", 32),
        inbound_handshake_rate_limit_per_minute=getattr(args, "inbound_handshake_rate_limit_per_minute", 12),
        min_stable_session_seconds=getattr(args, "min_stable_session_seconds", 30.0),
        peer_discovery_startup_prefer_persisted=getattr(args, "peer_discovery_startup_prefer_persisted", True),
        headers_sync_enabled=getattr(args, "headers_sync_enabled", True),
        max_headers_per_message=getattr(args, "headers_max_per_message", 2000),
        block_download_window_size=getattr(args, "block_download_window_size", 128),
        block_max_inflight_per_peer=getattr(args, "block_max_inflight_per_peer", 16),
        block_request_timeout_seconds=getattr(args, "block_request_timeout_seconds", 15.0),
        headers_sync_parallel_peers=getattr(args, "headers_sync_parallel_peers", 2),
        headers_sync_start_height_gap_threshold=getattr(args, "headers_sync_start_height_gap_threshold", 1),
        misbehavior_warning_threshold=getattr(args, "misbehavior_warning_threshold", 25),
        misbehavior_disconnect_threshold=getattr(args, "misbehavior_disconnect_threshold", 50),
        misbehavior_ban_threshold=getattr(args, "misbehavior_ban_threshold", 100),
        misbehavior_ban_duration_seconds=getattr(args, "misbehavior_ban_duration_seconds", 1800),
        misbehavior_decay_interval_seconds=getattr(args, "misbehavior_decay_interval_seconds", 300),
        misbehavior_decay_step=getattr(args, "misbehavior_decay_step", 5),
        http_host=getattr(args, "http_host", None),
        http_port=getattr(args, "http_port", None),
        reward_automation=load_reward_node_automation_config_from_env(),
        logger=None,
    )
    configured_peer_source = getattr(args, "peer_source", "manual")
    if hasattr(runtime, "_outbound_target_sources"):
        runtime._outbound_target_sources.update({(peer.host, peer.port): configured_peer_source for peer in peers})
    await runtime.start()
    if args.run_seconds is not None:
        await asyncio.sleep(args.run_seconds)
        await runtime.stop()
        return
    try:
        await runtime.run_forever()
    except KeyboardInterrupt:
        await runtime.stop()


def _emit_runtime_warnings(service: NodeService, args, peers: list[OutboundPeer]) -> None:
    """Emit conservative startup warnings for isolated or suspicious runtime configs."""

    logger = logging.getLogger("chipcoin.runtime.config")
    peer_discovery_enabled = bool(getattr(args, "peer_discovery_enabled", True))
    persisted_peers = service.list_peers()
    if not peer_discovery_enabled and not peers and not persisted_peers:
        logger.warning("startup warning: peer discovery is disabled and no peers are configured; runtime will stay isolated")
    elif not peers and not persisted_peers:
        logger.warning(
            "startup warning: no configured peers and empty peerbook; runtime will wait for inbound peers or later discovery"
        )

    block_request_timeout_seconds = float(getattr(args, "block_request_timeout_seconds", 15.0))
    block_download_window_size = int(getattr(args, "block_download_window_size", 128))
    block_max_inflight_per_peer = int(getattr(args, "block_max_inflight_per_peer", 16))

    if block_request_timeout_seconds < 5:
        logger.warning(
            "startup warning: block request timeout %.2fs is unusually low and may cause unnecessary reassignment churn",
            block_request_timeout_seconds,
        )
    if block_download_window_size < block_max_inflight_per_peer:
        logger.warning(
            "startup warning: block download window size %s is below per-peer inflight cap %s; effective throughput will be reduced",
            block_download_window_size,
            block_max_inflight_per_peer,
        )


def _parse_peer(raw: str) -> OutboundPeer:
    """Parse an outbound peer endpoint from host:port text."""

    host, separator, port_text = raw.rpartition(":")
    if not separator:
        raise ValueError(f"invalid peer endpoint: {raw}")
    return OutboundPeer(host=host, port=int(port_text))


def _save_wallet_key(path: Path, wallet_key: WalletKey) -> None:
    """Persist a minimal wallet key file."""

    payload = {
        "private_key_hex": serialize_private_key_hex(wallet_key.private_key),
        "public_key_hex": serialize_public_key_hex(wallet_key.public_key),
        "address": wallet_key.address,
        "compressed": wallet_key.compressed,
    }
    path.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def _load_wallet_key(path: Path) -> WalletKey:
    """Load and validate a minimal wallet key file."""

    payload = json.loads(path.read_text(encoding="utf-8"))
    wallet_key = wallet_key_from_private_key(
        parse_private_key_hex(str(payload["private_key_hex"])),
        compressed=bool(payload.get("compressed", True)),
    )
    stored_public_key_hex = payload.get("public_key_hex")
    stored_address = payload.get("address")
    if stored_public_key_hex is not None and stored_public_key_hex != serialize_public_key_hex(wallet_key.public_key):
        raise ValueError("Wallet file public key does not match the stored private key.")
    if stored_address is not None and stored_address != wallet_key.address:
        raise ValueError("Wallet file address does not match the stored private key.")
    return wallet_key


def _format_wallet_key(wallet_key: WalletKey) -> dict[str, str | bool]:
    """Convert a wallet key to CLI/HTTP-friendly text values."""

    return {
        "private_key_hex": serialize_private_key_hex(wallet_key.private_key),
        "public_key_hex": serialize_public_key_hex(wallet_key.public_key),
        "address": wallet_key.address,
        "compressed": wallet_key.compressed,
    }


def _build_register_node_transaction(service: NodeService, wallet_key: WalletKey, args):
    """Build a signed `register_node` special transaction with helpful prechecks."""

    signer = TransactionSigner(wallet_key)
    if service.get_registered_node(args.node_id) is not None:
        raise ValueError("Node id is already registered.")
    if service.get_registered_node_by_owner(wallet_key.public_key) is not None:
        raise ValueError("This wallet owner is already registered to a node.")
    return signer.build_register_node_transaction(node_id=args.node_id, payout_address=args.payout_address)


def _build_renew_node_transaction(service: NodeService, wallet_key: WalletKey, args, *, current_epoch: int):
    """Build a signed `renew_node` special transaction with owner consistency checks."""

    signer = TransactionSigner(wallet_key)
    record = service.get_registered_node(args.node_id)
    if record is None:
        raise ValueError("Node id is not registered.")
    if record.owner_pubkey != wallet_key.public_key:
        raise ValueError("Wallet does not match the registered node owner.")
    return signer.build_renew_node_transaction(node_id=args.node_id, renewal_epoch=current_epoch)


def _build_register_reward_node_transaction(service: NodeService, wallet_key: WalletKey, args) -> Transaction:
    """Build a signed native `register_reward_node` transaction."""

    signer = TransactionSigner(wallet_key)
    existing = service.get_registered_node(args.node_id)
    if existing is not None and (existing.reward_registration or existing.owner_pubkey != wallet_key.public_key):
        raise ValueError("Node id is already registered.")
    existing_owner = service.get_registered_node_by_owner(wallet_key.public_key)
    if existing_owner is not None and existing_owner.node_id != args.node_id:
        raise ValueError("This wallet owner is already registered to a node.")
    return signer.build_register_reward_node_transaction(
        node_id=args.node_id,
        payout_address=args.payout_address,
        node_public_key_hex=args.node_pubkey_hex,
        declared_host=args.declared_host,
        declared_port=args.declared_port,
        registration_fee_chipbits=int(service.reward_node_fee_schedule()["register_fee_chipbits"]),
    )


def _build_renew_reward_node_transaction(service: NodeService, wallet_key: WalletKey, args, *, current_epoch: int) -> Transaction:
    """Build a signed native `renew_reward_node` transaction."""

    signer = TransactionSigner(wallet_key)
    record = service.get_registered_node(args.node_id)
    if record is None:
        raise ValueError("Node id is not registered.")
    if record.owner_pubkey != wallet_key.public_key:
        raise ValueError("Wallet does not match the registered node owner.")
    return signer.build_renew_reward_node_transaction(
        node_id=args.node_id,
        renewal_epoch=current_epoch,
        declared_host=args.declared_host,
        declared_port=args.declared_port,
        renewal_fee_chipbits=int(service.reward_node_fee_schedule()["renew_fee_chipbits"]),
    )


def _build_reward_attestation_bundle_transaction(service: NodeService, args, wallet_key: WalletKey | None) -> Transaction:
    """Build a native `reward_attestation_bundle` transaction from JSON input."""

    payload = json.loads(Path(args.bundle_file).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Bundle file must contain a JSON object.")
    raw_attestations = payload.get("attestations", [])
    if not isinstance(raw_attestations, list):
        raise ValueError("Bundle file attestations must be a JSON array.")
    signer = None if wallet_key is None else TransactionSigner(wallet_key)
    attestations: list[RewardAttestation] = []
    for raw in raw_attestations:
        if not isinstance(raw, dict):
            raise ValueError("Bundle attestation entries must be objects.")
        attestation = RewardAttestation(
            epoch_index=int(raw["epoch_index"]),
            check_window_index=int(raw["check_window_index"]),
            candidate_node_id=str(raw["candidate_node_id"]),
            verifier_node_id=str(raw["verifier_node_id"]),
            result_code=str(raw["result_code"]),
            observed_sync_gap=int(raw["observed_sync_gap"]),
            endpoint_commitment=str(raw["endpoint_commitment"]),
            concentration_key=str(raw["concentration_key"]),
            signature_hex=str(raw.get("signature_hex", "")),
        )
        if wallet_key is not None:
            verifier_record = service.get_registered_node(attestation.verifier_node_id)
            if verifier_record is None:
                raise ValueError(
                    f"Verifier node_id is not registered locally: {attestation.verifier_node_id}"
                )
            if verifier_record.node_pubkey is None:
                raise ValueError(
                    f"Registered verifier node is missing node_pubkey: {attestation.verifier_node_id}"
                )
            if verifier_record.node_pubkey != wallet_key.public_key:
                raise ValueError(
                    "Wallet public key does not match the registered verifier node_pubkey "
                    f"for verifier_node_id={attestation.verifier_node_id}"
                )
        if signer is not None:
            attestation = signer.sign_reward_attestation(attestation)
        attestations.append(attestation)
    metadata = {
        "kind": "reward_attestation_bundle",
        "epoch_index": str(payload["epoch_index"]),
        "bundle_window_index": str(payload["bundle_window_index"]),
        "bundle_submitter_node_id": str(payload["bundle_submitter_node_id"]),
        "attestation_count": str(len(attestations)),
        "attestations_json": json.dumps(
            [
                {
                    "epoch_index": attestation.epoch_index,
                    "check_window_index": attestation.check_window_index,
                    "candidate_node_id": attestation.candidate_node_id,
                    "verifier_node_id": attestation.verifier_node_id,
                    "result_code": attestation.result_code,
                    "observed_sync_gap": attestation.observed_sync_gap,
                    "endpoint_commitment": attestation.endpoint_commitment,
                    "concentration_key": attestation.concentration_key,
                    "signature_hex": attestation.signature_hex,
                }
                for attestation in attestations
            ],
            sort_keys=True,
            separators=(",", ":"),
        ),
    }
    return Transaction(version=1, inputs=(), outputs=(), metadata=metadata)


def _build_reward_settle_epoch_transaction(args) -> Transaction:
    """Build a native `reward_settle_epoch` transaction from JSON input."""

    payload = json.loads(Path(args.settlement_file).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Settlement file must contain a JSON object.")
    reward_entries = payload.get("reward_entries", [])
    if not isinstance(reward_entries, list):
        raise ValueError("Settlement reward_entries must be a JSON array.")
    metadata = {
        "kind": "reward_settle_epoch",
        "epoch_index": str(payload["epoch_index"]),
        "epoch_start_height": str(payload["epoch_start_height"]),
        "epoch_end_height": str(payload["epoch_end_height"]),
        "epoch_seed": str(payload["epoch_seed"]),
        "policy_version": str(payload["policy_version"]),
        "candidate_summary_root": str(payload["candidate_summary_root"]),
        "verified_nodes_root": str(payload["verified_nodes_root"]),
        "rewarded_nodes_root": str(payload["rewarded_nodes_root"]),
        "rewarded_node_count": str(payload["rewarded_node_count"]),
        "distributed_node_reward_chipbits": str(payload["distributed_node_reward_chipbits"]),
        "undistributed_node_reward_chipbits": str(payload["undistributed_node_reward_chipbits"]),
        "reward_entries_json": json.dumps(reward_entries, sort_keys=True, separators=(",", ":")),
    }
    return Transaction(version=1, inputs=(), outputs=(), metadata=metadata)


def _submit_special_transaction(service: NodeService, transaction, connect: str | None) -> bool:
    """Submit a special node transaction locally or over the existing P2P boundary."""

    if connect:
        asyncio.run(_send_transaction_to_peer(transaction, _parse_peer(connect), network=service.network))
        return True
    service.receive_transaction(transaction)
    return True


def _build_wallet_transaction(service: NodeService, wallet_key: WalletKey, args) -> object:
    """Construct a signed wallet transaction using active-chain spendable outputs."""

    signer = TransactionSigner(wallet_key)
    spend_candidates = service.list_spendable_outputs(wallet_key.address)
    return signer.build_signed_transaction(
        spend_candidates=spend_candidates,
        recipient=args.to,
        amount_chipbits=args.amount,
        fee_chipbits=args.fee,
        change_recipient=args.change_address,
        metadata={"kind": "payment"},
    )


async def _send_transaction_to_peer(transaction, peer: OutboundPeer, *, network: str) -> None:
    """Open a temporary P2P session and relay one transaction."""

    network_config = get_network_config(network)
    protocol = await PeerProtocol.connect(
        peer.host,
        peer.port,
        identity=LocalPeerIdentity(
            node_id=secrets.token_hex(16),
            network=network,
            start_height=0,
            user_agent="chipcoin-wallet/0.1",
            relay=False,
            network_magic=network_config.magic,
        ),
    )
    try:
        await protocol.send_message(MessageEnvelope(command="tx", payload=TransactionMessage(transaction=transaction)))
        await asyncio.sleep(0.1)
    finally:
        await protocol.close(reason="Wallet submission complete.")
