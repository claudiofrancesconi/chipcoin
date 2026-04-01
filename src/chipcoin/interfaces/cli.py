"""CLI for local Chipcoin v2 diagnostics and control."""

from __future__ import annotations

import argparse
import asyncio
import json
import secrets
import sys
from pathlib import Path

from ..config import DEFAULT_NETWORK, NETWORK_CONFIGS, get_network_config, resolve_data_path
from ..consensus.serialization import serialize_transaction
from ..crypto.keys import parse_private_key_hex, serialize_private_key_hex, serialize_public_key_hex
from ..node.messages import MessageEnvelope, TransactionMessage
from ..node.p2p.protocol import LocalPeerIdentity, PeerProtocol
from ..node.service import NodeService
from ..node.runtime import NodeRuntime, OutboundPeer
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
        service = None if args.command in {"wallet-generate", "wallet-import", "wallet-address"} else NodeService.open_sqlite(data_path, network=args.network)

        if args.command == "start":
            assert service is not None
            service.start()
            _print_json({"started": True, "status": service.status()})
            return 0

        if args.command == "run":
            assert service is not None
            asyncio.run(_run_runtime(service, args))
            return 0

        if args.command == "mine":
            assert service is not None
            asyncio.run(_run_runtime(service, args, miner_address=args.miner_address))
            _print_json({"mining": True, "tip": format_tip(service.chain_tip())})
            return 0

        if args.command == "status":
            assert service is not None
            _print_json(service.status())
            return 0

        if args.command == "tip":
            assert service is not None
            _print_json(service.tip_diagnostics())
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
            assert service is not None
            accepted = service.submit_raw_transaction(args.raw_hex)
            _print_json({"accepted": True, "txid": accepted.transaction.txid(), "fee": accepted.fee})
            return 0

        if args.command == "add-peer":
            assert service is not None
            peer = service.add_peer(args.host, args.port)
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
                    "reward_per_winner_chc": format_amount_chc(int(payload["reward_per_winner_chipbits"])),
                    "miner_subsidy_chc": format_amount_chc(int(payload["miner_subsidy_chipbits"])),
                    "node_reward_pool_chc": format_amount_chc(int(payload["node_reward_pool_chipbits"])),
                    "remainder_to_miner_chc": format_amount_chc(int(payload["remainder_to_miner_chipbits"])),
                    "selected_winners": [
                        {
                            **winner,
                            "reward_chc": format_amount_chc(int(winner["reward_chipbits"])),
                        }
                        for winner in payload["selected_winners"]
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
                        "remainder_from_node_pool_chc": format_amount_chc(int(row["remainder_from_node_pool_chipbits"])),
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
                    "current_miner_subsidy_chc": format_amount_chc(int(payload["current_miner_subsidy_chipbits"])),
                    "current_node_reward_pool_chc": format_amount_chc(int(payload["current_node_reward_pool_chipbits"])),
                    "total_emitted_supply_chc": format_amount_chc(int(payload["total_emitted_supply_chipbits"])),
                    "circulating_spendable_supply_chc": format_amount_chc(
                        int(payload["circulating_spendable_supply_chipbits"])
                    ),
                    "immature_supply_chc": format_amount_chc(int(payload["immature_supply_chipbits"])),
                    "max_supply_chc": format_amount_chc(int(payload["max_supply_chipbits"])),
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
                        "total_remainder_from_node_pool_chc": format_amount_chc(
                            int(row["total_remainder_from_node_pool_chipbits"])
                        ),
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
                    "current_miner_subsidy_chc": format_amount_chc(int(payload["current_miner_subsidy_chipbits"])),
                    "current_node_reward_pool_chc": format_amount_chc(int(payload["current_node_reward_pool_chipbits"])),
                    "total_emitted_supply_chc": format_amount_chc(int(payload["total_emitted_supply_chipbits"])),
                    "circulating_spendable_supply_chc": format_amount_chc(
                        int(payload["circulating_spendable_supply_chipbits"])
                    ),
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
    run_parser.add_argument("--peer", action="append", default=[], help="Outbound peer in host:port form.")
    run_parser.add_argument("--run-seconds", type=float, default=None, help="Stop automatically after N seconds.")
    run_parser.add_argument("--miner-address", default=None, help="Enable local mining to the supplied payout address.")
    run_parser.add_argument(
        "--mining-min-interval-seconds",
        type=float,
        default=0.0,
        help="Optional local throttle between mined blocks. Does not change consensus.",
    )
    mine_parser = subparsers.add_parser("mine")
    mine_parser.add_argument("--listen-host", default="127.0.0.1")
    mine_parser.add_argument("--listen-port", type=int, default=None)
    mine_parser.add_argument("--peer", action="append", default=[], help="Outbound peer in host:port form.")
    mine_parser.add_argument("--run-seconds", type=float, default=None, help="Stop automatically after N seconds.")
    mine_parser.add_argument("--miner-address", required=True, help="Mining reward payout address.")
    mine_parser.add_argument(
        "--mining-min-interval-seconds",
        type=float,
        default=0.0,
        help="Optional local throttle between mined blocks. Does not change consensus.",
    )
    subparsers.add_parser("status")
    subparsers.add_parser("tip")

    block_parser = subparsers.add_parser("block")
    group = block_parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--height", type=int)
    group.add_argument("--hash")

    tx_parser = subparsers.add_parser("tx")
    tx_parser.add_argument("txid")

    submit_parser = subparsers.add_parser("submit-raw-tx")
    submit_parser.add_argument("raw_hex")

    add_peer_parser = subparsers.add_parser("add-peer")
    add_peer_parser.add_argument("host")
    add_peer_parser.add_argument("port", type=int)

    subparsers.add_parser("list-peers")
    peer_detail_parser = subparsers.add_parser("peer-detail")
    peer_detail_parser.add_argument("--node-id", required=True)
    subparsers.add_parser("peer-summary")
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


def _print_error(message: str) -> None:
    """Print a readable JSON error payload to stderr."""

    json.dump({"error": message}, sys.stderr, sort_keys=True)
    sys.stderr.write("\n")


async def _run_runtime(service: NodeService, args, miner_address: str | None = None) -> None:
    """Run the persistent node runtime."""

    peers = [_parse_peer(peer) for peer in args.peer]
    network_config = get_network_config(service.network)
    runtime = NodeRuntime(
        service=service,
        listen_host=args.listen_host,
        listen_port=network_config.default_p2p_port if args.listen_port is None else args.listen_port,
        outbound_peers=peers,
        miner_address=miner_address if miner_address is not None else getattr(args, "miner_address", None),
        mining_min_interval_seconds=getattr(args, "mining_min_interval_seconds", 0.0),
        logger=None,
    )
    await runtime.start()
    if args.run_seconds is not None:
        await asyncio.sleep(args.run_seconds)
        await runtime.stop()
        return
    try:
        await runtime.run_forever()
    except KeyboardInterrupt:
        await runtime.stop()


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
