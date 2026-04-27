from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import json

from chipcoin.consensus.economics import subsidy_split_chipbits
from chipcoin.consensus.epoch_settlement import RewardAttestation, parse_reward_settlement_metadata
from chipcoin.consensus.models import Block, Transaction
from chipcoin.consensus.params import DEVNET_PARAMS
from chipcoin.consensus.pow import verify_proof_of_work
from chipcoin.node.service import NodeService
from chipcoin.node.sync import SyncManager
from chipcoin.consensus.validation import ValidationError
from chipcoin.wallet.signer import TransactionSigner
from tests.helpers import wallet_key


def _make_params():
    return replace(
        DEVNET_PARAMS,
        node_reward_activation_height=0,
        epoch_length_blocks=5,
        reward_check_windows_per_epoch=4,
        reward_target_checks_per_epoch=1,
        reward_min_passed_checks_per_epoch=1,
        reward_verifier_committee_size=1,
        reward_verifier_quorum=1,
        reward_final_confirmation_window_blocks=1,
        max_rewarded_nodes_per_epoch=4,
        reward_node_warmup_epochs=0,
    )


def _make_service(database_path: Path, *, start_time: int = 1_700_000_000) -> NodeService:
    timestamps = iter(range(start_time, start_time + 400))
    return NodeService.open_sqlite(database_path, network="devnet", params=_make_params(), time_provider=lambda: next(timestamps))


def _make_boundary_params():
    return replace(_make_params(), reward_node_warmup_epochs=1)


def _make_boundary_service(database_path: Path, *, start_time: int = 1_701_000_000) -> NodeService:
    timestamps = iter(range(start_time, start_time + 400))
    return NodeService.open_sqlite(database_path, network="devnet", params=_make_boundary_params(), time_provider=lambda: next(timestamps))


def _mine_block(block: Block) -> Block:
    for nonce in range(2_000_000):
        header = replace(block.header, nonce=nonce)
        if verify_proof_of_work(header):
            return replace(block, header=header)
    raise AssertionError("Expected to find a valid nonce for the easy target.")


def _mine_local_block(service: NodeService, payout_address: str) -> Block:
    block = _mine_block(service.build_candidate_block(payout_address).block)
    service.apply_block(block)
    return block


def _mine_until_height(service: NodeService, payout_address: str, target_height: int) -> None:
    while service.chain_tip() is not None and service.chain_tip().height < target_height:
        _mine_local_block(service, payout_address)


def _register_reward_node(service: NodeService, *, wallet, node_id: str, port: int) -> None:
    service.receive_transaction(
        TransactionSigner(wallet).build_register_reward_node_transaction(
            node_id=node_id,
            payout_address=wallet.address,
            node_public_key_hex=wallet.public_key.hex(),
            declared_host="127.0.0.1",
            declared_port=port,
            registration_fee_chipbits=int(service.reward_node_fee_schedule()["register_fee_chipbits"]),
        )
    )


def _renew_reward_node(service: NodeService, *, wallet, node_id: str, port: int) -> None:
    service.receive_transaction(
        TransactionSigner(wallet).build_renew_reward_node_transaction(
            node_id=node_id,
            renewal_epoch=service.next_block_epoch(),
            declared_host="127.0.0.1",
            declared_port=port,
            renewal_fee_chipbits=int(service.reward_node_fee_schedule()["renew_fee_chipbits"]),
        )
    )


def _submit_signed_attestation(
    service: NodeService,
    *,
    epoch_index: int,
    candidate_node_id: str,
    verifier_wallet,
    verifier_node_id: str,
    window_index: int,
    endpoint_commitment: str,
    concentration_key: str,
) -> None:
    attestation = TransactionSigner(verifier_wallet).sign_reward_attestation(
        RewardAttestation(
            epoch_index=epoch_index,
            check_window_index=window_index,
            candidate_node_id=candidate_node_id,
            verifier_node_id=verifier_node_id,
            result_code="pass",
            observed_sync_gap=0,
            endpoint_commitment=endpoint_commitment,
            concentration_key=concentration_key,
            signature_hex="",
        )
    )
    service.receive_transaction(
        Transaction(
            version=1,
            inputs=(),
            outputs=(),
            metadata={
                "kind": "reward_attestation_bundle",
                "epoch_index": str(epoch_index),
                "bundle_window_index": str(window_index),
                "bundle_submitter_node_id": verifier_node_id,
                "attestation_count": "1",
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
                    ],
                    sort_keys=True,
                ),
            },
        )
    )


def _build_settlement_transaction(preview: dict[str, object]) -> Transaction:
    return Transaction(
        version=1,
        inputs=(),
        outputs=(),
        metadata={
            "kind": "reward_settle_epoch",
            "epoch_index": str(preview["epoch_index"]),
            "epoch_start_height": str(preview["epoch_start_height"]),
            "epoch_end_height": str(preview["epoch_end_height"]),
            "epoch_seed": str(preview["epoch_seed"]),
            "policy_version": str(preview["policy_version"]),
            "candidate_summary_root": str(preview["candidate_summary_root"]),
            "verified_nodes_root": str(preview["verified_nodes_root"]),
            "rewarded_nodes_root": str(preview["rewarded_nodes_root"]),
            "rewarded_node_count": str(preview["rewarded_node_count"]),
            "distributed_node_reward_chipbits": str(preview["distributed_node_reward_chipbits"]),
            "undistributed_node_reward_chipbits": str(preview["undistributed_node_reward_chipbits"]),
            "reward_entries_json": json.dumps(preview["reward_entries"], sort_keys=True),
        },
    )


def _qualify_reward_node_for_epoch(
    service: NodeService,
    *,
    epoch_index: int,
    candidate_node_id: str,
    verifier_wallets_by_node_id: dict[str, object],
) -> None:
    assignment = service.native_reward_assignments(epoch_index=epoch_index, node_id=candidate_node_id)[0]
    window_index = next(
        window
        for window in assignment["candidate_check_windows"]
        if assignment["verifier_committees"].get(str(window))
    )
    verifier_node_id = assignment["verifier_committees"][str(window_index)][0]
    _submit_signed_attestation(
        service,
        epoch_index=epoch_index,
        candidate_node_id=candidate_node_id,
        verifier_wallet=verifier_wallets_by_node_id[verifier_node_id],
        verifier_node_id=verifier_node_id,
        window_index=window_index,
        endpoint_commitment=f"{assignment['declared_host']}:{assignment['declared_port']}",
        concentration_key=f"demo:{candidate_node_id}",
    )


def _wallet_by_node_id(*wallets_by_node_id: tuple[str, object]) -> dict[str, object]:
    return dict(wallets_by_node_id)


def _clone_service(source: NodeService, database_path: Path, *, start_time: int) -> NodeService:
    clone = _make_service(database_path, start_time=start_time)
    source.connection.commit()
    source.connection.backup(clone.connection)
    clone.connection.commit()
    return clone


def _reopen_service(database_path: Path, *, start_time: int) -> NodeService:
    return _make_service(database_path, start_time=start_time)


def test_native_reward_node_registration_and_renewal_persist_in_local_chain() -> None:
    with TemporaryDirectory() as tempdir:
        service = _make_service(Path(tempdir) / "chipcoin.sqlite3")
        owner = wallet_key(0)
        signer = TransactionSigner(owner)

        register_tx = signer.build_register_reward_node_transaction(
            node_id="reward-node-a",
            payout_address=owner.address,
            node_public_key_hex=owner.public_key.hex(),
            declared_host="127.0.0.1",
            declared_port=19001,
            registration_fee_chipbits=int(service.reward_node_fee_schedule()["register_fee_chipbits"]),
        )
        service.receive_transaction(register_tx)
        _mine_local_block(service, wallet_key(1).address)

        renew_tx = signer.build_renew_reward_node_transaction(
            node_id="reward-node-a",
            renewal_epoch=service.next_block_epoch(),
            declared_host="127.0.0.1",
            declared_port=19011,
            renewal_fee_chipbits=int(service.reward_node_fee_schedule()["renew_fee_chipbits"]),
        )
        service.receive_transaction(renew_tx)
        _mine_local_block(service, wallet_key(1).address)

        record = service.get_registered_node("reward-node-a")
        assert record is not None
        assert record.reward_registration is True
        assert record.node_pubkey == owner.public_key
        assert record.declared_host == "127.0.0.1"
        assert record.declared_port == 19011
        assert record.last_renewed_height == 1


