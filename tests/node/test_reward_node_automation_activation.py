"""Regression tests for reward-node attestation activation gating."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

from chipcoin.consensus.params import DEVNET_PARAMS
from chipcoin.node.runtime import NodeRuntime, RewardNodeAutomationConfig
from chipcoin.node.service import NodeService
from chipcoin.wallet.signer import TransactionSigner
from tests.helpers import wallet_key
from tests.node.test_reward_node_automation import _apply_candidate_block, _write_wallet_file


def _activation_params():
    return replace(
        DEVNET_PARAMS,
        coinbase_maturity=0,
        node_reward_activation_height=2,
        reward_node_warmup_epochs=0,
        epoch_length_blocks=100,
        reward_check_windows_per_epoch=4,
        reward_target_checks_per_epoch=1,
        reward_min_passed_checks_per_epoch=1,
        reward_verifier_committee_size=1,
        reward_verifier_quorum=1,
        reward_final_confirmation_window_blocks=1,
        max_rewarded_nodes_per_epoch=4,
    )


def _make_activation_service(database_path: Path, *, start_time: int) -> NodeService:
    timestamps = iter(range(start_time, start_time + 1000))
    return NodeService.open_sqlite(
        database_path,
        network="devnet",
        params=_activation_params(),
        time_provider=lambda: next(timestamps),
    )


def _register_reward_node(service: NodeService, *, wallet, node_id: str, declared_port: int) -> None:
    service.receive_transaction(
        TransactionSigner(wallet).build_register_reward_node_transaction(
            node_id=node_id,
            payout_address=wallet.address,
            node_public_key_hex=wallet.public_key.hex(),
            declared_host="127.0.0.1",
            declared_port=declared_port,
            registration_fee_chipbits=int(service.reward_node_fee_schedule()["register_fee_chipbits"]),
        )
    )


def test_reward_node_automation_does_not_attest_before_activation_height() -> None:
    with TemporaryDirectory() as tempdir:
        reward_a = wallet_key(0)
        reward_b = wallet_key(1)
        service = _make_activation_service(Path(tempdir) / "node.sqlite3", start_time=1_700_080_000)
        reward_a_path = _write_wallet_file(Path(tempdir) / "reward-a.json", reward_a)
        reward_b_path = _write_wallet_file(Path(tempdir) / "reward-b.json", reward_b)
        runtime_a = NodeRuntime(
            service=service,
            reward_automation=RewardNodeAutomationConfig(
                node_id="reward-node-a",
                owner_wallet_path=reward_a_path,
                attest_wallet_path=reward_a_path,
                poll_interval_seconds=0.05,
            ),
        )
        runtime_b = NodeRuntime(
            service=service,
            reward_automation=RewardNodeAutomationConfig(
                node_id="reward-node-b",
                owner_wallet_path=reward_b_path,
                attest_wallet_path=reward_b_path,
                poll_interval_seconds=0.05,
            ),
        )

        _register_reward_node(service, wallet=reward_a, node_id="reward-node-a", declared_port=18444)
        _register_reward_node(service, wallet=reward_b, node_id="reward-node-b", declared_port=18445)
        _apply_candidate_block(service, reward_a.address)

        asyncio.run(runtime_a._run_reward_automation_once())
        asyncio.run(runtime_b._run_reward_automation_once())
        assert [
            tx for tx in service.list_mempool_transactions() if tx.metadata.get("kind") == "reward_attestation_bundle"
        ] == []

        _apply_candidate_block(service, reward_a.address)

        asyncio.run(runtime_a._run_reward_automation_once())
        asyncio.run(runtime_b._run_reward_automation_once())
        attestation_txs = [tx for tx in service.list_mempool_transactions() if tx.metadata.get("kind") == "reward_attestation_bundle"]
        assert attestation_txs
        for transaction in attestation_txs:
            json.loads(transaction.metadata["attestations_json"])
