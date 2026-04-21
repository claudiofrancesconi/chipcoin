import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from contextlib import redirect_stdout
from io import BytesIO
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory

from chipcoin.consensus.epoch_settlement import RewardAttestation
from chipcoin.consensus.merkle import merkle_root
from chipcoin.consensus.models import Block, OutPoint, Transaction
from chipcoin.consensus.params import MAINNET_PARAMS
from chipcoin.consensus.nodes import NodeRecord
from chipcoin.consensus.pow import verify_proof_of_work
from chipcoin.consensus.serialization import deserialize_transaction, serialize_block, serialize_transaction
from chipcoin.interfaces.cli import main as cli_main
from chipcoin.interfaces.http_api import HttpApiApp
from chipcoin.node.service import NodeService
from chipcoin.wallet.signer import TransactionSigner
from ..helpers import put_wallet_utxo, signed_payment, wallet_key


def _make_service(database_path: Path) -> NodeService:
    timestamps = iter(range(1_700_000_000, 1_700_000_400))
    return NodeService.open_sqlite(database_path, time_provider=lambda: next(timestamps))


def _mine_block(block: Block) -> Block:
    for nonce in range(2_000_000):
        header = replace(block.header, nonce=nonce)
        if verify_proof_of_work(header):
            return replace(block, header=header)
    raise AssertionError("Expected to find a valid nonce for the easy target.")


def _call_wsgi(app, *, method: str, path: str, query: str = "", body: object | None = None, origin: str | None = None):
    encoded_body = b"" if body is None else json.dumps(body, sort_keys=True).encode("utf-8")
    environ = {
        "REQUEST_METHOD": method,
        "PATH_INFO": path,
        "QUERY_STRING": query,
        "CONTENT_LENGTH": str(len(encoded_body)),
        "wsgi.input": BytesIO(encoded_body),
    }
    if origin is not None:
        environ["HTTP_ORIGIN"] = origin
    captured: dict[str, object] = {}

    def start_response(status, headers):
        captured["status"] = status
        captured["headers"] = dict(headers)

    raw = b"".join(app(environ, start_response))
    payload = None if not raw else json.loads(raw.decode("utf-8"))
    return captured["status"], captured["headers"], payload


def _block_from_template(template: dict[str, object]) -> Block:
    from chipcoin.consensus.models import BlockHeader, Transaction, TxOutput

    coinbase = Transaction(
        version=1,
        inputs=(),
        outputs=tuple(
            TxOutput(value=int(output["amount_chipbits"]), recipient=str(output["recipient"]))
            for output in template["coinbase_tx"]["outputs"]
        ),
        metadata={"coinbase": "true", "height": str(template["height"]), "extra_nonce": "1"},
    )
    transactions = [coinbase]
    for row in template["transactions"]:
        transaction, offset = deserialize_transaction(bytes.fromhex(row["raw_hex"]))
        assert offset == len(bytes.fromhex(row["raw_hex"]))
        transactions.append(transaction)
    block = Block(
        header=BlockHeader(
            version=int(template["version"]),
            previous_block_hash=str(template["previous_block_hash"]),
            merkle_root=merkle_root([transaction.txid() for transaction in transactions]),
            timestamp=int(template["curtime"]),
            bits=int(template["bits"]),
            nonce=0,
        ),
        transactions=tuple(transactions),
    )
    return _mine_block(block)


def _run_cli(argv: list[str]) -> tuple[int, object]:
    stdout = StringIO()
    with redirect_stdout(stdout):
        code = cli_main(argv)
    return code, json.loads(stdout.getvalue().strip())


def test_http_api_health_status_and_tip() -> None:
    with TemporaryDirectory() as tempdir:
        service = _make_service(Path(tempdir) / "chipcoin.sqlite3")
        app = HttpApiApp(service)

        health_status, _, health_body = _call_wsgi(app, method="GET", path="/v1/health")
        status_status, _, status_body = _call_wsgi(app, method="GET", path="/v1/status")
        supply_status, _, supply_body = _call_wsgi(app, method="GET", path="/v1/supply")
        tip_status, _, tip_body = _call_wsgi(app, method="GET", path="/v1/tip")

        assert health_status == "200 OK"
        assert health_body == {"status": "ok", "api_version": "v1", "network": "mainnet"}
        assert status_status == "200 OK"
        assert status_body["api_version"] == "v1"
        assert status_body["network"] == "mainnet"
        assert status_body["sync"]["mode"] == "idle"
        assert status_body["supply"]["minted_supply_chipbits"] == 0
        assert status_body["supply"]["remaining_supply_chipbits"] == 11_000_000 * 100_000_000
        assert status_body["operator_summary"] == {
            "sync_state": "idle",
            "connectivity_state": "no_known_peers",
            "peer_attention": True,
            "warnings": ["no_known_peers"],
        }
        assert supply_status == "200 OK"
        assert supply_body["api_version"] == "v1"
        assert supply_body["network"] == "mainnet"
        assert supply_body["minted_supply_chipbits"] == 0
        assert supply_body["miner_minted_supply_chipbits"] == 0
        assert supply_body["node_minted_supply_chipbits"] == 0
        assert supply_body["remaining_supply_chipbits"] == 11_000_000 * 100_000_000
        assert tip_status == "200 OK"
        assert tip_body == {"height": None, "block_hash": None}