def test_native_reward_attestation_and_auto_settlement_persist_after_mining() -> None:
    with TemporaryDirectory() as tempdir:
        service = _make_service(Path(tempdir) / "chipcoin.sqlite3")
        reward_a = wallet_key(0)
        reward_b = wallet_key(1)

        for node_id, wallet, port in (
            ("reward-node-a", reward_a, 19001),
            ("reward-node-b", reward_b, 19002),
        ):
            _register_reward_node(service, wallet=wallet, node_id=node_id, port=port)
        _mine_local_block(service, wallet_key(2).address)

        assignment = service.native_reward_assignments(epoch_index=0, node_id="reward-node-a")[0]
        assert assignment["declared_host"] == "127.0.0.1"
        assert assignment["declared_port"] == 19001
        window_index = assignment["candidate_check_windows"][0]
        verifier_node_id = assignment["verifier_committees"][str(window_index)][0]
        assert verifier_node_id == "reward-node-b"

        _submit_signed_attestation(
            service,
            epoch_index=0,
            candidate_node_id="reward-node-a",
            verifier_wallet=reward_b,
            verifier_node_id=verifier_node_id,
            window_index=window_index,
            endpoint_commitment="127.0.0.1:19001",
            concentration_key="demo:reward-node-a",
        )
        _mine_local_block(service, wallet_key(2).address)

        stored_bundles = service.native_reward_attestation_diagnostics(epoch_index=0)
        assert len(stored_bundles) == 1
        assert stored_bundles[0]["block_height"] == 1
        assert stored_bundles[0]["bundle_window_index"] == window_index
        assert stored_bundles[0]["attestations"][0]["verifier_node_id"] == "reward-node-b"

        preview = service.native_reward_settlement_preview(epoch_index=0)
        assert preview["rewarded_node_count"] == 1
        assert preview["reward_entries"][0]["node_id"] == "reward-node-a"
        assert preview["distributed_node_reward_chipbits"] == subsidy_split_chipbits(4, service.params)[1]
        built_once = service.build_native_reward_settlement_transaction(epoch_index=0, submission_mode="auto")
        built_twice = service.build_native_reward_settlement_transaction(epoch_index=0, submission_mode="auto")
        assert built_once.metadata == built_twice.metadata
        _mine_until_height(service, wallet_key(2).address, 3)
        closing_block = _mine_local_block(service, wallet_key(2).address)

        stored_settlements = service.native_reward_settlement_diagnostics(epoch_index=0)
        assert len(stored_settlements) == 1
        assert stored_settlements[0]["block_height"] == 4
        assert stored_settlements[0]["submission_mode"] == "auto"
        assert stored_settlements[0]["rewarded_node_count"] == 1
        assert stored_settlements[0]["reward_entries"][0]["node_id"] == "reward-node-a"
        inspect = service.inspect_block(block_hash=closing_block.block_hash())
        assert inspect is not None
        assert inspect["node_reward_payouts"] == [
            {"recipient": reward_a.address, "amount_chipbits": subsidy_split_chipbits(4, service.params)[1]}
        ]
        reward_rows = service.reward_history(reward_a.address, limit=10)
        assert any(row["amount_chipbits"] == subsidy_split_chipbits(4, service.params)[1] for row in reward_rows)


def test_native_reward_settlement_preview_returns_zero_recipients_when_no_candidate_qualifies() -> None:
    with TemporaryDirectory() as tempdir:
        service = _make_service(Path(tempdir) / "chipcoin.sqlite3")
        _register_reward_node(service, wallet=wallet_key(0), node_id="reward-node-a", port=19001)
        _register_reward_node(service, wallet=wallet_key(1), node_id="reward-node-b", port=19002)
        _mine_local_block(service, wallet_key(2).address)
        _mine_until_height(service, wallet_key(2).address, 3)

        preview = service.native_reward_settlement_preview(epoch_index=0)

        assert preview["rewarded_node_count"] == 0
        assert preview["reward_entries"] == []
        assert preview["distributed_node_reward_chipbits"] == 0
        assert preview["undistributed_node_reward_chipbits"] == subsidy_split_chipbits(4, service.params)[1]
        closing_block = _mine_local_block(service, wallet_key(2).address)
        stored_settlements = service.native_reward_settlement_diagnostics(epoch_index=0)
        assert len(stored_settlements) == 1
        assert stored_settlements[0]["submission_mode"] == "auto"
        assert stored_settlements[0]["reward_entries"] == []
        inspect = service.inspect_block(block_hash=closing_block.block_hash())
        assert inspect is not None
        assert inspect["node_reward_payouts"] == []
        supply = service.supply_snapshot()
        expected_miner_supply = service.params.epoch_length_blocks * subsidy_split_chipbits(0, service.params)[0]
        expected_node_pool = subsidy_split_chipbits(4, service.params)[1]
        assert supply["tip_hash"] == closing_block.block_hash()
        assert supply["scheduled_node_reward_supply_chipbits"] == expected_node_pool
        assert supply["materialized_miner_supply_chipbits"] == expected_miner_supply
        assert supply["materialized_node_reward_supply_chipbits"] == 0
        assert supply["materialized_supply_chipbits"] == expected_miner_supply
        assert supply["minted_supply_chipbits"] == expected_miner_supply
        assert supply["undistributed_node_reward_supply_chipbits"] == expected_node_pool