def test_http_api_exposes_mining_status_and_template() -> None:
    with TemporaryDirectory() as tempdir:
        service = _make_service(Path(tempdir) / "chipcoin.sqlite3")
        app = HttpApiApp(service)

        status_code, _, status_body = _call_wsgi(app, method="GET", path="/mining/status")
        template_code, _, template_body = _call_wsgi(
            app,
            method="POST",
            path="/mining/get-block-template",
            body={"payout_address": wallet_key(0).address, "miner_id": "miner-a"},
        )

        assert status_code == "200 OK"
        assert status_body["network"] == "mainnet"
        assert status_body["best_height"] == -1
        assert status_body["bootstrap_mode"] == "full"
        assert status_body["snapshot_anchor_height"] is None
        assert status_body["snapshot_trust_mode"] == "off"
        assert status_body["sync_phase"] == "idle"
        assert status_body["local_height"] is None
        assert status_body["remote_height"] is None
        assert status_body["current_sync_peers"] == []
        assert template_code == "200 OK"
        assert template_body["template_id"]
        assert template_body["height"] == 0
        assert template_body["previous_block_hash"] == "00" * 32
        assert template_body["payout_address"] == wallet_key(0).address


def test_http_api_exposes_native_reward_epoch_routes() -> None:
    with TemporaryDirectory() as tempdir:
        params = replace(
            MAINNET_PARAMS,
            node_reward_activation_height=0,
            epoch_length_blocks=5,
            reward_check_windows_per_epoch=4,
            reward_target_checks_per_epoch=1,
            reward_min_passed_checks_per_epoch=1,
            reward_verifier_committee_size=1,
            reward_verifier_quorum=1,
            reward_final_confirmation_window_blocks=1,
            max_rewarded_nodes_per_epoch=4,
        )
        timestamps = iter(range(1_700_000_000, 1_700_000_400))
        service = NodeService.open_sqlite(
            Path(tempdir) / "chipcoin.sqlite3",
            params=params,
            time_provider=lambda: next(timestamps),
        )
        app = HttpApiApp(service)
        for node_id, wallet, port in (
            ("reward-node-a", wallet_key(0), 19001),
            ("reward-node-b", wallet_key(1), 19002),
        ):
            service.receive_transaction(
                TransactionSigner(wallet).build_register_reward_node_transaction(
                    node_id=node_id,
                    payout_address=wallet.address,
                    node_public_key_hex=wallet.public_key.hex(),
                    declared_host="127.0.0.1",
                    declared_port=port,
                    registration_fee_chipbits=service.params.register_node_fee_chipbits,
                )
            )
        service.apply_block(_mine_block(service.build_candidate_block("CHCminer").block))

        epoch_status, _, epoch_body = _call_wsgi(app, method="GET", path="/v1/rewards/epoch", query="epoch_index=0")
        assignments_status, _, assignments_body = _call_wsgi(app, method="GET", path="/v1/rewards/assignments", query="epoch_index=0")
        attestations_status, _, attestations_body = _call_wsgi(app, method="GET", path="/v1/rewards/attestations", query="epoch_index=0")
        settlements_status, _, settlements_body = _call_wsgi(app, method="GET", path="/v1/rewards/settlements", query="epoch_index=0")
        report_status, _, report_body = _call_wsgi(app, method="GET", path="/v1/rewards/settlement-report", query="epoch_index=0")

        assert epoch_status == "200 OK"
        assert epoch_body["api_version"] == "v1"
        assert epoch_body["epoch_index"] == 0
        assert epoch_body["active_reward_node_count"] == 2
        assert set(epoch_body["comparison_keys"]) == {
            "active_reward_nodes_digest",
            "assignments_digest",
            "attestations_digest",
            "settlement_preview_digest",
            "stored_settlements_digest",
        }
        assert assignments_status == "200 OK"
        assert assignments_body["assignments"]
        assert len(assignments_body["assignments"]) == 2
        assert attestations_status == "200 OK"
        assert attestations_body["attestations"] == []
        assert settlements_status == "200 OK"
        assert settlements_body["settlements"] == []
        assert report_status == "200 OK"
        assert report_body["rewarded_node_count"] == 0
        assert report_body["settlement_accounting_summary"]["distributed_node_reward_chipbits"] == 0