def test_native_reward_auto_settlement_materializes_multiple_reward_outputs() -> None:
    with TemporaryDirectory() as tempdir:
        service = _make_service(Path(tempdir) / "chipcoin.sqlite3")
        reward_a = wallet_key(0)
        reward_b = wallet_key(1)
        reward_c = wallet_key(2)

        for node_id, wallet, port in (
            ("reward-node-a", reward_a, 19001),
            ("reward-node-b", reward_b, 19002),
            ("reward-node-c", reward_c, 19003),
        ):
            _register_reward_node(service, wallet=wallet, node_id=node_id, port=port)
        _mine_local_block(service, reward_a.address)

        assignments_a = service.native_reward_assignments(epoch_index=0, node_id="reward-node-a")[0]
        window_a = assignments_a["candidate_check_windows"][0]
        verifier_a = assignments_a["verifier_committees"][str(window_a)][0]
        verifier_a_wallet = {"reward-node-a": reward_a, "reward-node-b": reward_b, "reward-node-c": reward_c}[verifier_a]
        _submit_signed_attestation(
            service,
            epoch_index=0,
            candidate_node_id="reward-node-a",
            verifier_wallet=verifier_a_wallet,
            verifier_node_id=verifier_a,
            window_index=window_a,
            endpoint_commitment="127.0.0.1:19001",
            concentration_key="demo:reward-node-a",
        )

        assignments_b = service.native_reward_assignments(epoch_index=0, node_id="reward-node-b")[0]
        window_b = assignments_b["candidate_check_windows"][0]
        verifier_b = assignments_b["verifier_committees"][str(window_b)][0]
        verifier_b_wallet = {"reward-node-a": reward_a, "reward-node-b": reward_b, "reward-node-c": reward_c}[verifier_b]
        _submit_signed_attestation(
            service,
            epoch_index=0,
            candidate_node_id="reward-node-b",
            verifier_wallet=verifier_b_wallet,
            verifier_node_id=verifier_b,
            window_index=window_b,
            endpoint_commitment="127.0.0.1:19002",
            concentration_key="demo:reward-node-b",
        )
        _mine_local_block(service, reward_a.address)

        _mine_until_height(service, reward_a.address, 3)

        preview = service.native_reward_settlement_preview(epoch_index=0)
        expected_pool = subsidy_split_chipbits(4, service.params)[1]
        assert preview["rewarded_node_count"] == 2
        assert preview["distributed_node_reward_chipbits"] == expected_pool
        assert preview["undistributed_node_reward_chipbits"] == 0
        assert (
            preview["distributed_node_reward_chipbits"] + preview["undistributed_node_reward_chipbits"] == expected_pool
        )
        assert [entry["node_id"] for entry in preview["reward_entries"]] == ["reward-node-a", "reward-node-b"]
        assert [entry["selection_rank"] for entry in preview["reward_entries"]] == [0, 1]
        amounts = [entry["reward_chipbits"] for entry in preview["reward_entries"]]
        assert amounts == [expected_pool // 2, expected_pool // 2]
        assert sum(amounts) == expected_pool
        assert preview["reward_split_summary"] == {
            "rewarded_node_count": 2,
            "scheduled_node_reward_chipbits": expected_pool,
            "distributed_node_reward_chipbits": expected_pool,
            "undistributed_node_reward_chipbits": 0,
            "base_reward_chipbits": expected_pool // 2,
            "remainder_chipbits": 0,
            "ordered_rewarded_node_ids": ["reward-node-a", "reward-node-b"],
            "ordered_payouts": [
                {
                    "selection_rank": 0,
                    "node_id": "reward-node-a",
                    "payout_address": reward_a.address,
                    "reward_chipbits": expected_pool // 2,
                },
                {
                    "selection_rank": 1,
                    "node_id": "reward-node-b",
                    "payout_address": reward_b.address,
                    "reward_chipbits": expected_pool // 2,
                },
            ],
        }

        report = service.native_reward_settlement_report(epoch_index=0)
        assert [entry["node_id"] for entry in report["eligible_ranking"]] == ["reward-node-a", "reward-node-b"]
        assert report["reward_entries"] == preview["reward_entries"]
        assert report["reward_split_summary"] == preview["reward_split_summary"]
        assert report["settlement_accounting_summary"] == {
            "scheduled_node_reward_chipbits": expected_pool,
            "distributed_node_reward_chipbits": expected_pool,
            "undistributed_node_reward_chipbits": 0,
        }

        closing_block = _mine_local_block(service, reward_a.address)
        stored_settlements = service.native_reward_settlement_diagnostics(epoch_index=0)
        assert len(stored_settlements) == 1
        assert stored_settlements[0]["submission_mode"] == "auto"
        assert [entry["node_id"] for entry in stored_settlements[0]["reward_entries"]] == ["reward-node-a", "reward-node-b"]
        assert [entry["reward_chipbits"] for entry in stored_settlements[0]["reward_entries"]] == [
            expected_pool // 2,
            expected_pool // 2,
        ]
        inspect = service.inspect_block(block_hash=closing_block.block_hash())
        assert inspect is not None
        assert inspect["node_reward_payouts"] == [
            {"recipient": preview["reward_entries"][0]["payout_address"], "amount_chipbits": preview["reward_entries"][0]["reward_chipbits"]},
            {"recipient": preview["reward_entries"][1]["payout_address"], "amount_chipbits": preview["reward_entries"][1]["reward_chipbits"]},
        ]
        assert inspect["node_reward_payouts"] == [
            {"recipient": payout["payout_address"], "amount_chipbits": payout["reward_chipbits"]}
            for payout in preview["reward_split_summary"]["ordered_payouts"]
        ]
        supply = service.supply_snapshot()
        expected_miner_supply = service.params.epoch_length_blocks * subsidy_split_chipbits(0, service.params)[0]
        assert supply["tip_hash"] == closing_block.block_hash()
        assert supply["scheduled_node_reward_supply_chipbits"] == expected_pool
        assert supply["materialized_miner_supply_chipbits"] == expected_miner_supply
        assert supply["materialized_node_reward_supply_chipbits"] == expected_pool
        assert supply["materialized_supply_chipbits"] == expected_miner_supply + expected_pool
        assert supply["minted_supply_chipbits"] == expected_miner_supply + expected_pool
        assert supply["undistributed_node_reward_supply_chipbits"] == 0


def test_native_reward_auto_settlement_materializes_three_reward_outputs_with_deterministic_split() -> None:
    with TemporaryDirectory() as tempdir:
        service = _make_service(Path(tempdir) / "chipcoin.sqlite3")
        reward_a = wallet_key(0)
        reward_b = wallet_key(1)
        reward_c = wallet_key(2)

        for node_id, wallet, port in (
            ("reward-node-a", reward_a, 19001),
            ("reward-node-b", reward_b, 19002),
            ("reward-node-c", reward_c, 19003),
        ):
            _register_reward_node(service, wallet=wallet, node_id=node_id, port=port)
        _mine_local_block(service, reward_a.address)

        wallet_map = _wallet_by_node_id(
            ("reward-node-a", reward_a),
            ("reward-node-b", reward_b),
            ("reward-node-c", reward_c),
        )
        endpoint_map = {
            "reward-node-a": "127.0.0.1:19001",
            "reward-node-b": "127.0.0.1:19002",
            "reward-node-c": "127.0.0.1:19003",
        }
        for node_id in ("reward-node-a", "reward-node-b", "reward-node-c"):
            assignment = service.native_reward_assignments(epoch_index=0, node_id=node_id)[0]
            window_index = assignment["candidate_check_windows"][0]
            verifier_node_id = assignment["verifier_committees"][str(window_index)][0]
            _submit_signed_attestation(
                service,
                epoch_index=0,
                candidate_node_id=node_id,
                verifier_wallet=wallet_map[verifier_node_id],
                verifier_node_id=verifier_node_id,
                window_index=window_index,
                endpoint_commitment=endpoint_map[node_id],
                concentration_key=f"demo:{node_id}",
            )
        _mine_local_block(service, reward_a.address)
        _mine_until_height(service, reward_a.address, 3)

        preview = service.native_reward_settlement_preview(epoch_index=0)
        expected_pool = subsidy_split_chipbits(4, service.params)[1]
        assert preview["rewarded_node_count"] == 3
        assert preview["distributed_node_reward_chipbits"] == expected_pool
        assert preview["undistributed_node_reward_chipbits"] == 0
        assert (
            preview["distributed_node_reward_chipbits"] + preview["undistributed_node_reward_chipbits"] == expected_pool
        )
        assert [entry["node_id"] for entry in preview["reward_entries"]] == [
            "reward-node-c",
            "reward-node-a",
            "reward-node-b",
        ]
        assert [entry["selection_rank"] for entry in preview["reward_entries"]] == [0, 1, 2]
        assert [entry["reward_chipbits"] for entry in preview["reward_entries"]] == [
            1_666_666_667,
            1_666_666_667,
            1_666_666_666,
        ]
        assert sum(entry["reward_chipbits"] for entry in preview["reward_entries"]) == expected_pool
        assert preview["reward_split_summary"] == {
            "rewarded_node_count": 3,
            "scheduled_node_reward_chipbits": expected_pool,
            "distributed_node_reward_chipbits": expected_pool,
            "undistributed_node_reward_chipbits": 0,
            "base_reward_chipbits": 1_666_666_666,
            "remainder_chipbits": 2,
            "ordered_rewarded_node_ids": ["reward-node-c", "reward-node-a", "reward-node-b"],
            "ordered_payouts": [
                {
                    "selection_rank": 0,
                    "node_id": "reward-node-c",
                    "payout_address": reward_c.address,
                    "reward_chipbits": 1_666_666_667,
                },
                {
                    "selection_rank": 1,
                    "node_id": "reward-node-a",
                    "payout_address": reward_a.address,
                    "reward_chipbits": 1_666_666_667,
                },
                {
                    "selection_rank": 2,
                    "node_id": "reward-node-b",
                    "payout_address": reward_b.address,
                    "reward_chipbits": 1_666_666_666,
                },
            ],
        }

        report = service.native_reward_settlement_report(epoch_index=0)
        assert [entry["node_id"] for entry in report["eligible_ranking"]] == [
            "reward-node-c",
            "reward-node-a",
            "reward-node-b",
        ]
        assert report["reward_entries"] == preview["reward_entries"]
        assert report["reward_split_summary"] == preview["reward_split_summary"]
        assert report["settlement_accounting_summary"] == {
            "scheduled_node_reward_chipbits": expected_pool,
            "distributed_node_reward_chipbits": expected_pool,
            "undistributed_node_reward_chipbits": 0,
        }

        closing_block = _mine_local_block(service, reward_a.address)
        stored_settlements = service.native_reward_settlement_diagnostics(epoch_index=0)
        assert len(stored_settlements) == 1
        assert stored_settlements[0]["submission_mode"] == "auto"
        assert [entry["node_id"] for entry in stored_settlements[0]["reward_entries"]] == [
            "reward-node-c",
            "reward-node-a",
            "reward-node-b",
        ]
        assert [entry["reward_chipbits"] for entry in stored_settlements[0]["reward_entries"]] == [
            1_666_666_667,
            1_666_666_667,
            1_666_666_666,
        ]
        inspect = service.inspect_block(block_hash=closing_block.block_hash())
        assert inspect is not None
        assert inspect["node_reward_payouts"] == [
            {"recipient": payout["payout_address"], "amount_chipbits": payout["reward_chipbits"]}
            for payout in preview["reward_split_summary"]["ordered_payouts"]
        ]


def test_native_reward_manual_settlement_takes_precedence_over_auto_generation() -> None:
    with TemporaryDirectory() as tempdir:
        service = _make_service(Path(tempdir) / "chipcoin.sqlite3")
        reward_a = wallet_key(0)
        reward_b = wallet_key(1)

        for node_id, wallet, port in (
            ("reward-node-a", reward_a, 19001),
            ("reward-node-b", reward_b, 19002),
        ):
            _register_reward_node(service, wallet=wallet, node_id=node_id, port=port)
        _mine_local_block(service, wallet_key(2).address)

        assignment = service.native_reward_assignments(epoch_index=0, node_id="reward-node-a")[0]
        window_index = assignment["candidate_check_windows"][0]
        verifier_node_id = assignment["verifier_committees"][str(window_index)][0]
        _submit_signed_attestation(
            service,
            epoch_index=0,
            candidate_node_id="reward-node-a",
            verifier_wallet=reward_b,
            verifier_node_id=verifier_node_id,
            window_index=window_index,
            endpoint_commitment="127.0.0.1:19001",
            concentration_key="demo:reward-node-a",
        )
        _mine_until_height(service, wallet_key(2).address, 3)

        manual_preview = service.native_reward_settlement_preview(epoch_index=0)
        assert manual_preview["rewarded_node_count"] == 1
        manual_tx = _build_settlement_transaction(manual_preview)
        service.receive_transaction(manual_tx)
        closing_block = _mine_local_block(service, wallet_key(2).address)

        settlement_transactions = [
            transaction
            for transaction in closing_block.transactions
            if transaction.metadata.get("kind") == "reward_settle_epoch"
        ]
        assert len(settlement_transactions) == 1
        assert settlement_transactions[0].txid() == manual_tx.txid()
        stored_settlements = service.native_reward_settlement_diagnostics(epoch_index=0)
        assert stored_settlements[0]["submission_mode"] == "manual"


def test_native_reward_auto_settlement_does_not_generate_second_settlement_after_epoch_close() -> None:
    with TemporaryDirectory() as tempdir:
        service = _make_service(Path(tempdir) / "chipcoin.sqlite3")
        _register_reward_node(service, wallet=wallet_key(0), node_id="reward-node-a", port=19001)
        _register_reward_node(service, wallet=wallet_key(1), node_id="reward-node-b", port=19002)
        _mine_local_block(service, wallet_key(2).address)
        _mine_until_height(service, wallet_key(2).address, 3)
        _mine_local_block(service, wallet_key(2).address)
        assert len(service.native_reward_settlement_diagnostics(epoch_index=0)) == 1
        next_block = _mine_local_block(service, wallet_key(2).address)
        settlement_transactions = [
            transaction
            for transaction in next_block.transactions
            if transaction.metadata.get("kind") == "reward_settle_epoch"
        ]
        assert settlement_transactions == []
        assert len(service.native_reward_settlement_diagnostics(epoch_index=0)) == 1


def test_native_reward_rebuilt_closing_block_is_deterministic_and_does_not_double_pay() -> None:
    with TemporaryDirectory() as tempdir:
        service = _make_service(Path(tempdir) / "chipcoin.sqlite3")
        reward_a = wallet_key(0)
        reward_b = wallet_key(1)
        _register_reward_node(service, wallet=reward_a, node_id="reward-node-a", port=19001)
        _register_reward_node(service, wallet=reward_b, node_id="reward-node-b", port=19002)
        _mine_local_block(service, wallet_key(2).address)
        assignment = service.native_reward_assignments(epoch_index=0, node_id="reward-node-a")[0]
        window_index = assignment["candidate_check_windows"][0]
        verifier_node_id = assignment["verifier_committees"][str(window_index)][0]
        _submit_signed_attestation(
            service,
            epoch_index=0,
            candidate_node_id="reward-node-a",
            verifier_wallet=reward_b,
            verifier_node_id=verifier_node_id,
            window_index=window_index,
            endpoint_commitment="127.0.0.1:19001",
            concentration_key="demo:reward-node-a",
        )
        _mine_local_block(service, wallet_key(2).address)
        _mine_until_height(service, wallet_key(2).address, 3)

        candidate_one = service.build_candidate_block(wallet_key(2).address).block
        candidate_two = service.build_candidate_block(wallet_key(2).address).block
        settlement_one = next(
            transaction for transaction in candidate_one.transactions if transaction.metadata.get("kind") == "reward_settle_epoch"
        )
        settlement_two = next(
            transaction for transaction in candidate_two.transactions if transaction.metadata.get("kind") == "reward_settle_epoch"
        )
        assert settlement_one.metadata == settlement_two.metadata
        assert candidate_one.transactions[0].outputs == candidate_two.transactions[0].outputs

        closing_block = _mine_local_block(service, wallet_key(2).address)
        inspect = service.inspect_block(block_hash=closing_block.block_hash())
        assert inspect is not None
        assert len(inspect["node_reward_payouts"]) == 1
        next_block = _mine_local_block(service, wallet_key(2).address)
        next_inspect = service.inspect_block(block_hash=next_block.block_hash())
        assert next_inspect is not None
        assert next_inspect["node_reward_payouts"] == []


def test_native_reward_invalid_manual_settlement_is_rejected_before_auto_close() -> None:
    with TemporaryDirectory() as tempdir:
        service = _make_service(Path(tempdir) / "chipcoin.sqlite3")
        _register_reward_node(service, wallet=wallet_key(0), node_id="reward-node-a", port=19001)
        _register_reward_node(service, wallet=wallet_key(1), node_id="reward-node-b", port=19002)
        _mine_local_block(service, wallet_key(2).address)
        _mine_until_height(service, wallet_key(2).address, 3)

        preview = service.native_reward_settlement_preview(epoch_index=0)
        invalid_tx = _build_settlement_transaction({**preview, "epoch_seed": "00" * 32})
        try:
            service.receive_transaction(invalid_tx)
        except ValidationError:
            pass
        else:
            raise AssertionError("Expected invalid manual settlement to be rejected.")

        closing_block = _mine_local_block(service, wallet_key(2).address)
        settlement_transactions = [
            parse_reward_settlement_metadata(transaction.metadata)
            for transaction in closing_block.transactions
            if transaction.metadata.get("kind") == "reward_settle_epoch"
        ]
        assert len(settlement_transactions) == 1
        assert settlement_transactions[0].submission_mode == "auto"


def test_native_reward_expired_node_is_not_rewarded_in_following_epoch() -> None:
    with TemporaryDirectory() as tempdir:
        service = _make_service(Path(tempdir) / "chipcoin.sqlite3")
        reward_a = wallet_key(0)
        reward_b = wallet_key(1)
        _register_reward_node(service, wallet=reward_a, node_id="reward-node-a", port=19001)
        _register_reward_node(service, wallet=reward_b, node_id="reward-node-b", port=19002)
        _mine_local_block(service, wallet_key(2).address)

        _qualify_reward_node_for_epoch(
            service,
            epoch_index=0,
            candidate_node_id="reward-node-a",
            verifier_wallets_by_node_id={"reward-node-a": reward_a, "reward-node-b": reward_b},
        )
        _mine_local_block(service, wallet_key(2).address)
        _mine_until_height(service, wallet_key(2).address, 4)
        _mine_local_block(service, wallet_key(2).address)

        assert service.native_reward_settlement_diagnostics(epoch_index=0)[0]["rewarded_node_count"] == 1
        epoch_one_preview = service.native_reward_settlement_preview(epoch_index=1)
        assert epoch_one_preview["rewarded_node_count"] == 0
        assert service.native_reward_assignments(epoch_index=1, node_id="reward-node-a") == []


def test_native_reward_boundary_semantics_for_warmup_renewal_and_expiry() -> None:
    with TemporaryDirectory() as tempdir:
        service = _make_boundary_service(Path(tempdir) / "chipcoin.sqlite3")
        reward_a = wallet_key(0)
        reward_b = wallet_key(1)
        miner = wallet_key(2)

        _register_reward_node(service, wallet=reward_a, node_id="reward-node-a", port=19001)
        _register_reward_node(service, wallet=reward_b, node_id="reward-node-b", port=19002)
        _mine_local_block(service, miner.address)

        registry_after_registration = {row["node_id"]: row for row in service.node_registry_diagnostics()}
        assert registry_after_registration["reward-node-a"]["warmup_complete"] is False
        assert registry_after_registration["reward-node-a"]["warmup_complete_epoch"] == 1
        assert registry_after_registration["reward-node-a"]["warmup_complete_height"] == 5
        assert registry_after_registration["reward-node-a"]["eligible_from_height"] == 5
        assert registry_after_registration["reward-node-a"]["eligibility_status"] == "warming_up"

        _mine_until_height(service, miner.address, 4)
        epoch_one_before_boundary = service.native_reward_epoch_state(epoch_index=1)
        assert epoch_one_before_boundary["tip_height"] == 4
        assert epoch_one_before_boundary["seed"]["evaluation_height"] == 5
        assert epoch_one_before_boundary["active_reward_node_count"] == 0
        assert epoch_one_before_boundary["assignments_count"] == 0

        _renew_reward_node(service, wallet=reward_a, node_id="reward-node-a", port=19001)
        _mine_local_block(service, miner.address)
        registry_at_boundary = {row["node_id"]: row for row in service.node_registry_diagnostics()}
        assert registry_at_boundary["reward-node-a"]["last_renewal_height"] == 5
        assert registry_at_boundary["reward-node-a"]["eligible_from_height"] == 6
        assert registry_at_boundary["reward-node-a"]["eligibility_status"] == "active"
        assert registry_at_boundary["reward-node-b"]["eligibility_status"] == "stale"

        epoch_one_after_boundary = service.native_reward_epoch_state(epoch_index=1)
        assert epoch_one_after_boundary["tip_height"] == 5
        assert [row["node_id"] for row in epoch_one_after_boundary["active_reward_nodes"]] == ["reward-node-a"]
        assert [row["node_id"] for row in epoch_one_after_boundary["assignments"]] == ["reward-node-a"]
        assert epoch_one_after_boundary["active_reward_nodes"][0]["eligible_from_height"] == 6

        _renew_reward_node(service, wallet=reward_b, node_id="reward-node-b", port=19002)
        _mine_local_block(service, miner.address)
        registry_one_block_after_boundary = {row["node_id"]: row for row in service.node_registry_diagnostics()}
        assert registry_one_block_after_boundary["reward-node-b"]["last_renewal_height"] == 6
        assert registry_one_block_after_boundary["reward-node-b"]["eligible_from_height"] == 7
        assert registry_one_block_after_boundary["reward-node-b"]["eligibility_status"] == "active"

        epoch_one_with_two_active = service.native_reward_epoch_state(epoch_index=1)
        assert [row["node_id"] for row in epoch_one_with_two_active["active_reward_nodes"]] == [
            "reward-node-a",
            "reward-node-b",
        ]
        assert [row["node_id"] for row in epoch_one_with_two_active["assignments"]] == [
            "reward-node-a",
            "reward-node-b",
        ]

        _mine_until_height(service, miner.address, 9)
        close_epoch_one_preview = service.native_reward_epoch_state(epoch_index=1)
        assert [row["node_id"] for row in close_epoch_one_preview["active_reward_nodes"]] == [
            "reward-node-a",
            "reward-node-b",
        ]

        epoch_two_preview = service.native_reward_epoch_state(epoch_index=2)
        assert epoch_two_preview["active_reward_node_count"] == 0
        assert epoch_two_preview["assignments_count"] == 0
        assert epoch_two_preview["active_reward_nodes"] == []


def test_native_reward_same_block_close_boundary_excludes_same_block_renewals() -> None:
    with TemporaryDirectory() as tempdir:
        service = _make_boundary_service(Path(tempdir) / "chipcoin.sqlite3")
        reward_a = wallet_key(0)
        reward_b = wallet_key(1)
        reward_c = wallet_key(2)
        miner = wallet_key(0)

        for node_id, wallet, port in (
            ("reward-node-a", reward_a, 19001),
            ("reward-node-b", reward_b, 19002),
            ("reward-node-c", reward_c, 19003),
        ):
            _register_reward_node(service, wallet=wallet, node_id=node_id, port=port)
        _mine_local_block(service, miner.address)

        _mine_until_height(service, miner.address, 4)
        _renew_reward_node(service, wallet=reward_a, node_id="reward-node-a", port=19001)
        _renew_reward_node(service, wallet=reward_b, node_id="reward-node-b", port=19002)
        _mine_local_block(service, miner.address)
        _mine_until_height(service, miner.address, 7)

        _qualify_reward_node_for_epoch(
            service,
            epoch_index=1,
            candidate_node_id="reward-node-a",
            verifier_wallets_by_node_id={
                "reward-node-a": reward_a,
                "reward-node-b": reward_b,
                "reward-node-c": reward_c,
            },
        )
        _mine_local_block(service, miner.address)
        _renew_reward_node(service, wallet=reward_c, node_id="reward-node-c", port=19003)
        close_block = _mine_local_block(service, miner.address)

        close_epoch_report = service.native_reward_settlement_report(epoch_index=1)
        assert [entry["node_id"] for entry in close_epoch_report["reward_entries"]] == ["reward-node-a"]
        node_evaluations = {row["node_id"]: row for row in close_epoch_report["node_evaluations"]}
        assert node_evaluations["reward-node-b"]["status"] == "not_rewarded"
        assert node_evaluations["reward-node-b"]["not_rewarded_reason"] == "insufficient_passed_windows"
        assert "reward-node-c" not in node_evaluations

        inspect = service.inspect_block(block_hash=close_block.block_hash())
        assert inspect is not None
        assert inspect["node_reward_payouts"] == [
            {"recipient": reward_a.address, "amount_chipbits": subsidy_split_chipbits(9, service.params)[1]}
        ]

        stored = service.native_reward_settlement_diagnostics(epoch_index=1)
        assert len(stored) == 1
        assert [entry["node_id"] for entry in stored[0]["reward_entries"]] == ["reward-node-a"]
        history = [entry for entry in service.reward_history(reward_a.address, limit=50, descending=False) if entry["reward_type"] == "node_reward"]
        assert [entry["block_hash"] for entry in history] == [close_block.block_hash()]

        epoch_two_state = service.native_reward_epoch_state(epoch_index=2)
        assert epoch_two_state["active_reward_nodes"] == []
        assert epoch_two_state["assignments"] == []
        registry_after_close = {row["node_id"]: row for row in service.node_registry_diagnostics()}
        assert registry_after_close["reward-node-c"]["eligibility_status"] == "stale"


def test_native_reward_boundary_competition_stays_consistent_across_surfaces() -> None:
    with TemporaryDirectory() as tempdir:
        service = _make_boundary_service(Path(tempdir) / "chipcoin.sqlite3")
        reward_a = wallet_key(0)
        reward_b = wallet_key(1)
        reward_c = wallet_key(2)
        miner = wallet_key(0)

        for node_id, wallet, port in (
            ("reward-node-a", reward_a, 19001),
            ("reward-node-b", reward_b, 19002),
            ("reward-node-c", reward_c, 19003),
        ):
            _register_reward_node(service, wallet=wallet, node_id=node_id, port=port)
        _mine_local_block(service, miner.address)

        _mine_until_height(service, miner.address, 19)
        _renew_reward_node(service, wallet=reward_a, node_id="reward-node-a", port=19001)
        _renew_reward_node(service, wallet=reward_b, node_id="reward-node-b", port=19002)
        _mine_local_block(service, miner.address)
        _mine_until_height(service, miner.address, 22)

        epoch_four_state = service.native_reward_epoch_state(epoch_index=4)
        assert [row["node_id"] for row in epoch_four_state["active_reward_nodes"]] == [
            "reward-node-a",
            "reward-node-b",
        ]
        assert [row["node_id"] for row in epoch_four_state["assignments"]] == [
            "reward-node-a",
            "reward-node-b",
        ]
        assert service.native_reward_assignments(epoch_index=4, node_id="reward-node-c") == []

        wallet_map = {
            "reward-node-a": reward_a,
            "reward-node-b": reward_b,
            "reward-node-c": reward_c,
        }
        for node_id in ("reward-node-a", "reward-node-b"):
            _qualify_reward_node_for_epoch(
                service,
                epoch_index=4,
                candidate_node_id=node_id,
                verifier_wallets_by_node_id=wallet_map,
            )
        _mine_local_block(service, miner.address)
        close_block = _mine_local_block(service, miner.address)

        report = service.native_reward_settlement_report(epoch_index=4)
        expected_pool = subsidy_split_chipbits(24, service.params)[1]
        expected_reward_entries = sorted(report["reward_entries"], key=lambda entry: entry["selection_rank"])
        assert {entry["node_id"] for entry in report["eligible_ranking"]} == {"reward-node-a", "reward-node-b"}
        assert {entry["node_id"] for entry in report["reward_entries"]} == {"reward-node-a", "reward-node-b"}
        assert [entry["selection_rank"] for entry in report["reward_entries"]] == [0, 1]
        assert [entry["reward_chipbits"] for entry in report["reward_entries"]] == [
            expected_pool // 2,
            expected_pool // 2,
        ]
        assert report["distributed_node_reward_chipbits"] == expected_pool
        assert report["undistributed_node_reward_chipbits"] == 0
        assert report["settlement_accounting_summary"] == {
            "scheduled_node_reward_chipbits": expected_pool,
            "distributed_node_reward_chipbits": expected_pool,
            "undistributed_node_reward_chipbits": 0,
        }

        epoch_state_after_close = service.native_reward_epoch_state(epoch_index=4)
        assert epoch_state_after_close["comparison_keys"]["settlement_preview_digest"] is not None
        assert epoch_state_after_close["stored_settlement_count"] == 1
        assert epoch_state_after_close["attestation_bundle_count"] == 2
        assert [row["node_id"] for row in epoch_state_after_close["active_reward_nodes"]] == [
            "reward-node-a",
            "reward-node-b",
        ]

        inspect = service.inspect_block(block_hash=close_block.block_hash())
        assert inspect is not None
        assert inspect["node_reward_payouts"] == [
            {"recipient": entry["payout_address"], "amount_chipbits": entry["reward_chipbits"]}
            for entry in expected_reward_entries
        ]

        settlements = service.native_reward_settlement_diagnostics(epoch_index=4)
        assert len(settlements) == 1
        assert settlements[0]["reward_entries"] == expected_reward_entries
        assert [entry["reward_chipbits"] for entry in settlements[0]["reward_entries"]] == [
            expected_pool // 2,
            expected_pool // 2,
        ]

        history_a = [entry for entry in service.reward_history(reward_a.address, limit=50, descending=False) if entry["reward_type"] == "node_reward"]
        history_b = [entry for entry in service.reward_history(reward_b.address, limit=50, descending=False) if entry["reward_type"] == "node_reward"]
        history_c = [entry for entry in service.reward_history(reward_c.address, limit=50, descending=False) if entry["reward_type"] == "node_reward"]
        assert [entry["block_hash"] for entry in history_a] == [close_block.block_hash()]
        assert [entry["block_hash"] for entry in history_b] == [close_block.block_hash()]
        assert history_c == []


def test_native_reward_snapshot_restore_mid_cycle_preserves_auto_settlement_path() -> None:
    with TemporaryDirectory() as tempdir:
        source_path = Path(tempdir) / "source.sqlite3"
        target_path = Path(tempdir) / "target.sqlite3"
        snapshot_path = Path(tempdir) / "midcycle.snapshot"
        source = _make_service(source_path)
        reward_a = wallet_key(0)
        reward_b = wallet_key(1)
        _register_reward_node(source, wallet=reward_a, node_id="reward-node-a", port=19001)
        _register_reward_node(source, wallet=reward_b, node_id="reward-node-b", port=19002)
        _mine_local_block(source, wallet_key(2).address)
        _qualify_reward_node_for_epoch(
            source,
            epoch_index=0,
            candidate_node_id="reward-node-a",
            verifier_wallets_by_node_id={"reward-node-a": reward_a, "reward-node-b": reward_b},
        )
        _mine_local_block(source, wallet_key(2).address)
        source.export_snapshot_file(snapshot_path)

        target = _make_service(target_path, start_time=1_700_001_000)
        target.import_snapshot_file(snapshot_path)
        _mine_until_height(target, wallet_key(2).address, 3)
        closing_block = _mine_local_block(target, wallet_key(2).address)
        inspect = target.inspect_block(block_hash=closing_block.block_hash())
        assert inspect is not None
        assert inspect["node_reward_payouts"] == [
            {"recipient": reward_a.address, "amount_chipbits": subsidy_split_chipbits(4, target.params)[1]}
        ]
        stored = target.native_reward_settlement_diagnostics(epoch_index=0)
        assert len(stored) == 1
        assert stored[0]["submission_mode"] == "auto"


def test_native_reward_multi_epoch_consecutive_auto_settlement_operation() -> None:
    with TemporaryDirectory() as tempdir:
        service = _make_service(Path(tempdir) / "chipcoin.sqlite3")
        reward_a = wallet_key(0)
        reward_b = wallet_key(1)
        verifier_wallets = {"reward-node-a": reward_a, "reward-node-b": reward_b}
        _register_reward_node(service, wallet=reward_a, node_id="reward-node-a", port=19001)
        _register_reward_node(service, wallet=reward_b, node_id="reward-node-b", port=19002)
        _mine_local_block(service, wallet_key(2).address)

        _qualify_reward_node_for_epoch(
            service,
            epoch_index=0,
            candidate_node_id="reward-node-a",
            verifier_wallets_by_node_id=verifier_wallets,
        )
        _mine_local_block(service, wallet_key(2).address)
        _mine_until_height(service, wallet_key(2).address, 4)
        _mine_local_block(service, wallet_key(2).address)
        assert service.native_reward_settlement_diagnostics(epoch_index=0)[0]["rewarded_node_count"] == 1

        service.receive_transaction(
            TransactionSigner(reward_a).build_renew_reward_node_transaction(
                node_id="reward-node-a",
                renewal_epoch=service.next_block_epoch(),
                declared_host="127.0.0.1",
                declared_port=19001,
                renewal_fee_chipbits=int(service.reward_node_fee_schedule()["renew_fee_chipbits"]),
            )
        )
        service.receive_transaction(
            TransactionSigner(reward_b).build_renew_reward_node_transaction(
                node_id="reward-node-b",
                renewal_epoch=service.next_block_epoch(),
                declared_host="127.0.0.1",
                declared_port=19002,
                renewal_fee_chipbits=int(service.reward_node_fee_schedule()["renew_fee_chipbits"]),
            )
        )
        _mine_local_block(service, wallet_key(2).address)
        _qualify_reward_node_for_epoch(
            service,
            epoch_index=1,
            candidate_node_id="reward-node-a",
            verifier_wallets_by_node_id=verifier_wallets,
        )
        _mine_local_block(service, wallet_key(2).address)
        _mine_until_height(service, wallet_key(2).address, 8)
        second_closing = _mine_local_block(service, wallet_key(2).address)

        settlements = service.native_reward_settlement_diagnostics()
        assert [row["epoch_index"] for row in settlements] == [0, 1]
        assert all(row["submission_mode"] == "auto" for row in settlements)
        inspect = service.inspect_block(block_hash=second_closing.block_hash())
        assert inspect is not None
        assert inspect["node_reward_payouts"] == [
            {"recipient": reward_a.address, "amount_chipbits": subsidy_split_chipbits(9, service.params)[1]}
        ]


def test_native_reward_attestation_bundles_disconnect_and_reconnect_across_reorg() -> None:
    with TemporaryDirectory() as tempdir:
        base_path = Path(tempdir) / "base.sqlite3"
        branch_a_path = Path(tempdir) / "branch-a.sqlite3"
        branch_b_path = Path(tempdir) / "branch-b.sqlite3"
        target_path = Path(tempdir) / "target.sqlite3"
        base = _make_service(base_path)
        reward_a = wallet_key(0)
        reward_b = wallet_key(1)
        miner = wallet_key(2)

        _register_reward_node(base, wallet=reward_a, node_id="reward-node-a", port=19001)
        _register_reward_node(base, wallet=reward_b, node_id="reward-node-b", port=19002)
        _mine_local_block(base, miner.address)

        branch_a = _clone_service(base, branch_a_path, start_time=1_700_001_000)
        branch_b = _clone_service(base, branch_b_path, start_time=1_700_002_000)
        target = _make_service(target_path, start_time=1_700_003_000)
        sync = SyncManager(node=target)

        assignment = branch_a.native_reward_assignments(epoch_index=0, node_id="reward-node-a")[0]
        window_index = assignment["candidate_check_windows"][0]
        verifier_node_id = assignment["verifier_committees"][str(window_index)][0]
        _submit_signed_attestation(
            branch_a,
            epoch_index=0,
            candidate_node_id="reward-node-a",
            verifier_wallet=reward_b,
            verifier_node_id=verifier_node_id,
            window_index=window_index,
            endpoint_commitment="127.0.0.1:19001",
            concentration_key="demo:reward-node-a",
        )
        attestation_block = _mine_local_block(branch_a, miner.address)
        expected_bundle = branch_a.native_reward_attestation_diagnostics(epoch_index=0)[0]

        first = sync.synchronize(branch_a)
        assert first.reorged is False
        bundles = target.native_reward_attestation_diagnostics(epoch_index=0)
        assert len(bundles) == 1
        assert bundles[0]["txid"] == expected_bundle["txid"]
        assert bundles[0]["block_height"] == 1
        assert bundles[0]["bundle_block_hash"] == attestation_block.block_hash()
        assert bundles[0]["reward_state_anchor"]["tip_hash"] == branch_a.chain_tip().block_hash
        assert bundles[0]["reward_state_anchor"]["previous_epoch_close_hash"] == "00" * 32

        _mine_local_block(branch_b, miner.address)
        _mine_local_block(branch_b, miner.address)
        second = sync.synchronize(branch_b)
        assert second.reorged is True
        assert second.reorg_depth == 1
        assert target.native_reward_attestation_diagnostics(epoch_index=0) == []
        epoch_state = target.native_reward_epoch_state(epoch_index=0)
        assert epoch_state["attestation_bundle_count"] == 0
        assert epoch_state["reward_state_anchor"]["tip_hash"] == branch_b.chain_tip().block_hash

        _mine_local_block(branch_a, miner.address)
        _mine_local_block(branch_a, miner.address)
        third = sync.synchronize(branch_a)
        assert third.reorged is True
        restored = target.native_reward_attestation_diagnostics(epoch_index=0)
        assert len(restored) == 1
        assert restored[0]["txid"] == expected_bundle["txid"]
        assert restored[0]["bundle_block_hash"] == attestation_block.block_hash()
        assert restored[0]["reward_state_anchor"]["tip_hash"] == branch_a.chain_tip().block_hash


def test_native_reward_settlement_and_payouts_follow_active_branch_across_reorg() -> None:
    with TemporaryDirectory() as tempdir:
        base_path = Path(tempdir) / "base.sqlite3"
        rewarded_path = Path(tempdir) / "rewarded.sqlite3"
        unrewarded_path = Path(tempdir) / "unrewarded.sqlite3"
        target_path = Path(tempdir) / "target.sqlite3"
        base = _make_service(base_path)
        reward_a = wallet_key(0)
        reward_b = wallet_key(1)
        miner = wallet_key(2)

        _register_reward_node(base, wallet=reward_a, node_id="reward-node-a", port=19001)
        _register_reward_node(base, wallet=reward_b, node_id="reward-node-b", port=19002)
        _mine_local_block(base, miner.address)

        rewarded_branch = _clone_service(base, rewarded_path, start_time=1_700_001_000)
        unrewarded_branch = _clone_service(base, unrewarded_path, start_time=1_700_002_000)
        target = _make_service(target_path, start_time=1_700_003_000)
        sync = SyncManager(node=target)

        assignment = rewarded_branch.native_reward_assignments(epoch_index=0, node_id="reward-node-a")[0]
        window_index = assignment["candidate_check_windows"][0]
        verifier_node_id = assignment["verifier_committees"][str(window_index)][0]
        _submit_signed_attestation(
            rewarded_branch,
            epoch_index=0,
            candidate_node_id="reward-node-a",
            verifier_wallet=reward_b,
            verifier_node_id=verifier_node_id,
            window_index=window_index,
            endpoint_commitment="127.0.0.1:19001",
            concentration_key="demo:reward-node-a",
        )
        _mine_local_block(rewarded_branch, miner.address)
        _mine_until_height(rewarded_branch, miner.address, 3)
        rewarded_close = _mine_local_block(rewarded_branch, miner.address)
        rewarded_settlement = rewarded_branch.native_reward_settlement_diagnostics(epoch_index=0)[0]

        _mine_until_height(unrewarded_branch, miner.address, 3)
        unrewarded_close = _mine_local_block(unrewarded_branch, miner.address)
        _mine_local_block(unrewarded_branch, miner.address)
        unrewarded_settlement = unrewarded_branch.native_reward_settlement_diagnostics(epoch_index=0)[0]

        first = sync.synchronize(rewarded_branch)
        assert first.reorged is False
        settlements = target.native_reward_settlement_diagnostics(epoch_index=0)
        assert len(settlements) == 1
        assert settlements[0]["txid"] == rewarded_settlement["txid"]
        assert settlements[0]["settlement_block_hash"] == rewarded_close.block_hash()
        assert [entry["node_id"] for entry in settlements[0]["reward_entries"]] == ["reward-node-a"]
        assert target.inspect_block(height=4)["block_hash"] == rewarded_close.block_hash()
        assert target.inspect_block(height=4)["node_reward_payouts"] == [
            {"recipient": reward_a.address, "amount_chipbits": subsidy_split_chipbits(4, target.params)[1]}
        ]
        rewarded_supply = target.supply_snapshot()
        assert rewarded_supply["tip_hash"] == rewarded_branch.chain_tip().block_hash
        assert rewarded_supply["materialized_node_reward_supply_chipbits"] == subsidy_split_chipbits(4, target.params)[1]
        assert rewarded_supply["undistributed_node_reward_supply_chipbits"] == 0
        assert [
            entry["block_hash"]
            for entry in target.reward_history(reward_a.address, limit=10_000, descending=False)
            if entry["reward_type"] == "node_reward"
        ] == [rewarded_close.block_hash()]

        second = sync.synchronize(unrewarded_branch)
        assert second.reorged is True
        settlements = target.native_reward_settlement_diagnostics(epoch_index=0)
        assert len(settlements) == 1
        assert settlements[0]["txid"] == unrewarded_settlement["txid"]
        assert settlements[0]["settlement_block_hash"] == unrewarded_close.block_hash()
        assert settlements[0]["reward_entries"] == []
        assert target.inspect_block(height=4)["block_hash"] == unrewarded_close.block_hash()
        assert target.inspect_block(height=4)["node_reward_payouts"] == []
        unrewarded_supply = target.supply_snapshot()
        assert unrewarded_supply["tip_hash"] == unrewarded_branch.chain_tip().block_hash
        assert unrewarded_supply["materialized_node_reward_supply_chipbits"] == 0
        assert unrewarded_supply["undistributed_node_reward_supply_chipbits"] == subsidy_split_chipbits(4, target.params)[1]
        assert [
            entry
            for entry in target.reward_history(reward_a.address, limit=10_000, descending=False)
            if entry["reward_type"] == "node_reward"
        ] == []

        _mine_local_block(rewarded_branch, miner.address)
        _mine_local_block(rewarded_branch, miner.address)
        third = sync.synchronize(rewarded_branch)
        assert third.reorged is True
        settlements = target.native_reward_settlement_diagnostics(epoch_index=0)
        assert len(settlements) == 1
        assert settlements[0]["txid"] == rewarded_settlement["txid"]
        assert settlements[0]["settlement_block_hash"] == rewarded_close.block_hash()
        assert [entry["node_id"] for entry in settlements[0]["reward_entries"]] == ["reward-node-a"]
        assert target.inspect_block(height=4)["block_hash"] == rewarded_close.block_hash()
        assert target.inspect_block(height=4)["node_reward_payouts"] == [
            {"recipient": reward_a.address, "amount_chipbits": subsidy_split_chipbits(4, target.params)[1]}
        ]
        restored_supply = target.supply_snapshot()
        assert restored_supply["tip_hash"] == rewarded_branch.chain_tip().block_hash
        assert restored_supply["materialized_node_reward_supply_chipbits"] == subsidy_split_chipbits(4, target.params)[1]
        assert restored_supply["undistributed_node_reward_supply_chipbits"] == 0
        assert len(
            [
                entry
                for entry in target.reward_history(reward_a.address, limit=10_000, descending=False)
                if entry["reward_type"] == "node_reward"
            ]
        ) == 1


def test_native_reward_reorg_clears_or_restores_renewal_driven_eligibility() -> None:
    with TemporaryDirectory() as tempdir:
        base_path = Path(tempdir) / "base.sqlite3"
        renewed_path = Path(tempdir) / "renewed.sqlite3"
        stale_path = Path(tempdir) / "stale.sqlite3"
        target_path = Path(tempdir) / "target.sqlite3"
        base = _make_service(base_path)
        reward_a = wallet_key(0)
        reward_b = wallet_key(1)
        miner = wallet_key(2)

        _register_reward_node(base, wallet=reward_a, node_id="reward-node-a", port=19001)
        _register_reward_node(base, wallet=reward_b, node_id="reward-node-b", port=19002)
        _mine_local_block(base, miner.address)
        _mine_until_height(base, miner.address, 4)

        renewed_branch = _clone_service(base, renewed_path, start_time=1_700_001_000)
        stale_branch = _clone_service(base, stale_path, start_time=1_700_002_000)
        target = _make_service(target_path, start_time=1_700_003_000)
        sync = SyncManager(node=target)

        for wallet, node_id, port in (
            (reward_a, "reward-node-a", 19001),
            (reward_b, "reward-node-b", 19002),
        ):
                renewed_branch.receive_transaction(
                    TransactionSigner(wallet).build_renew_reward_node_transaction(
                        node_id=node_id,
                        renewal_epoch=renewed_branch.next_block_epoch(),
                        declared_host="127.0.0.1",
                        declared_port=port,
                        renewal_fee_chipbits=int(renewed_branch.reward_node_fee_schedule()["renew_fee_chipbits"]),
                    )
                )
        _mine_local_block(renewed_branch, miner.address)

        first = sync.synchronize(renewed_branch)
        assert first.reorged is False
        renewed_state = target.native_reward_epoch_state(epoch_index=1)
        assert renewed_state["active_reward_node_count"] == 2
        assert renewed_state["assignments_count"] == 2
        assert [row["node_id"] for row in renewed_state["active_reward_nodes"]] == ["reward-node-a", "reward-node-b"]

        _mine_local_block(stale_branch, miner.address)
        _mine_local_block(stale_branch, miner.address)
        second = sync.synchronize(stale_branch)
        assert second.reorged is True
        stale_state = target.native_reward_epoch_state(epoch_index=1)
        assert stale_state["active_reward_node_count"] == 0
        assert stale_state["assignments_count"] == 0
        assert stale_state["active_reward_nodes"] == []
        assert stale_state["assignments"] == []

        _mine_local_block(renewed_branch, miner.address)
        _mine_local_block(renewed_branch, miner.address)
        third = sync.synchronize(renewed_branch)
        assert third.reorged is True
        restored_state = target.native_reward_epoch_state(epoch_index=1)
        assert restored_state["active_reward_node_count"] == 2
        assert restored_state["assignments_count"] == 2
        assert [row["node_id"] for row in restored_state["active_reward_nodes"]] == ["reward-node-a", "reward-node-b"]


def test_native_reward_restart_before_epoch_close_preserves_pending_attestations_and_eligibility() -> None:
    with TemporaryDirectory() as tempdir:
        db_path = Path(tempdir) / "node.sqlite3"
        service = _make_service(db_path)
        reward_a = wallet_key(0)
        reward_b = wallet_key(1)
        miner = wallet_key(2)

        _register_reward_node(service, wallet=reward_a, node_id="reward-node-a", port=19001)
        _register_reward_node(service, wallet=reward_b, node_id="reward-node-b", port=19002)
        _mine_local_block(service, miner.address)
        assignment = service.native_reward_assignments(epoch_index=0, node_id="reward-node-a")[0]
        window_index = assignment["candidate_check_windows"][0]
        verifier_node_id = assignment["verifier_committees"][str(window_index)][0]
        _submit_signed_attestation(
            service,
            epoch_index=0,
            candidate_node_id="reward-node-a",
            verifier_wallet=reward_b,
            verifier_node_id=verifier_node_id,
            window_index=window_index,
            endpoint_commitment="127.0.0.1:19001",
            concentration_key="demo:reward-node-a",
        )
        attestation_block = _mine_local_block(service, miner.address)
        expected_attestations = service.native_reward_attestation_diagnostics(epoch_index=0)
        expected_epoch_state = service.native_reward_epoch_state(epoch_index=0)
        assert expected_epoch_state["stored_settlement_count"] == 0

        restarted = _reopen_service(db_path, start_time=1_700_004_000)

        assert restarted.chain_tip() is not None
        assert restarted.chain_tip().block_hash == attestation_block.block_hash()
        assert restarted.native_reward_attestation_diagnostics(epoch_index=0) == expected_attestations
        assert restarted.native_reward_settlement_diagnostics(epoch_index=0) == []
        restarted_state = restarted.native_reward_epoch_state(epoch_index=0)
        assert restarted_state["comparison_keys"] == expected_epoch_state["comparison_keys"]
        assert restarted_state["active_reward_nodes"] == expected_epoch_state["active_reward_nodes"]
        assert restarted_state["assignments"] == expected_epoch_state["assignments"]
        assert restarted.reward_history(reward_a.address, limit=100, descending=False) == []


def test_native_reward_restart_after_epoch_close_preserves_settlement_history_and_prevents_duplicates() -> None:
    with TemporaryDirectory() as tempdir:
        db_path = Path(tempdir) / "node.sqlite3"
        service = _make_service(db_path)
        reward_a = wallet_key(0)
        reward_b = wallet_key(1)
        miner = wallet_key(2)

        _register_reward_node(service, wallet=reward_a, node_id="reward-node-a", port=19001)
        _register_reward_node(service, wallet=reward_b, node_id="reward-node-b", port=19002)
        _mine_local_block(service, miner.address)
        assignment = service.native_reward_assignments(epoch_index=0, node_id="reward-node-a")[0]
        window_index = assignment["candidate_check_windows"][0]
        verifier_node_id = assignment["verifier_committees"][str(window_index)][0]
        _submit_signed_attestation(
            service,
            epoch_index=0,
            candidate_node_id="reward-node-a",
            verifier_wallet=reward_b,
            verifier_node_id=verifier_node_id,
            window_index=window_index,
            endpoint_commitment="127.0.0.1:19001",
            concentration_key="demo:reward-node-a",
        )
        _mine_local_block(service, miner.address)
        _mine_until_height(service, miner.address, 3)
        closing_block = _mine_local_block(service, miner.address)
        next_block = _mine_local_block(service, miner.address)
        assert [
            transaction
            for transaction in next_block.transactions
            if transaction.metadata.get("kind") == "reward_settle_epoch"
        ] == []
        expected_settlements = service.native_reward_settlement_diagnostics(epoch_index=0)
        expected_history = service.reward_history(reward_a.address, limit=100, descending=False)
        expected_state = service.native_reward_epoch_state(epoch_index=0)
        expected_report = service.native_reward_settlement_report(epoch_index=0)

        restarted = _reopen_service(db_path, start_time=1_700_005_000)

        assert restarted.chain_tip() is not None
        assert restarted.native_reward_settlement_diagnostics(epoch_index=0) == expected_settlements
        assert restarted.native_reward_epoch_state(epoch_index=0)["comparison_keys"] == expected_state["comparison_keys"]
        assert restarted.native_reward_settlement_report(epoch_index=0)["reward_entries"] == expected_report["reward_entries"]
        assert restarted.reward_history(reward_a.address, limit=100, descending=False) == expected_history
        assert restarted.inspect_block(block_hash=closing_block.block_hash())["node_reward_payouts"] == [
            {"recipient": reward_a.address, "amount_chipbits": subsidy_split_chipbits(4, restarted.params)[1]}
        ]
        assert len(restarted.native_reward_settlement_diagnostics(epoch_index=0)) == 1
        later_block = _mine_local_block(restarted, miner.address)
        assert [
            transaction
            for transaction in later_block.transactions
            if transaction.metadata.get("kind") == "reward_settle_epoch"
        ] == []
        assert len(restarted.native_reward_settlement_diagnostics(epoch_index=0)) == 1


def test_native_reward_restart_during_sync_catchup_rebuilds_reward_state_correctly() -> None:
    with TemporaryDirectory() as tempdir:
        source_path = Path(tempdir) / "source.sqlite3"
        target_path = Path(tempdir) / "target.sqlite3"
        source = _make_service(source_path)
        reward_a = wallet_key(0)
        reward_b = wallet_key(1)
        miner = wallet_key(2)

        _register_reward_node(source, wallet=reward_a, node_id="reward-node-a", port=19001)
        _register_reward_node(source, wallet=reward_b, node_id="reward-node-b", port=19002)
        registration_block = _mine_local_block(source, miner.address)
        assignment = source.native_reward_assignments(epoch_index=0, node_id="reward-node-a")[0]
        window_index = assignment["candidate_check_windows"][0]
        verifier_node_id = assignment["verifier_committees"][str(window_index)][0]
        _submit_signed_attestation(
            source,
            epoch_index=0,
            candidate_node_id="reward-node-a",
            verifier_wallet=reward_b,
            verifier_node_id=verifier_node_id,
            window_index=window_index,
            endpoint_commitment="127.0.0.1:19001",
            concentration_key="demo:reward-node-a",
        )
        attestation_block = _mine_local_block(source, miner.address)
        partial_peer = _clone_service(source, Path(tempdir) / "partial.sqlite3", start_time=1_700_007_000)
        _mine_until_height(source, miner.address, 3)
        closing_block = _mine_local_block(source, miner.address)
        expected_state = source.native_reward_epoch_state(epoch_index=0)
        expected_settlements = source.native_reward_settlement_diagnostics(epoch_index=0)
        expected_history = source.reward_history(reward_a.address, limit=100, descending=False)

        target = _make_service(target_path, start_time=1_700_006_000)
        SyncManager(node=target).synchronize(partial_peer)
        assert target.chain_tip() is not None
        assert target.chain_tip().block_hash == attestation_block.block_hash()
        assert target.native_reward_settlement_diagnostics(epoch_index=0) == []
        assert target.get_block_by_hash(registration_block.block_hash()) is not None

        restarted = _reopen_service(target_path, start_time=1_700_008_000)
        result = SyncManager(node=restarted).synchronize(source)
        assert result.activated_tip == closing_block.block_hash()
        assert restarted.native_reward_epoch_state(epoch_index=0)["comparison_keys"] == expected_state["comparison_keys"]
        assert restarted.native_reward_settlement_diagnostics(epoch_index=0) == expected_settlements
        assert restarted.reward_history(reward_a.address, limit=100, descending=False) == expected_history


def test_native_reward_repeated_restart_cycles_across_epochs_remain_consistent() -> None:
    with TemporaryDirectory() as tempdir:
        db_path = Path(tempdir) / "node.sqlite3"
        reward_a = wallet_key(0)
        reward_b = wallet_key(1)
        miner = wallet_key(2)

        service = _make_service(db_path)
        _register_reward_node(service, wallet=reward_a, node_id="reward-node-a", port=19001)
        _register_reward_node(service, wallet=reward_b, node_id="reward-node-b", port=19002)
        _mine_local_block(service, miner.address)

        _qualify_reward_node_for_epoch(
            service,
            epoch_index=0,
            candidate_node_id="reward-node-a",
            verifier_wallets_by_node_id={"reward-node-a": reward_a, "reward-node-b": reward_b},
        )
        _mine_local_block(service, miner.address)
        service = _reopen_service(db_path, start_time=1_700_009_000)
        _mine_until_height(service, miner.address, 3)
        first_close = _mine_local_block(service, miner.address)
        service = _reopen_service(db_path, start_time=1_700_010_000)

        service.receive_transaction(
            TransactionSigner(reward_a).build_renew_reward_node_transaction(
                node_id="reward-node-a",
                renewal_epoch=service.next_block_epoch(),
                declared_host="127.0.0.1",
                declared_port=19001,
                renewal_fee_chipbits=int(service.reward_node_fee_schedule()["renew_fee_chipbits"]),
            )
        )
        service.receive_transaction(
            TransactionSigner(reward_b).build_renew_reward_node_transaction(
                node_id="reward-node-b",
                renewal_epoch=service.next_block_epoch(),
                declared_host="127.0.0.1",
                declared_port=19002,
                renewal_fee_chipbits=int(service.reward_node_fee_schedule()["renew_fee_chipbits"]),
            )
        )
        _mine_local_block(service, miner.address)
        service = _reopen_service(db_path, start_time=1_700_011_000)
        _qualify_reward_node_for_epoch(
            service,
            epoch_index=1,
            candidate_node_id="reward-node-a",
            verifier_wallets_by_node_id={"reward-node-a": reward_a, "reward-node-b": reward_b},
        )
        _mine_local_block(service, miner.address)
        service = _reopen_service(db_path, start_time=1_700_012_000)
        _mine_until_height(service, miner.address, 8)
        second_close = _mine_local_block(service, miner.address)
        service = _reopen_service(db_path, start_time=1_700_013_000)

        settlements = service.native_reward_settlement_diagnostics()
        assert [row["epoch_index"] for row in settlements] == [0, 1]
        assert len(settlements) == 2
        assert service.native_reward_epoch_state(epoch_index=0)["stored_settlement_count"] == 1
        assert service.native_reward_epoch_state(epoch_index=1)["stored_settlement_count"] == 1
        history = [entry for entry in service.reward_history(reward_a.address, limit=100, descending=False) if entry["reward_type"] == "node_reward"]
        assert [entry["block_hash"] for entry in history] == [first_close.block_hash(), second_close.block_hash()]
        assert service.inspect_block(block_hash=first_close.block_hash())["node_reward_payouts"] != []
        assert service.inspect_block(block_hash=second_close.block_hash())["node_reward_payouts"] != []


def test_native_reward_snapshot_import_midcycle_preserves_pending_reward_state() -> None:
    with TemporaryDirectory() as tempdir:
        source_path = Path(tempdir) / "source.sqlite3"
        target_path = Path(tempdir) / "target.sqlite3"
        snapshot_path = Path(tempdir) / "reward-midcycle.snapshot"
        source = _make_service(source_path)
        reward_a = wallet_key(0)
        reward_b = wallet_key(1)
        miner = wallet_key(2)

        _register_reward_node(source, wallet=reward_a, node_id="reward-node-a", port=19001)
        _register_reward_node(source, wallet=reward_b, node_id="reward-node-b", port=19002)
        _mine_local_block(source, miner.address)
        assignment = source.native_reward_assignments(epoch_index=0, node_id="reward-node-a")[0]
        window_index = assignment["candidate_check_windows"][0]
        verifier_node_id = assignment["verifier_committees"][str(window_index)][0]
        _submit_signed_attestation(
            source,
            epoch_index=0,
            candidate_node_id="reward-node-a",
            verifier_wallet=reward_b,
            verifier_node_id=verifier_node_id,
            window_index=window_index,
            endpoint_commitment="127.0.0.1:19001",
            concentration_key="demo:reward-node-a",
        )
        _mine_local_block(source, miner.address)
        expected_epoch_state = source.native_reward_epoch_state(epoch_index=0)
        expected_attestations = source.native_reward_attestation_diagnostics(epoch_index=0)
        expected_settlements = source.native_reward_settlement_diagnostics(epoch_index=0)
        expected_history = source.reward_history(reward_a.address, limit=100, descending=False)
        source.export_snapshot_file(snapshot_path)

        target = _make_service(target_path, start_time=1_700_014_000)
        target.import_snapshot_file(snapshot_path)

        assert target.native_reward_epoch_state(epoch_index=0)["comparison_keys"] == expected_epoch_state["comparison_keys"]
        assert target.native_reward_epoch_state(epoch_index=0)["active_reward_nodes"] == expected_epoch_state["active_reward_nodes"]
        assert target.native_reward_attestation_diagnostics(epoch_index=0) == expected_attestations
        assert target.native_reward_settlement_diagnostics(epoch_index=0) == expected_settlements
        assert target.reward_history(reward_a.address, limit=100, descending=False) == expected_history
        assert target.snapshot_anchor() is not None
        assert target.snapshot_anchor().block_hash == source.chain_tip().block_hash


def test_native_reward_snapshot_import_after_close_preserves_closed_state_without_duplicates() -> None:
    with TemporaryDirectory() as tempdir:
        source_path = Path(tempdir) / "source.sqlite3"
        target_path = Path(tempdir) / "target.sqlite3"
        snapshot_path = Path(tempdir) / "reward-closed.snapshot"
        source = _make_service(source_path)
        reward_a = wallet_key(0)
        reward_b = wallet_key(1)
        miner = wallet_key(2)

        _register_reward_node(source, wallet=reward_a, node_id="reward-node-a", port=19001)
        _register_reward_node(source, wallet=reward_b, node_id="reward-node-b", port=19002)
        _mine_local_block(source, miner.address)
        assignment = source.native_reward_assignments(epoch_index=0, node_id="reward-node-a")[0]
        window_index = assignment["candidate_check_windows"][0]
        verifier_node_id = assignment["verifier_committees"][str(window_index)][0]
        _submit_signed_attestation(
            source,
            epoch_index=0,
            candidate_node_id="reward-node-a",
            verifier_wallet=reward_b,
            verifier_node_id=verifier_node_id,
            window_index=window_index,
            endpoint_commitment="127.0.0.1:19001",
            concentration_key="demo:reward-node-a",
        )
        _mine_local_block(source, miner.address)
        _mine_until_height(source, miner.address, 3)
        closing_block = _mine_local_block(source, miner.address)
        expected_settlements = source.native_reward_settlement_diagnostics(epoch_index=0)
        expected_history = source.reward_history(reward_a.address, limit=100, descending=False)
        expected_report = source.native_reward_settlement_report(epoch_index=0)
        expected_epoch_state = source.native_reward_epoch_state(epoch_index=0)
        source.export_snapshot_file(snapshot_path)

        target = _make_service(target_path, start_time=1_700_015_000)
        target.import_snapshot_file(snapshot_path)

        imported_state = target.native_reward_epoch_state(epoch_index=0)
        assert imported_state["active_reward_nodes"] == expected_epoch_state["active_reward_nodes"]
        assert imported_state["assignments"] == expected_epoch_state["assignments"]
        assert imported_state["attestations"] == expected_epoch_state["attestations"]
        assert target.native_reward_settlement_diagnostics(epoch_index=0) == expected_settlements
        assert target.native_reward_settlement_report(epoch_index=0)["reward_entries"] == expected_report["reward_entries"]
        assert target.reward_history(reward_a.address, limit=100, descending=False) == expected_history
        assert target.inspect_block(block_hash=closing_block.block_hash())["node_reward_payouts"] == [
            {"recipient": reward_a.address, "amount_chipbits": subsidy_split_chipbits(4, target.params)[1]}
        ]
        assert len(target.native_reward_settlement_diagnostics(epoch_index=0)) == 1
        later_block = _mine_local_block(target, miner.address)
        assert [
            transaction
            for transaction in later_block.transactions
            if transaction.metadata.get("kind") == "reward_settle_epoch"
        ] == []
        assert len(target.native_reward_settlement_diagnostics(epoch_index=0)) == 1