def test_http_api_reward_node_status_reports_active_stale_and_warming_up() -> None:
    with TemporaryDirectory() as tempdir:
        params = replace(
            MAINNET_PARAMS,
            node_reward_activation_height=0,
            epoch_length_blocks=5,
            reward_node_warmup_epochs=2,
            reward_check_windows_per_epoch=4,
            reward_target_checks_per_epoch=1,
            reward_min_passed_checks_per_epoch=1,
            reward_verifier_committee_size=1,
            reward_verifier_quorum=1,
            reward_final_confirmation_window_blocks=1,
            max_rewarded_nodes_per_epoch=4,
        )
        timestamps = iter(range(1_700_000_000, 1_700_000_400))
        service = NodeService.open_sqlite(
            Path(tempdir) / "chipcoin.sqlite3",
            params=params,
            time_provider=lambda: next(timestamps),
        )
        app = HttpApiApp(service)
        service.node_registry.upsert(
            NodeRecord(
                node_id="reward-node-a",
                payout_address=wallet_key(0).address,
                owner_pubkey=wallet_key(0).public_key,
                registered_height=0,
                last_renewed_height=10,
                node_pubkey=wallet_key(0).public_key,
                declared_host="node-a.example",
                declared_port=19001,
                reward_registration=True,
            )
        )
        service.node_registry.upsert(
            NodeRecord(
                node_id="reward-node-b",
                payout_address=wallet_key(1).address,
                owner_pubkey=wallet_key(1).public_key,
                registered_height=0,
                last_renewed_height=0,
                node_pubkey=wallet_key(1).public_key,
                declared_host="node-b.example",
                declared_port=19002,
                reward_registration=True,
            )
        )
        service.node_registry.upsert(
            NodeRecord(
                node_id="reward-node-c",
                payout_address=wallet_key(2).address,
                owner_pubkey=wallet_key(2).public_key,
                registered_height=6,
                last_renewed_height=10,
                node_pubkey=wallet_key(2).public_key,
                declared_host="node-c.example",
                declared_port=19003,
                reward_registration=True,
            )
        )
        for _ in range(11):
            service.apply_block(_mine_block(service.build_candidate_block("CHCminer").block))

        active_status, _, active_body = _call_wsgi(
            app, method="GET", path="/v1/rewards/node-status", query="node_id=reward-node-a"
        )
        stale_status, _, stale_body = _call_wsgi(
            app, method="GET", path="/v1/rewards/node-status", query="node_id=reward-node-b&epoch_index=2"
        )
        warming_status, _, warming_body = _call_wsgi(
            app, method="GET", path="/v1/rewards/node-status", query="node_id=reward-node-c&epoch_index=2"
        )

        assert active_status == "200 OK"
        assert active_body["eligibility_reason"] == "active_from_height_11"
        assert active_body["selected_epoch_assigned"] is True
        assert active_body["selected_epoch_assignment"]["node_id"] == "reward-node-a"
        assert active_body["reward_state_anchor"]

        assert stale_status == "200 OK"
        assert stale_body["eligibility_reason"] == "missed_renewal_for_epoch_2"
        assert stale_body["selected_epoch_exclusion_reason"] == "no_assignment_because_stale_missed_renewal_for_epoch_2"
        assert stale_body["reward_state_anchor"]

        assert warming_status == "200 OK"
        assert warming_body["eligibility_reason"] == "warming_up_until_height_15"
        assert warming_body["selected_epoch_exclusion_reason"] == "no_assignment_because_warming_up_until_height_15"
        assert warming_body["reward_state_anchor"]


def test_http_api_reward_node_status_errors_for_missing_and_unknown_node_id() -> None:
    with TemporaryDirectory() as tempdir:
        service = _make_service(Path(tempdir) / "chipcoin.sqlite3")
        app = HttpApiApp(service)

        missing_status, _, missing_body = _call_wsgi(app, method="GET", path="/v1/rewards/node-status")
        unknown_status, _, unknown_body = _call_wsgi(
            app, method="GET", path="/v1/rewards/node-status", query="node_id=unknown-node"
        )

        assert missing_status == "400 Bad Request"
        assert missing_body["error"]["code"] == "invalid_request"
        assert missing_body["error"]["message"] == "node_id is required"
        assert unknown_status == "404 Not Found"
        assert unknown_body["error"]["code"] == "not_found"
        assert unknown_body["error"]["message"] == "Node id is not registered."


def test_http_api_reward_epoch_summary_reports_open_epoch_and_required_fields() -> None:
    with TemporaryDirectory() as tempdir:
        params = replace(
            MAINNET_PARAMS,
            node_reward_activation_height=0,
            epoch_length_blocks=5,
            reward_node_warmup_epochs=0,
            reward_check_windows_per_epoch=4,
            reward_target_checks_per_epoch=1,
            reward_min_passed_checks_per_epoch=1,
            reward_verifier_committee_size=1,
            reward_verifier_quorum=1,
            reward_final_confirmation_window_blocks=1,
            max_rewarded_nodes_per_epoch=4,
        )
        timestamps = iter(range(1_700_000_000, 1_700_000_400))
        service = NodeService.open_sqlite(
            Path(tempdir) / "chipcoin.sqlite3",
            params=params,
            time_provider=lambda: next(timestamps),
        )
        app = HttpApiApp(service)
        service.node_registry.upsert(
            NodeRecord(
                node_id="reward-node-a",
                payout_address=wallet_key(0).address,
                owner_pubkey=wallet_key(0).public_key,
                registered_height=0,
                last_renewed_height=10,
                node_pubkey=wallet_key(0).public_key,
                declared_host="node-a.example",
                declared_port=19001,
                reward_registration=True,
            )
        )
        for _ in range(11):
            service.apply_block(_mine_block(service.build_candidate_block("CHCminer").block))

        status, _, body = _call_wsgi(app, method="GET", path="/v1/rewards/epoch-summary", query="epoch_index=2")

        assert status == "200 OK"
        assert body["active_reward_node_count"] == 1
        assert body["active_reward_node_ids"] == ["reward-node-a"]
        assert body["settlement_status"] == "open"
        assert body["settlement_reason"] == "no_settlement_because_epoch_open"
        assert body["reward_state_anchor"]


def test_http_api_reward_epoch_summary_reports_closed_epoch_with_payouts() -> None:
    with TemporaryDirectory() as tempdir:
        params = replace(
            MAINNET_PARAMS,
            node_reward_activation_height=0,
            epoch_length_blocks=5,
            reward_node_warmup_epochs=0,
            reward_check_windows_per_epoch=4,
            reward_target_checks_per_epoch=1,
            reward_min_passed_checks_per_epoch=1,
            reward_verifier_committee_size=1,
            reward_verifier_quorum=1,
            reward_final_confirmation_window_blocks=1,
            max_rewarded_nodes_per_epoch=4,
        )
        timestamps = iter(range(1_700_000_000, 1_700_000_400))
        service = NodeService.open_sqlite(
            Path(tempdir) / "chipcoin.sqlite3",
            params=params,
            time_provider=lambda: next(timestamps),
        )
        app = HttpApiApp(service)

        reward_a = wallet_key(0)
        reward_b = wallet_key(1)
        for node_id, wallet, port in (
            ("reward-node-a", reward_a, 19001),
            ("reward-node-b", reward_b, 19002),
        ):
            service.receive_transaction(
                TransactionSigner(wallet).build_register_reward_node_transaction(
                    node_id=node_id,
                    payout_address=wallet.address,
                    node_public_key_hex=wallet.public_key.hex(),
                    declared_host="127.0.0.1",
                    declared_port=port,
                    registration_fee_chipbits=service.params.register_node_fee_chipbits,
                )
            )
        service.apply_block(_mine_block(service.build_candidate_block("CHCminer").block))
        assignment = service.native_reward_assignments(epoch_index=0, node_id="reward-node-a")[0]
        window_index = assignment["candidate_check_windows"][0]
        verifier_node_id = assignment["verifier_committees"][str(window_index)][0]
        attestation = TransactionSigner(reward_b).sign_reward_attestation(
            RewardAttestation(
                epoch_index=0,
                check_window_index=window_index,
                candidate_node_id="reward-node-a",
                verifier_node_id=verifier_node_id,
                result_code="pass",
                observed_sync_gap=0,
                endpoint_commitment="127.0.0.1:19001",
                concentration_key="demo:reward-node-a",
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
                    "epoch_index": "0",
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
        service.apply_block(_mine_block(service.build_candidate_block("CHCminer").block))
        while service.chain_tip() is not None and service.chain_tip().height < 4:
            service.apply_block(_mine_block(service.build_candidate_block("CHCminer").block))

        status, _, body = _call_wsgi(app, method="GET", path="/v1/rewards/epoch-summary", query="epoch_index=0")

        assert status == "200 OK"
        assert body["settlement_status"] == "closed"
        assert body["settlement_reason"] == "settlement_stored"
        assert body["settlement_exists"] is True
        assert body["rewarded_node_ids"] == ["reward-node-a"]
        assert body["payout_totals"]["distributed_node_reward_chipbits"] > 0
        assert body["reward_state_anchor"]


def test_http_api_reward_epoch_summary_errors_for_missing_or_invalid_epoch_index() -> None:
    with TemporaryDirectory() as tempdir:
        service = _make_service(Path(tempdir) / "chipcoin.sqlite3")
        app = HttpApiApp(service)

        missing_status, _, missing_body = _call_wsgi(app, method="GET", path="/v1/rewards/epoch-summary")
        invalid_status, _, invalid_body = _call_wsgi(
            app, method="GET", path="/v1/rewards/epoch-summary", query="epoch_index=-1"
        )

        assert missing_status == "400 Bad Request"
        assert missing_body["error"]["code"] == "invalid_request"
        assert missing_body["error"]["message"] == "epoch_index is required"
        assert invalid_status == "400 Bad Request"
        assert invalid_body["error"]["code"] == "invalid_request"
        assert "epoch_index must be >= 0" in invalid_body["error"]["message"]


def test_http_api_submit_block_accepts_solved_template_and_rejects_stale() -> None:
    with TemporaryDirectory() as tempdir:
        service = _make_service(Path(tempdir) / "chipcoin.sqlite3")
        app = HttpApiApp(service)

        _, _, template_body = _call_wsgi(
            app,
            method="POST",
            path="/mining/get-block-template",
            body={"payout_address": wallet_key(0).address, "miner_id": "miner-a"},
        )
        solved_block = _block_from_template(template_body)
        submit_status, _, submit_body = _call_wsgi(
            app,
            method="POST",
            path="/mining/submit-block",
            body={
                "template_id": template_body["template_id"],
                "serialized_block": serialize_block(solved_block).hex(),
                "miner_id": "miner-a",
            },
        )

        assert submit_status == "200 OK"
        assert submit_body["accepted"] is True
        assert submit_body["became_tip"] is True

        _, _, stale_template_body = _call_wsgi(
            app,
            method="POST",
            path="/mining/get-block-template",
            body={"payout_address": wallet_key(0).address, "miner_id": "miner-a"},
        )
        service.apply_block(_mine_block(service.build_candidate_block("CHCminer").block))
        stale_block = _block_from_template(stale_template_body)
        stale_status, _, stale_body = _call_wsgi(
            app,
            method="POST",
            path="/mining/submit-block",
            body={
                "template_id": stale_template_body["template_id"],
                "serialized_block": serialize_block(stale_block).hex(),
                "miner_id": "miner-a",
            },
        )

        assert stale_status == "200 OK"
        assert stale_body["accepted"] is False
        assert stale_body["reason"] == "unknown_or_expired_template"


def test_http_api_blocks_and_block_lookup_by_height_and_hash() -> None:
    with TemporaryDirectory() as tempdir:
        service = _make_service(Path(tempdir) / "chipcoin.sqlite3")
        first = _mine_block(service.build_candidate_block("CHCminer").block)
        service.apply_block(first)
        second = _mine_block(service.build_candidate_block("CHCminer").block)
        service.apply_block(second)
        app = HttpApiApp(service)

        blocks_status, _, blocks_body = _call_wsgi(app, method="GET", path="/v1/blocks")
        by_height_status, _, by_height_body = _call_wsgi(app, method="GET", path="/v1/block", query="height=1")
        by_hash_status, _, by_hash_body = _call_wsgi(app, method="GET", path="/v1/block", query=f"hash={first.block_hash()}")

        assert blocks_status == "200 OK"
        assert [row["height"] for row in blocks_body] == [1, 0]
        assert by_height_status == "200 OK"
        assert by_height_body["block_hash"] == second.block_hash()
        assert by_height_body["height"] == 1
        assert by_hash_status == "200 OK"
        assert by_hash_body["block_hash"] == first.block_hash()
        assert by_hash_body["height"] == 0


def test_http_api_block_lookup_rejects_invalid_queries_and_returns_not_found() -> None:
    with TemporaryDirectory() as tempdir:
        service = _make_service(Path(tempdir) / "chipcoin.sqlite3")
        app = HttpApiApp(service)

        both_status, _, both_body = _call_wsgi(app, method="GET", path="/v1/block", query="hash=aa&height=0")
        missing_status, _, missing_body = _call_wsgi(app, method="GET", path="/v1/block")
        not_found_status, _, not_found_body = _call_wsgi(app, method="GET", path="/v1/block", query="height=5")

        assert both_status == "400 Bad Request"
        assert both_body["error"]["code"] == "invalid_request"
        assert missing_status == "400 Bad Request"
        assert missing_body["error"]["code"] == "invalid_request"
        assert not_found_status == "404 Not Found"
        assert not_found_body["error"]["code"] == "not_found"


def test_http_api_transaction_lookup_and_submit() -> None:
    with TemporaryDirectory() as tempdir:
        service = _make_service(Path(tempdir) / "chipcoin.sqlite3")
        funding_outpoint = OutPoint(txid="11" * 32, index=0)
        put_wallet_utxo(service, funding_outpoint, value=100, owner=wallet_key(0))
        transaction = signed_payment(funding_outpoint, value=100, sender=wallet_key(0), fee=10)
        app = HttpApiApp(service)
        raw_hex = serialize_transaction(transaction).hex()

        submit_status, _, submit_body = _call_wsgi(app, method="POST", path="/v1/tx/submit", body={"raw_hex": raw_hex})
        tx_status, _, tx_body = _call_wsgi(app, method="GET", path=f"/v1/tx/{transaction.txid()}")

        assert submit_status == "200 OK"
        assert submit_body["accepted"] is True
        assert tx_status == "200 OK"
        assert tx_body["location"] == "mempool"
        assert tx_body["transaction"]["txid"] == transaction.txid()


def test_http_api_submit_tx_reports_invalid_and_validation_errors() -> None:
    with TemporaryDirectory() as tempdir:
        service = _make_service(Path(tempdir) / "chipcoin.sqlite3")
        funding_outpoint = OutPoint(txid="22" * 32, index=0)
        put_wallet_utxo(service, funding_outpoint, value=100, owner=wallet_key(0))
        transaction = signed_payment(funding_outpoint, value=100, sender=wallet_key(0), fee=10)
        raw_hex = serialize_transaction(transaction).hex()
        app = HttpApiApp(service)

        invalid_status, _, invalid_body = _call_wsgi(app, method="POST", path="/v1/tx/submit", body={})
        first_status, _, _ = _call_wsgi(app, method="POST", path="/v1/tx/submit", body={"raw_hex": raw_hex})
        second_status, _, second_body = _call_wsgi(app, method="POST", path="/v1/tx/submit", body={"raw_hex": raw_hex})

        assert invalid_status == "400 Bad Request"
        assert invalid_body["error"]["code"] == "invalid_request"
        assert first_status == "200 OK"
        assert second_status == "400 Bad Request"
        assert second_body["error"]["code"] == "validation_error"


def test_http_api_tx_lookup_returns_not_found() -> None:
    with TemporaryDirectory() as tempdir:
        service = _make_service(Path(tempdir) / "chipcoin.sqlite3")
        app = HttpApiApp(service)

        status, _, body = _call_wsgi(app, method="GET", path=f"/v1/tx/{'aa' * 32}")

        assert status == "404 Not Found"
        assert body["error"]["code"] == "not_found"


def test_http_api_address_summary_and_utxos() -> None:
    with TemporaryDirectory() as tempdir:
        service = _make_service(Path(tempdir) / "chipcoin.sqlite3")
        owner = wallet_key(0)
        put_wallet_utxo(service, OutPoint(txid="33" * 32, index=0), value=75, owner=owner, height=0, is_coinbase=False)
        put_wallet_utxo(service, OutPoint(txid="44" * 32, index=1), value=50, owner=owner, height=0, is_coinbase=True)
        app = HttpApiApp(service)

        summary_status, _, summary_body = _call_wsgi(app, method="GET", path=f"/v1/address/{owner.address}")
        utxos_status, _, utxos_body = _call_wsgi(app, method="GET", path=f"/v1/address/{owner.address}/utxos")

        assert summary_status == "200 OK"
        assert summary_body["address"] == owner.address
        assert summary_body["confirmed_balance_chipbits"] == 125
        assert summary_body["immature_balance_chipbits"] == 50
        assert summary_body["spendable_balance_chipbits"] == 75
        assert utxos_status == "200 OK"
        assert len(utxos_body) == 2
        assert {row["txid"] for row in utxos_body} == {"33" * 32, "44" * 32}


def test_http_api_address_history() -> None:
    with TemporaryDirectory() as tempdir:
        service = _make_service(Path(tempdir) / "chipcoin.sqlite3")
        owner = wallet_key(0)
        recipient = wallet_key(1)
        funding_outpoint = OutPoint(txid="55" * 32, index=0)
        put_wallet_utxo(service, funding_outpoint, value=100, owner=owner)
        transaction = signed_payment(funding_outpoint, value=100, sender=owner, recipient=recipient.address, fee=10)
        service.receive_transaction(transaction)
        mined = _mine_block(service.build_candidate_block("CHCminer").block)
        service.apply_block(mined)
        app = HttpApiApp(service)

        status, _, body = _call_wsgi(
            app,
            method="GET",
            path=f"/v1/address/{recipient.address}/history",
            query="limit=10&order=desc",
        )

        assert status == "200 OK"
        assert len(body) >= 1
        assert body[0]["txid"] == transaction.txid()
        assert body[0]["incoming_chipbits"] > 0
        assert "net_chipbits" in body[0]


def test_http_api_address_endpoints_reject_invalid_address() -> None:
    with TemporaryDirectory() as tempdir:
        service = _make_service(Path(tempdir) / "chipcoin.sqlite3")
        app = HttpApiApp(service)

        summary_status, _, summary_body = _call_wsgi(app, method="GET", path="/v1/address/not-a-valid-address")
        history_status, _, history_body = _call_wsgi(app, method="GET", path="/v1/address/not-a-valid-address/history")

        assert summary_status == "400 Bad Request"
        assert summary_body["error"]["code"] == "invalid_request"
        assert history_status == "400 Bad Request"
        assert history_body["error"]["code"] == "invalid_request"


def test_http_api_mempool_includes_machine_friendly_fee_rate() -> None:
    with TemporaryDirectory() as tempdir:
        service = _make_service(Path(tempdir) / "chipcoin.sqlite3")
        funding_outpoint = OutPoint(txid="66" * 32, index=0)
        put_wallet_utxo(service, funding_outpoint, value=100, owner=wallet_key(0))
        transaction = signed_payment(funding_outpoint, value=100, sender=wallet_key(0), fee=10)
        service.receive_transaction(transaction)
        app = HttpApiApp(service)

        status, _, body = _call_wsgi(app, method="GET", path="/v1/mempool")

        assert status == "200 OK"
        assert len(body) == 1
        assert body[0]["txid"] == transaction.txid()
        assert "fee_rate" in body[0]
        assert "fee_rate_chipbits_per_weight_unit" in body[0]
        assert isinstance(body[0]["fee_rate_chipbits_per_weight_unit"], float)


def test_http_api_peers_and_peers_summary() -> None:
    with TemporaryDirectory() as tempdir:
        service = _make_service(Path(tempdir) / "chipcoin.sqlite3")
        service.add_peer("127.0.0.1", 8333, source="manual")
        app = HttpApiApp(service)

        peers_status, _, peers_body = _call_wsgi(app, method="GET", path="/v1/peers")
        summary_status, _, summary_body = _call_wsgi(app, method="GET", path="/v1/peers/summary")

        assert peers_status == "200 OK"
        assert peers_body[0]["host"] == "127.0.0.1"
        assert peers_body[0]["source"] == "manual"
        assert peers_body[0]["peer_state"] == "manual"
        assert "handshake_complete" in peers_body[0]
        assert "misbehavior_score" in peers_body[0]
        assert "banned" in peers_body[0]
        assert peers_body[0]["backoff_remaining_seconds"] == 0
        assert peers_body[0]["ban_remaining_seconds"] == 0
        assert summary_status == "200 OK"
        assert summary_body["peer_count"] == 1
        assert summary_body["banned_peer_count"] == 0
        assert summary_body["peer_count_by_source"] == {"manual": 1}
        assert summary_body["peer_count_by_state"] == {"manual": 1}
        assert summary_body["good_peer_count"] == 0
        assert summary_body["non_banned_peer_count"] == 1
        assert summary_body["operator_summary"] == {
            "peer_health": "ok",
            "non_banned_peer_count": 1,
            "active_backoff_peer_count": 0,
            "active_ban_count": 0,
            "warnings": [],
        }


def test_http_api_and_cli_surfaces_are_consistent_for_status_and_peer_summary() -> None:
    with TemporaryDirectory() as tempdir:
        db_path = Path(tempdir) / "chipcoin.sqlite3"
        service = _make_service(db_path)
        service.add_peer("127.0.0.1", 18444, source="manual")
        app = HttpApiApp(service)

        cli_status_code, cli_status = _run_cli(["--data", str(db_path), "status"])
        cli_summary_code, cli_summary = _run_cli(["--data", str(db_path), "peer-summary"])
        http_status_code, _, http_status = _call_wsgi(app, method="GET", path="/v1/status")
        http_summary_code, _, http_summary = _call_wsgi(app, method="GET", path="/v1/peers/summary")

        assert cli_status_code == 0
        assert cli_summary_code == 0
        assert http_status_code == "200 OK"
        assert http_summary_code == "200 OK"
        assert cli_status["network"] == http_status["network"]
        assert cli_status["sync"] == http_status["sync"]
        assert cli_status["operator_summary"] == http_status["operator_summary"]
        assert cli_summary == http_summary
        assert http_summary["peer_count_by_network"]["mainnet"] == 1


def test_http_api_stable_client_subset_shapes() -> None:
    with TemporaryDirectory() as tempdir:
        service = _make_service(Path(tempdir) / "chipcoin.sqlite3")
        owner = wallet_key(0)
        funding_outpoint = OutPoint(txid="77" * 32, index=0)
        put_wallet_utxo(service, funding_outpoint, value=100, owner=owner)
        transaction = signed_payment(funding_outpoint, value=100, sender=owner, fee=10)
        service.receive_transaction(transaction)
        mined = _mine_block(service.build_candidate_block("CHCminer").block)
        service.apply_block(mined)
        service.add_peer("127.0.0.1", 18444, source="manual")
        app = HttpApiApp(service)

        _, _, health = _call_wsgi(app, method="GET", path="/v1/health")
        _, _, status = _call_wsgi(app, method="GET", path="/v1/status")
        _, _, blocks = _call_wsgi(app, method="GET", path="/v1/blocks")
        _, _, block = _call_wsgi(app, method="GET", path="/v1/block", query="height=0")
        _, _, tx = _call_wsgi(app, method="GET", path=f"/v1/tx/{transaction.txid()}")
        _, _, address = _call_wsgi(app, method="GET", path=f"/v1/address/{owner.address}")
        _, _, utxos = _call_wsgi(app, method="GET", path=f"/v1/address/{owner.address}/utxos")
        _, _, history = _call_wsgi(app, method="GET", path=f"/v1/address/{owner.address}/history")
        _, _, mempool = _call_wsgi(app, method="GET", path="/v1/mempool")
        _, _, peers = _call_wsgi(app, method="GET", path="/v1/peers")
        _, _, peer_summary = _call_wsgi(app, method="GET", path="/v1/peers/summary")

        assert set(health) == {"api_version", "network", "status"}
        assert health["status"] == "ok"

        assert {
            "api_version",
            "network",
            "network_magic_hex",
            "height",
            "tip_hash",
            "current_bits",
            "current_target",
            "current_difficulty_ratio",
            "expected_next_bits",
            "expected_next_target",
            "cumulative_work",
            "mempool_size",
            "peer_count",
            "handshaken_peer_count",
            "banned_peer_count",
            "sync",
            "operator_summary",
            "next_block_node_reward_recipients",
            "supply",
        }.issubset(status.keys())
        assert {
            "network",
            "height",
            "max_supply_chipbits",
            "minted_supply_chipbits",
            "miner_minted_supply_chipbits",
            "node_minted_supply_chipbits",
            "circulating_supply_chipbits",
            "remaining_supply_chipbits",
        }.issubset(status["supply"].keys())
        assert {
            "mode",
            "validated_tip_height",
            "validated_tip_hash",
            "best_header_height",
            "best_header_hash",
            "missing_block_count",
            "queued_block_count",
            "inflight_block_count",
            "inflight_block_hashes",
            "header_peer_count",
            "header_peers",
            "block_peer_count",
            "block_peers",
            "stalled_peers",
            "download_window",
        }.issubset(status["sync"].keys())

        assert blocks and {"height", "block_hash", "timestamp", "bits", "difficulty_target", "difficulty_ratio", "cumulative_work", "weight_units", "transaction_count"}.issubset(blocks[0].keys())
        assert {"block_hash", "height", "header", "cumulative_work", "weight_units", "fees_chipbits", "miner_payout_chipbits", "node_reward_payouts", "transaction_count", "transactions"}.issubset(block.keys())
        assert {"location", "block_hash", "height", "transaction"}.issubset(tx.keys())
        assert {"address", "confirmed_balance_chipbits", "immature_balance_chipbits", "spendable_balance_chipbits", "utxo_count"}.issubset(address.keys())
        assert isinstance(utxos, list)
        assert isinstance(history, list)
        assert isinstance(mempool, list)
        assert peers and {
            "host",
            "port",
            "network",
            "network_magic_hex",
            "source",
            "peer_state",
            "handshake_complete",
            "score",
            "misbehavior_score",
            "ban_until",
            "ban_remaining_seconds",
            "backoff_remaining_seconds",
            "banned",
        }.issubset(peers[0].keys())
        assert {
            "error_class_counts",
            "penalty_reason_counts",
            "peer_count_by_network",
            "peer_count_by_direction",
            "peer_count_by_source",
            "peer_count_by_state",
            "peer_count_by_handshake_status",
            "good_peer_count",
            "questionable_peer_count",
            "manual_peer_count",
            "seed_peer_count",
            "discovered_peer_count",
            "non_banned_peer_count",
            "backoff_peer_count",
            "banned_peer_count",
            "backoff_peers",
            "worst_score_peer",
            "highest_misbehavior_peer",
            "most_disconnected_peer",
            "most_recent_error_peer",
            "peer_count",
            "operator_summary",
        }.issubset(peer_summary.keys())


def test_http_api_error_payload_is_stable_across_common_failures() -> None:
    with TemporaryDirectory() as tempdir:
        service = _make_service(Path(tempdir) / "chipcoin.sqlite3")
        app = HttpApiApp(service)

        responses = [
            _call_wsgi(app, method="GET", path="/v1/block"),
            _call_wsgi(app, method="GET", path="/v1/block", query="height=99"),
            _call_wsgi(app, method="GET", path="/v1/address/not-a-valid-address"),
            _call_wsgi(app, method="POST", path="/v1/tx/submit", body={}),
            _call_wsgi(app, method="GET", path="/v1/not-found"),
        ]

        for status, _headers, body in responses:
            assert status in {"400 Bad Request", "404 Not Found"}
            assert set(body.keys()) == {"error"}
            assert set(body["error"].keys()) == {"code", "message"}
            assert isinstance(body["error"]["code"], str)
            assert isinstance(body["error"]["message"], str)


def test_http_api_blocks_validation() -> None:
    with TemporaryDirectory() as tempdir:
        service = _make_service(Path(tempdir) / "chipcoin.sqlite3")
        block = _mine_block(service.build_candidate_block("CHCminer").block)
        service.apply_block(block)
        app = HttpApiApp(service)

        limit_status, _, limit_body = _call_wsgi(app, method="GET", path="/v1/blocks", query="limit=101")
        from_status, _, from_body = _call_wsgi(app, method="GET", path="/v1/blocks", query="from_height=10")

        assert limit_status == "400 Bad Request"
        assert limit_body["error"]["code"] == "invalid_request"
        assert from_status == "400 Bad Request"
        assert from_body["error"]["code"] == "invalid_request"


def test_http_api_cors_allow_list() -> None:
    with TemporaryDirectory() as tempdir:
        service = _make_service(Path(tempdir) / "chipcoin.sqlite3")
        app = HttpApiApp(service, allowed_origins={"http://localhost:3000"})

        ok_status, ok_headers, _ = _call_wsgi(app, method="GET", path="/v1/health", origin="http://localhost:3000")
        blocked_status, blocked_headers, _ = _call_wsgi(app, method="GET", path="/v1/health", origin="http://evil.example")
        options_status, options_headers, options_body = _call_wsgi(
            app,
            method="OPTIONS",
            path="/v1/status",
            origin="http://localhost:3000",
        )

        assert ok_status == "200 OK"
        assert ok_headers["Access-Control-Allow-Origin"] == "http://localhost:3000"
        assert "Access-Control-Allow-Origin" not in blocked_headers
        assert blocked_status == "200 OK"
        assert options_status == "204 No Content"
        assert options_headers["Access-Control-Allow-Origin"] == "http://localhost:3000"
        assert options_headers["Access-Control-Allow-Methods"] == "GET, POST, OPTIONS"
        assert options_headers["Access-Control-Allow-Headers"] == "Content-Type"
        assert options_body is None


def test_http_api_handles_concurrent_read_requests() -> None:
    with TemporaryDirectory() as tempdir:
        service = _make_service(Path(tempdir) / "chipcoin.sqlite3")
        owner = wallet_key(0)
        for index in range(3):
            block = _mine_block(service.build_candidate_block(owner.address).block)
            service.apply_block(block)
        app = HttpApiApp(service)

        requests = [
            ("GET", "/v1/status", ""),
            ("GET", "/v1/blocks", "limit=3"),
            ("GET", f"/v1/address/{owner.address}", ""),
            ("GET", f"/v1/address/{owner.address}/utxos", ""),
            ("GET", f"/v1/address/{owner.address}/history", "limit=10&order=desc"),
        ]

        with ThreadPoolExecutor(max_workers=len(requests)) as executor:
            results = list(
                executor.map(
                    lambda request: _call_wsgi(app, method=request[0], path=request[1], query=request[2]),
                    requests,
                )
            )

        assert all(status == "200 OK" for status, _headers, _body in results)
