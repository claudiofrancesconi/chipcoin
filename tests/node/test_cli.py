import asyncio
import json
import logging
import socket
from contextlib import redirect_stdout
from dataclasses import replace
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

from chipcoin.consensus.nodes import NodeRecord
from chipcoin.consensus.params import DEVNET_PARAMS, MAINNET_PARAMS
from chipcoin.consensus.models import Block, OutPoint
from chipcoin.consensus.pow import verify_proof_of_work
from chipcoin.consensus.serialization import serialize_transaction
from chipcoin.crypto.keys import serialize_private_key_hex
from chipcoin.interfaces import cli as cli_module
from chipcoin.interfaces.cli import main
from chipcoin.node.runtime import NodeRuntime
from chipcoin.node.service import NodeService
from tests.helpers import put_wallet_utxo, signed_payment, wallet_key


def _make_service(database_path: Path) -> NodeService:
    timestamps = iter(range(1_700_000_000, 1_700_000_200))
    return NodeService.open_sqlite(database_path, time_provider=lambda: next(timestamps))


def _mine_block(block: Block) -> Block:
    for nonce in range(2_000_000):
        header = replace(block.header, nonce=nonce)
        if verify_proof_of_work(header):
            return replace(block, header=header)
    raise AssertionError("Expected to find a valid nonce for the easy target.")


def _run_cli(argv: list[str]) -> tuple[int, object]:
    stdout = StringIO()
    with redirect_stdout(stdout):
        code = main(argv)
    output = stdout.getvalue().strip()
    return code, json.loads(output)


def _run_cli_with_stderr(argv: list[str]) -> tuple[int, str, str]:
    import contextlib

    stdout = StringIO()
    stderr = StringIO()
    with redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = main(argv)
    return code, stdout.getvalue().strip(), stderr.getvalue().strip()


async def _start_runtime_with_http(service: NodeService) -> NodeRuntime:
    runtime = NodeRuntime(
        service=service,
        listen_host="127.0.0.1",
        listen_port=0,
        http_host="127.0.0.1",
        http_port=_free_port(),
        ping_interval=0.2,
    )
    await runtime.start()
    return runtime


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _require_local_socket_support() -> None:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
    except PermissionError as exc:
        raise pytest.skip(f"local TCP binds are unavailable in this environment: {exc}") from exc


def _ed25519_private_key_hex() -> tuple[str, str]:
    private_key = ed25519.Ed25519PrivateKey.generate()
    private_bytes = private_key.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_bytes = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return private_bytes.hex(), public_bytes.hex()


def test_cli_start_returns_status_snapshot() -> None:
    with TemporaryDirectory() as tempdir:
        db_path = Path(tempdir) / "chipcoin.sqlite3"

        code, payload = _run_cli(["--data", str(db_path), "start"])

        assert code == 0
        assert payload["started"] is True
        assert payload["status"]["network"] == "mainnet"


def test_cli_start_uses_devnet_profile() -> None:
    with TemporaryDirectory() as tempdir:
        db_path = Path(tempdir) / "chipcoin-devnet.sqlite3"

        code, payload = _run_cli(["--network", "devnet", "--data", str(db_path), "start"])

        assert code == 0
        assert payload["started"] is True
        assert payload["status"]["network"] == "devnet"
        assert payload["status"]["current_bits"] == DEVNET_PARAMS.genesis_bits


def test_cli_uses_network_specific_default_data_path() -> None:
    with TemporaryDirectory() as tempdir:
        original_cwd = Path.cwd()
        try:
            import os

            os.chdir(tempdir)
            code, payload = _run_cli(["--network", "devnet", "start"])
            assert code == 0
            assert payload["status"]["network"] == "devnet"
            assert (Path(tempdir) / "chipcoin-devnet.sqlite3").exists()
            assert not (Path(tempdir) / "chipcoin.sqlite3").exists()
        finally:
            os.chdir(original_cwd)


def test_cli_status_and_tip() -> None:
    with TemporaryDirectory() as tempdir:
        db_path = Path(tempdir) / "chipcoin.sqlite3"
        service = _make_service(db_path)
        mined = _mine_block(service.build_candidate_block("CHCminer").block)
        service.apply_block(mined)

        status_code, status_payload = _run_cli(["--data", str(db_path), "status"])
        tip_code, tip_payload = _run_cli(["--data", str(db_path), "tip"])

        assert status_code == 0
        assert status_payload["height"] == 0
        assert status_payload["tip_hash"] == mined.block_hash()
        assert status_payload["bootstrap_mode"] == "full"
        assert status_payload["snapshot_anchor_height"] is None
        assert status_payload["snapshot_anchor_hash"] is None
        assert status_payload["snapshot_trust_mode"] == "off"
        assert status_payload["accepted_snapshot_signer_pubkeys"] == []
        assert status_payload["sync_phase"] == "idle"
        assert status_payload["current_bits"] == mined.header.bits
        assert status_payload["cumulative_work"] is not None
        assert status_payload["expected_next_bits"] == mined.header.bits
        assert status_payload["sync"]["mode"] == "idle"
        assert status_payload["sync"]["phase"] == "idle"
        assert status_payload["sync"]["local_height"] == 0
        assert status_payload["sync"]["remote_height"] == 0
        assert status_payload["operator_summary"] == {
            "sync_state": "idle",
            "connectivity_state": "no_known_peers",
            "peer_attention": True,
            "warnings": ["no_known_peers"],
        }
        assert tip_code == 0
        assert tip_payload["block_hash"] == mined.block_hash()
        assert tip_payload["bits"] == mined.header.bits
        assert tip_payload["transaction_count"] == len(mined.transactions)


def test_cli_block_lookup_by_height_and_hash() -> None:
    with TemporaryDirectory() as tempdir:
        db_path = Path(tempdir) / "chipcoin.sqlite3"
        service = _make_service(db_path)
        mined = _mine_block(service.build_candidate_block("CHCminer").block)
        service.apply_block(mined)

        by_height_code, by_height = _run_cli(["--data", str(db_path), "block", "--height", "0"])
        by_hash_code, by_hash = _run_cli(["--data", str(db_path), "block", "--hash", mined.block_hash()])

        assert by_height_code == 0
        assert by_hash_code == 0
        assert by_height["block_hash"] == mined.block_hash()
        assert by_hash["block_hash"] == mined.block_hash()
        assert by_hash["header"]["bits"] == mined.header.bits
        assert by_hash["miner_payout_chipbits"] == int(mined.transactions[0].outputs[0].value)
        assert by_hash["weight_units"] > 0


def test_cli_tx_lookup_and_submit_raw_tx() -> None:
    async def scenario() -> None:
        _require_local_socket_support()
        with TemporaryDirectory() as tempdir:
            db_path = Path(tempdir) / "chipcoin.sqlite3"
            service = _make_service(db_path)
            funding_outpoint = OutPoint(txid="11" * 32, index=0)
            put_wallet_utxo(service, funding_outpoint, value=100, owner=wallet_key(0))
            transaction = signed_payment(funding_outpoint, value=100, sender=wallet_key(0), fee=10)
            raw_hex = serialize_transaction(transaction).hex()
            runtime = await _start_runtime_with_http(service)
            try:
                submit_code, submit_payload = await asyncio.to_thread(
                    _run_cli,
                    ["submit-raw-tx", "--node-url", f"http://127.0.0.1:{runtime.http_bound_port}", raw_hex],
                )
                tx_code, tx_payload = await asyncio.to_thread(_run_cli, ["--data", str(db_path), "tx", transaction.txid()])
                assert submit_code == 0
                assert submit_payload["accepted"] is True
                assert submit_payload["txid"] == transaction.txid()
                assert tx_code == 0
                assert tx_payload["location"] == "mempool"
                assert tx_payload["transaction"]["txid"] == transaction.txid()
            finally:
                await runtime.stop()

    asyncio.run(scenario())


def test_cli_snapshot_export_and_import() -> None:
    with TemporaryDirectory() as tempdir:
        source_db = Path(tempdir) / "source.sqlite3"
        target_db = Path(tempdir) / "target.sqlite3"
        snapshot_path = Path(tempdir) / "chain.snapshot.json"
        source = _make_service(source_db)
        mined = _mine_block(source.build_candidate_block("CHCminer").block)
        source.apply_block(mined)

        export_code, export_payload = _run_cli(
            ["--data", str(source_db), "snapshot-export", "--snapshot-file", str(snapshot_path)]
        )
        import_code, import_payload = _run_cli(
            ["--data", str(target_db), "snapshot-import", "--snapshot-file", str(snapshot_path)]
        )

        imported_service = _make_service(target_db)
        assert export_code == 0
        assert import_code == 0
        assert export_payload["snapshot_block_hash"] == mined.block_hash()
        assert import_payload["snapshot_block_hash"] == mined.block_hash()
        assert imported_service.chain_tip() is not None
        assert imported_service.chain_tip().block_hash == mined.block_hash()


def test_cli_snapshot_sign_and_enforce_import() -> None:
    with TemporaryDirectory() as tempdir:
        source_db = Path(tempdir) / "source.sqlite3"
        target_db = Path(tempdir) / "target.sqlite3"
        snapshot_path = Path(tempdir) / "chain.snapshot.json"
        source = _make_service(source_db)
        mined = _mine_block(source.build_candidate_block("CHCminer").block)
        source.apply_block(mined)
        private_key_hex, public_key_hex = _ed25519_private_key_hex()

        export_code, export_payload = _run_cli(
            ["--data", str(source_db), "snapshot-export", "--snapshot-file", str(snapshot_path)]
        )
        sign_code, sign_payload = _run_cli(
            [
                "snapshot-sign",
                "--snapshot-file",
                str(snapshot_path),
                "--private-key-hex",
                private_key_hex,
            ]
        )
        import_code, import_payload = _run_cli(
            [
                "--network",
                "mainnet",
                "--data",
                str(target_db),
                "snapshot-import",
                "--snapshot-file",
                str(snapshot_path),
                "--snapshot-trust-mode",
                "enforce",
                "--snapshot-trusted-key",
                public_key_hex,
            ]
        )

        imported_service = _make_service(target_db)
        assert export_code == 0
        assert export_payload["snapshot_block_hash"] == mined.block_hash()
        assert sign_code == 0
        assert sign_payload["signer_public_key_hex"] == public_key_hex
        assert sign_payload["signature_count"] == 1
        assert import_code == 0
        assert import_payload["trusted_signature_count"] == 1
        assert imported_service.chain_tip() is not None
        assert imported_service.chain_tip().block_hash == mined.block_hash()


def test_cli_snapshot_import_warn_mode_emits_warning_for_unsigned_snapshot() -> None:
    with TemporaryDirectory() as tempdir:
        source_db = Path(tempdir) / "source.sqlite3"
        target_db = Path(tempdir) / "target.sqlite3"
        snapshot_path = Path(tempdir) / "chain.snapshot.json"
        source = _make_service(source_db)
        mined = _mine_block(source.build_candidate_block("CHCminer").block)
        source.apply_block(mined)

        export_code, _ = _run_cli(["--data", str(source_db), "snapshot-export", "--snapshot-file", str(snapshot_path)])
        import_code, stdout, stderr = _run_cli_with_stderr(
            [
                "--data",
                str(target_db),
                "snapshot-import",
                "--snapshot-file",
                str(snapshot_path),
                "--snapshot-trust-mode",
                "warn",
            ]
        )

        imported_payload = json.loads(stdout)
        warning_payload = json.loads(stderr)
        assert export_code == 0
        assert import_code == 0
        assert imported_payload["warnings"] == ["snapshot_unsigned_but_accepted_due_to_warn_mode"]
        assert "continued only because --snapshot-trust-mode=warn" in warning_payload["warning"]


def test_cli_snapshot_import_reads_trusted_keys_file() -> None:
    with TemporaryDirectory() as tempdir:
        source_db = Path(tempdir) / "source.sqlite3"
        target_db = Path(tempdir) / "target.sqlite3"
        snapshot_path = Path(tempdir) / "chain.snapshot.json"
        keys_path = Path(tempdir) / "trusted-keys.json"
        source = _make_service(source_db)
        mined = _mine_block(source.build_candidate_block("CHCminer").block)
        source.apply_block(mined)
        private_key_hex, public_key_hex = _ed25519_private_key_hex()

        _run_cli(["--data", str(source_db), "snapshot-export", "--snapshot-file", str(snapshot_path)])
        _run_cli(
            [
                "snapshot-sign",
                "--snapshot-file",
                str(snapshot_path),
                "--private-key-hex",
                private_key_hex,
            ]
        )
        keys_path.write_text(json.dumps({"trusted_keys": [public_key_hex]}), encoding="utf-8")

        import_code, import_payload = _run_cli(
            [
                "--data",
                str(target_db),
                "snapshot-import",
                "--snapshot-file",
                str(snapshot_path),
                "--snapshot-trust-mode",
                "enforce",
                "--snapshot-trusted-keys-file",
                str(keys_path),
            ]
        )

        assert import_code == 0
        assert import_payload["trusted_signature_count"] == 1


def test_cli_add_peer_and_list_peers() -> None:
    with TemporaryDirectory() as tempdir:
        db_path = Path(tempdir) / "chipcoin.sqlite3"

        add_code, add_payload = _run_cli(["--data", str(db_path), "add-peer", "127.0.0.1", "8333"])
        list_code, list_payload = _run_cli(["--data", str(db_path), "list-peers"])

        assert add_code == 0
        assert add_payload["host"] == "127.0.0.1"
        assert list_code == 0
        assert len(list_payload) == 1
        peer = list_payload[0]
        assert peer["host"] == "127.0.0.1"
        assert peer["port"] == 8333
        assert peer["network"] == "mainnet"
        assert peer["source"] == "manual"
        assert peer["peer_state"] == "manual"
        assert isinstance(peer["first_seen"], int)
        assert isinstance(peer["last_seen"], int)
        assert peer["ban_until"] is None
        assert peer["banned"] is False


def test_cli_list_peers_and_peer_detail_show_protocol_error_class() -> None:
    with TemporaryDirectory() as tempdir:
        db_path = Path(tempdir) / "chipcoin.sqlite3"
        service = _make_service(db_path)
        service.record_peer_observation(
            host="127.0.0.1",
            port=18444,
            direction="outbound",
            handshake_complete=False,
            node_id="peer-a",
            score=-25,
            reconnect_attempts=2,
            backoff_until=1_700_000_123,
            last_error="Unexpected network magic.",
            last_error_at=1_700_000_124,
            protocol_error_class="wrong_network_magic",
            disconnect_count=3,
            session_started_at=1_700_000_120,
            last_known_height=42,
            misbehavior_score=60,
            misbehavior_last_updated_at=1_700_000_124,
            ban_until=2_700_000_224,
            last_penalty_reason="wrong_network_magic",
            last_penalty_at=1_700_000_124,
        )

        list_code, list_payload = _run_cli(["--data", str(db_path), "list-peers"])
        detail_code, detail_payload = _run_cli(["--data", str(db_path), "peer-detail", "--node-id", "peer-a"])

        assert list_code == 0
        assert detail_code == 0
        assert len(list_payload) == 1
        assert list_payload[0] == detail_payload
        assert list_payload[0]["host"] == "127.0.0.1"
        assert list_payload[0]["port"] == 18444
        assert list_payload[0]["network"] == "mainnet"
        assert list_payload[0]["network_magic_hex"] == "f9beb4d9"
        assert list_payload[0]["direction"] == "outbound"
        assert list_payload[0]["source"] is None
        assert list_payload[0]["peer_state"] == "banned"
        assert list_payload[0]["first_seen"] == 1_700_000_000
        assert list_payload[0]["node_id"] == "peer-a"
        assert list_payload[0]["handshake_complete"] is False
        assert list_payload[0]["last_success"] is None
        assert list_payload[0]["last_failure"] is None
        assert list_payload[0]["failure_count"] is None
        assert list_payload[0]["success_count"] is None
        assert list_payload[0]["score"] == -25
        assert list_payload[0]["reconnect_attempts"] == 2
        assert list_payload[0]["backoff_until"] == 1_700_000_123
        assert isinstance(list_payload[0]["backoff_remaining_seconds"], int)
        assert list_payload[0]["backoff_remaining_seconds"] >= 0
        assert list_payload[0]["last_seen"] == 1_700_000_000
        assert list_payload[0]["session_started_at"] == 1_700_000_120
        assert list_payload[0]["last_known_height"] == 42
        assert list_payload[0]["disconnect_count"] == 3
        assert list_payload[0]["last_error"] == "Unexpected network magic."
        assert list_payload[0]["last_error_at"] == 1_700_000_124
        assert list_payload[0]["last_penalty_at"] == 1_700_000_124
        assert list_payload[0]["last_penalty_reason"] == "wrong_network_magic"
        assert list_payload[0]["protocol_error_class"] == "wrong_network_magic"
        assert list_payload[0]["misbehavior_last_updated_at"] == 1_700_000_124
        assert list_payload[0]["misbehavior_score"] == 60
        assert list_payload[0]["ban_until"] == 2_700_000_224
        assert isinstance(list_payload[0]["ban_remaining_seconds"], int)
        assert list_payload[0]["ban_remaining_seconds"] > 0
        assert list_payload[0]["banned"] is True


def test_cli_peer_summary_aggregates_error_classes_and_worst_peers() -> None:
    with TemporaryDirectory() as tempdir:
        db_path = Path(tempdir) / "chipcoin.sqlite3"
        service = _make_service(db_path)
        service.record_peer_observation(
            host="127.0.0.1",
            port=18444,
            direction="outbound",
            handshake_complete=False,
            node_id="peer-a",
            score=-25,
            reconnect_attempts=2,
            backoff_until=1_700_000_123,
            last_error="Unexpected network magic.",
            last_error_at=1_700_000_124,
            protocol_error_class="wrong_network_magic",
            disconnect_count=3,
            session_started_at=1_700_000_120,
            last_known_height=42,
            misbehavior_score=60,
            misbehavior_last_updated_at=1_700_000_124,
            ban_until=2_700_000_224,
            last_penalty_reason="wrong_network_magic",
            last_penalty_at=1_700_000_124,
        )
        service.record_peer_observation(
            host="127.0.0.2",
            port=18445,
            direction="inbound",
            handshake_complete=True,
            node_id="peer-b",
            score=-5,
            reconnect_attempts=0,
            backoff_until=0,
            last_error="Duplicate peer connection.",
            last_error_at=1_700_000_125,
            protocol_error_class="duplicate_connection",
            disconnect_count=7,
            session_started_at=1_700_000_121,
            last_known_height=43,
            misbehavior_score=10,
            misbehavior_last_updated_at=1_700_000_125,
            last_penalty_reason="duplicate_connection",
            last_penalty_at=1_700_000_125,
        )

        code, payload = _run_cli(["--data", str(db_path), "peer-summary"])

        assert code == 0
        assert payload["error_class_counts"] == {
            "duplicate_connection": 1,
            "wrong_network_magic": 1,
        }
        assert payload["peer_count_by_network"] == {"mainnet": 2}
        assert payload["peer_count_by_direction"] == {"inbound": 1, "outbound": 1}
        assert payload["peer_count_by_source"] == {}
        assert payload["peer_count_by_state"] == {"banned": 1, "questionable": 1}
        assert payload["peer_count_by_handshake_status"] == {"complete": 1, "incomplete": 1, "unknown": 0}
        assert payload["good_peer_count"] == 0
        assert payload["questionable_peer_count"] == 1
        assert payload["manual_peer_count"] == 0
        assert payload["seed_peer_count"] == 0
        assert payload["discovered_peer_count"] == 0
        assert payload["non_banned_peer_count"] == 1
        assert payload["backoff_peer_count"] == 1
        assert payload["banned_peer_count"] == 1
        assert payload["penalty_reason_counts"] == {
            "duplicate_connection": 1,
            "wrong_network_magic": 1,
        }
        assert payload["operator_summary"] == {
            "peer_health": "degraded",
            "non_banned_peer_count": 1,
            "active_backoff_peer_count": 1,
            "active_ban_count": 1,
            "warnings": ["backoff_peers_present"],
        }
        assert payload["highest_misbehavior_peer"]["node_id"] == "peer-a"
        assert payload["worst_score_peer"]["node_id"] == "peer-a"
        assert payload["most_disconnected_peer"]["node_id"] == "peer-b"
        assert payload["most_recent_error_peer"]["node_id"] == "peer-b"


def test_cli_run_emits_warning_for_empty_peerbook_and_no_peers(caplog) -> None:
    with TemporaryDirectory() as tempdir:
        db_path = Path(tempdir) / "chipcoin.sqlite3"
        service = _make_service(db_path)
        args = cli_module._build_parser().parse_args(
            [
                "--data",
                str(db_path),
                "run",
                "--run-seconds",
                "0",
            ]
        )

        with caplog.at_level(logging.WARNING, logger="chipcoin.runtime.config"):
            cli_module._emit_runtime_warnings(service, args, [])

        assert "empty peerbook" in caplog.text


def test_cli_run_emits_warning_for_suspicious_block_timeout(caplog) -> None:
    with TemporaryDirectory() as tempdir:
        db_path = Path(tempdir) / "chipcoin.sqlite3"
        service = _make_service(db_path)
        args = cli_module._build_parser().parse_args(
            [
                "--data",
                str(db_path),
                "run",
                "--run-seconds",
                "0",
                "--block-request-timeout-seconds",
                "3",
            ]
        )

        with caplog.at_level(logging.WARNING, logger="chipcoin.runtime.config"):
            cli_module._emit_runtime_warnings(service, args, [])

        assert "unusually low" in caplog.text


def test_cli_peerbook_clean_prunes_ephemeral_discovered_peers_and_can_reset_penalties() -> None:
    with TemporaryDirectory() as tempdir:
        db_path = Path(tempdir) / "chipcoin-devnet.sqlite3"
        service = NodeService.open_sqlite(db_path, network="devnet")
        service.record_peer_observation(
            host="188.217.94.86",
            port=58236,
            source="discovered",
            handshake_complete=False,
            score=-20,
            reconnect_attempts=2,
            backoff_until=1_700_000_123,
            last_error="Handshake timed out.",
            last_error_at=1_700_000_124,
            protocol_error_class="handshake_failed",
            misbehavior_score=10,
            misbehavior_last_updated_at=1_700_000_124,
            ban_until=1_700_000_200,
            last_penalty_reason="timeout",
            last_penalty_at=1_700_000_124,
        )
        service.record_peer_observation(
            host="tiltmediaconsulting.com",
            port=18444,
            source="manual",
            handshake_complete=True,
            score=-15,
            reconnect_attempts=1,
            backoff_until=1_700_000_123,
            misbehavior_score=5,
            misbehavior_last_updated_at=1_700_000_124,
            ban_until=1_700_000_200,
            last_penalty_reason="timeout",
            last_penalty_at=1_700_000_124,
        )

        code, payload = _run_cli(["--network", "devnet", "--data", str(db_path), "peerbook-clean", "--reset-penalties"])

        assert code == 0
        assert payload["removed_count"] == 1
        assert payload["removed"] == [
            {"host": "188.217.94.86", "port": 58236, "reason": "noncanonical_discovered_port"}
        ]
        assert payload["penalties_reset_count"] == 1
        peers = NodeService.open_sqlite(db_path, network="devnet").list_peers()
        assert not any(peer.host == "188.217.94.86" and peer.port == 58236 for peer in peers)
        manual = next(peer for peer in peers if peer.host == "tiltmediaconsulting.com" and peer.port == 18444)
        assert manual.score == 0
        assert manual.reconnect_attempts == 0
        assert manual.backoff_until == 0
        assert manual.misbehavior_score == 0
        assert manual.ban_until is None


def test_cli_peerbook_clean_supports_dry_run() -> None:
    with TemporaryDirectory() as tempdir:
        db_path = Path(tempdir) / "chipcoin-devnet.sqlite3"
        service = NodeService.open_sqlite(db_path, network="devnet")
        service.record_peer_observation(
            host="188.217.94.86",
            port=58236,
            source="discovered",
            handshake_complete=False,
        )

        code, payload = _run_cli(["--network", "devnet", "--data", str(db_path), "peerbook-clean", "--dry-run"])

        assert code == 0
        assert payload["dry_run"] is True
        assert payload["removed_count"] == 1
        peers = NodeService.open_sqlite(db_path, network="devnet").list_peers()
        assert any(peer.host == "188.217.94.86" and peer.port == 58236 for peer in peers)


def test_cli_mempool_difficulty_and_chain_window() -> None:
    with TemporaryDirectory() as tempdir:
        db_path = Path(tempdir) / "chipcoin.sqlite3"
        service = _make_service(db_path)
        funding_outpoint = OutPoint(txid="aa" * 32, index=0)
        put_wallet_utxo(service, funding_outpoint, value=100, owner=wallet_key(0))
        transaction = signed_payment(funding_outpoint, value=100, sender=wallet_key(0), fee=10)
        service.receive_transaction(transaction)
        mined = _mine_block(service.build_candidate_block("CHCminer").block)
        service.apply_block(mined)

        mempool_code, mempool_payload = _run_cli(["--data", str(db_path), "mempool"])
        difficulty_code, difficulty_payload = _run_cli(["--data", str(db_path), "difficulty"])
        window_code, window_payload = _run_cli(["--data", str(db_path), "chain-window", "--start", "0", "--end", "0"])

        assert mempool_code == 0
        assert mempool_payload == []
        assert difficulty_code == 0
        assert difficulty_payload["current_bits"] == mined.header.bits
        assert difficulty_payload["next_retarget_height"] == 1000
        assert window_code == 0
        assert len(window_payload) == 1
        assert window_payload[0]["height"] == 0
        assert window_payload[0]["block_hash"] == mined.block_hash()


def test_cli_mempool_lists_fee_rate_and_dependencies() -> None:
    with TemporaryDirectory() as tempdir:
        db_path = Path(tempdir) / "chipcoin.sqlite3"
        service = _make_service(db_path)
        parent_outpoint = OutPoint(txid="bb" * 32, index=0)
        put_wallet_utxo(service, parent_outpoint, value=100, owner=wallet_key(0))
        parent = signed_payment(parent_outpoint, value=100, sender=wallet_key(0), amount=90, fee=10)
        child = signed_payment(
            OutPoint(txid=parent.txid(), index=0),
            value=90,
            sender=wallet_key(1),
            amount=80,
            fee=10,
        )
        service.receive_transaction(parent)
        service.receive_transaction(child)

        code, payload = _run_cli(["--data", str(db_path), "mempool"])

        assert code == 0
        assert {entry["txid"] for entry in payload} == {parent.txid(), child.txid()}
        by_txid = {entry["txid"]: entry for entry in payload}
        assert by_txid[parent.txid()]["fee_chipbits"] == 10
        assert by_txid[parent.txid()]["weight_units"] > 0
        assert by_txid[child.txid()]["depends_on"] == [parent.txid()]


def test_cli_utxos_and_balance_with_zero_utxos() -> None:
    with TemporaryDirectory() as tempdir:
        db_path = Path(tempdir) / "chipcoin.sqlite3"
        address = wallet_key(0).address

        utxos_code, utxos_payload = _run_cli(["--data", str(db_path), "utxos", "--address", address])
        balance_code, balance_payload = _run_cli(["--data", str(db_path), "balance", "--address", address])

        assert utxos_code == 0
        assert utxos_payload == []
        assert balance_code == 0
        assert balance_payload["confirmed_balance_chipbits"] == 0
        assert balance_payload["confirmed_balance_chc"] == "0.00000000"
        assert balance_payload["utxo_count"] == 0


def test_cli_utxos_and_balance_report_coinbase_maturity() -> None:
    with TemporaryDirectory() as tempdir:
        db_path = Path(tempdir) / "chipcoin.sqlite3"
        service = _make_service(db_path)
        owner = wallet_key(0)
        mature_outpoint = OutPoint(txid="cc" * 32, index=0)
        immature_outpoint = OutPoint(txid="dd" * 32, index=0)
        put_wallet_utxo(service, mature_outpoint, value=500, owner=owner, height=0, is_coinbase=False)
        put_wallet_utxo(service, immature_outpoint, value=700, owner=owner, height=0, is_coinbase=True)
        mined = _mine_block(service.build_candidate_block("CHCminer").block)
        service.apply_block(mined)

        utxos_code, utxos_payload = _run_cli(["--data", str(db_path), "utxos", "--address", owner.address])
        balance_code, balance_payload = _run_cli(["--data", str(db_path), "balance", "--address", owner.address])

        assert utxos_code == 0
        assert len(utxos_payload) == 2
        by_txid = {entry["txid"]: entry for entry in utxos_payload}
        assert by_txid[mature_outpoint.txid]["mature"] is True
        assert by_txid[immature_outpoint.txid]["coinbase"] is True
        assert by_txid[immature_outpoint.txid]["mature"] is False
        assert by_txid[immature_outpoint.txid]["amount_chc"] == "0.00000700"
        assert balance_code == 0
        assert balance_payload["confirmed_balance_chipbits"] == 1200
        assert balance_payload["immature_balance_chipbits"] == 700
        assert balance_payload["spendable_balance_chipbits"] == 500
        assert balance_payload["confirmed_balance_chc"] == "0.00001200"


def test_cli_node_registry_reports_active_and_inactive_entries() -> None:
    with TemporaryDirectory() as tempdir:
        db_path = Path(tempdir) / "chipcoin.sqlite3"
        params = replace(MAINNET_PARAMS, epoch_length_blocks=2)
        timestamps = iter(range(1_700_000_000, 1_700_000_200))
        service = NodeService.open_sqlite(db_path, params=params, time_provider=lambda: next(timestamps))
        for _ in range(3):
            mined = _mine_block(service.build_candidate_block("CHCminer").block)
            service.apply_block(mined)
        service.node_registry.upsert(
            NodeRecord(
                node_id="node-active",
                payout_address=wallet_key(0).address,
                owner_pubkey=wallet_key(0).public_key,
                registered_height=0,
                last_renewed_height=2,
            )
        )
        service.node_registry.upsert(
            NodeRecord(
                node_id="node-stale",
                payout_address=wallet_key(1).address,
                owner_pubkey=wallet_key(1).public_key,
                registered_height=0,
                last_renewed_height=0,
            )
        )
        original_open_sqlite = cli_module.NodeService.open_sqlite
        cli_module.NodeService.open_sqlite = lambda _path, **_kwargs: service
        try:
            code, payload = _run_cli(["--data", str(db_path), "node-registry"])
        finally:
            cli_module.NodeService.open_sqlite = original_open_sqlite

        assert code == 0
        by_id = {entry["node_id"]: entry for entry in payload}
        assert by_id["node-active"]["active"] is True
        assert by_id["node-active"]["eligible_from_height"] == 3
        assert by_id["node-stale"]["active"] is False
        assert by_id["node-stale"]["epoch_status"] == "stale"


def test_cli_next_winners_reports_less_than_ten_nodes() -> None:
    with TemporaryDirectory() as tempdir:
        db_path = Path(tempdir) / "chipcoin.sqlite3"
        service = _make_service(db_path)
        for index in range(3):
            key = wallet_key(index)
            service.node_registry.upsert(
                NodeRecord(
                    node_id=f"node-{index}",
                    payout_address=key.address,
                    owner_pubkey=key.public_key,
                    registered_height=0,
                    last_renewed_height=0,
                )
            )

        code, payload = _run_cli(["--data", str(db_path), "next-winners"])

        assert code == 0
        assert payload["next_block_height"] == 0
        assert payload["active_nodes_count"] == 0
        assert payload["selected_winners"] == []

        mined = _mine_block(service.build_candidate_block("CHCminer").block)
        service.apply_block(mined)
        code, payload = _run_cli(["--data", str(db_path), "next-winners"])
        assert code == 0
        assert payload["next_block_height"] == 1
        assert payload["active_nodes_count"] == 3
        assert len(payload["selected_winners"]) == 3
        assert payload["reward_per_winner_chipbits"] == 66_666_666
        assert payload["reward_per_winner_chc"] == "0.66666666"
        assert payload["remainder_to_miner_chipbits"] == 2


def test_cli_next_winners_caps_selected_winners_at_ten() -> None:
    with TemporaryDirectory() as tempdir:
        db_path = Path(tempdir) / "chipcoin.sqlite3"
        service = _make_service(db_path)
        keys = [wallet_key(index % 3) for index in range(12)]
        for index in range(12):
            key = keys[index]
            service.node_registry.upsert(
                NodeRecord(
                    node_id=f"node-{index:02d}",
                    payout_address=key.address,
                    owner_pubkey=bytes.fromhex(f"{index+1:064x}"),
                    registered_height=0,
                    last_renewed_height=0,
                )
            )
        mined = _mine_block(service.build_candidate_block("CHCminer").block)
        service.apply_block(mined)

        code, payload = _run_cli(["--data", str(db_path), "next-winners"])

        assert code == 0
        assert payload["active_nodes_count"] == 12
        assert len(payload["selected_winners"]) == 10
        assert payload["reward_per_winner_chipbits"] == 20_000_000
        assert payload["reward_per_winner_chc"] == "0.20000000"


def test_cli_reward_history_for_miner_address_and_empty_case() -> None:
    with TemporaryDirectory() as tempdir:
        db_path = Path(tempdir) / "chipcoin.sqlite3"
        service = _make_service(db_path)
        miner_address = wallet_key(0).address
        mined = _mine_block(service.build_candidate_block(miner_address).block)
        service.apply_block(mined)

        code, payload = _run_cli(["--data", str(db_path), "reward-history", "--address", miner_address])
        empty_code, empty_payload = _run_cli(["--data", str(db_path), "reward-history", "--address", wallet_key(1).address])

        assert code == 0
        assert len(payload) == 1
        assert payload[0]["reward_type"] == "miner_subsidy"
        assert payload[0]["amount_chipbits"] == 2_200_000_000
        assert payload[0]["amount_chc"] == "22.00000000"
        assert payload[0]["mature"] is False
        assert empty_code == 0
        assert empty_payload == []


def test_cli_reward_history_for_node_reward_address() -> None:
    with TemporaryDirectory() as tempdir:
        db_path = Path(tempdir) / "chipcoin.sqlite3"
        service = _make_service(db_path)
        for index in range(3):
            key = wallet_key(index)
            service.node_registry.upsert(
                NodeRecord(
                    node_id=f"node-{index}",
                    payout_address=key.address,
                    owner_pubkey=key.public_key,
                    registered_height=0,
                    last_renewed_height=0,
                )
            )
        service.apply_block(_mine_block(service.build_candidate_block("CHCminer").block))
        service.apply_block(_mine_block(service.build_candidate_block("CHCminer").block))

        code, payload = _run_cli(["--data", str(db_path), "reward-history", "--address", wallet_key(1).address])

        assert code == 0
        assert any(entry["reward_type"] == "node_reward" for entry in payload)
        node_reward_entry = next(entry for entry in payload if entry["reward_type"] == "node_reward")
        assert node_reward_entry["amount_chipbits"] == 66_666_666
        assert node_reward_entry["amount_chc"] == "0.66666666"


def test_cli_reward_summary_for_miner_and_node_addresses() -> None:
    with TemporaryDirectory() as tempdir:
        db_path = Path(tempdir) / "chipcoin.sqlite3"
        service = _make_service(db_path)
        miner_address = wallet_key(0).address
        for index in range(3):
            key = wallet_key(index)
            service.node_registry.upsert(
                NodeRecord(
                    node_id=f"node-{index}",
                    payout_address=key.address,
                    owner_pubkey=key.public_key,
                    registered_height=0,
                    last_renewed_height=0,
                )
            )
        service.apply_block(_mine_block(service.build_candidate_block(miner_address).block))
        service.apply_block(_mine_block(service.build_candidate_block(miner_address).block))

        miner_code, miner_payload = _run_cli(["--data", str(db_path), "reward-summary", "--address", miner_address])
        node_code, node_payload = _run_cli(["--data", str(db_path), "reward-summary", "--address", wallet_key(1).address])

        assert miner_code == 0
        assert miner_payload["address"] == miner_address
        assert miner_payload["total_rewards_chipbits"] > 0
        assert miner_payload["total_miner_subsidy_chipbits"] > 0
        assert miner_payload["total_node_rewards_chipbits"] == 66_666_666
        assert miner_payload["payout_count"] >= 2
        assert node_code == 0
        assert node_payload["total_node_rewards_chipbits"] == 66_666_666
        assert node_payload["total_miner_subsidy_chipbits"] == 0


def test_cli_node_income_summary_for_active_and_inactive_node() -> None:
    with TemporaryDirectory() as tempdir:
        db_path = Path(tempdir) / "chipcoin.sqlite3"
        params = replace(MAINNET_PARAMS, epoch_length_blocks=2)
        timestamps = iter(range(1_700_000_000, 1_700_000_200))
        service = NodeService.open_sqlite(db_path, params=params, time_provider=lambda: next(timestamps))
        for index in range(3):
            service.apply_block(_mine_block(service.build_candidate_block("CHCminer").block))
        service.node_registry.upsert(
            NodeRecord(
                node_id="node-active",
                payout_address=wallet_key(0).address,
                owner_pubkey=wallet_key(0).public_key,
                registered_height=0,
                last_renewed_height=2,
            )
        )
        service.node_registry.upsert(
            NodeRecord(
                node_id="node-inactive",
                payout_address=wallet_key(1).address,
                owner_pubkey=wallet_key(1).public_key,
                registered_height=0,
                last_renewed_height=0,
            )
        )
        original_open_sqlite = cli_module.NodeService.open_sqlite
        cli_module.NodeService.open_sqlite = lambda _path, **_kwargs: service
        try:
            code, payload = _run_cli(["--data", str(db_path), "node-income-summary"])
            single_code, single_payload = _run_cli(
                ["--data", str(db_path), "node-income-summary", "--node-id", "node-active"]
            )
        finally:
            cli_module.NodeService.open_sqlite = original_open_sqlite

        assert code == 0
        by_id = {entry["node_id"]: entry for entry in payload}
        assert by_id["node-active"]["active"] is True
        assert by_id["node-inactive"]["active"] is False
        assert single_code == 0
        assert len(single_payload) == 1
        assert single_payload[0]["node_id"] == "node-active"


def test_cli_mining_history_matches_reward_history_for_miner() -> None:
    with TemporaryDirectory() as tempdir:
        db_path = Path(tempdir) / "chipcoin.sqlite3"
        service = _make_service(db_path)
        miner_address = wallet_key(0).address
        first = _mine_block(service.build_candidate_block(miner_address).block)
        service.apply_block(first)
        second = _mine_block(service.build_candidate_block(miner_address).block)
        service.apply_block(second)

        mining_code, mining_payload = _run_cli(["--data", str(db_path), "mining-history", "--address", miner_address])
        reward_code, reward_payload = _run_cli(["--data", str(db_path), "reward-history", "--address", miner_address])

        assert mining_code == 0
        assert reward_code == 0
        assert len(mining_payload) == 2
        assert mining_payload[0]["miner_subsidy_chipbits"] == 2_000_000_000
        assert mining_payload[0]["miner_subsidy_chc"] == "20.00000000"
        assert any(entry["reward_type"] == "miner_subsidy" for entry in reward_payload)


def test_cli_economy_summary_and_supply_diagnostics_with_zero_data() -> None:
    with TemporaryDirectory() as tempdir:
        db_path = Path(tempdir) / "chipcoin.sqlite3"

        economy_code, economy_payload = _run_cli(["--data", str(db_path), "economy-summary"])
        supply_code, supply_payload = _run_cli(["--data", str(db_path), "supply-diagnostics"])

        assert economy_code == 0
        assert economy_payload["current_height"] is None
        assert economy_payload["registered_nodes_count"] == 0
        assert economy_payload["active_nodes_count"] == 0
        assert economy_payload["total_emitted_supply_chipbits"] == 0
        assert economy_payload["remaining_supply_chipbits"] == MAINNET_PARAMS.max_money_chipbits
        assert supply_code == 0
        assert supply_payload["confirmed_unspent_supply_chipbits"] == 0
        assert supply_payload["total_utxo_count"] == 0


def test_cli_top_miners_and_top_recipients() -> None:
    with TemporaryDirectory() as tempdir:
        db_path = Path(tempdir) / "chipcoin.sqlite3"
        service = _make_service(db_path)
        miner_a = wallet_key(0).address
        miner_b = wallet_key(1).address
        service.apply_block(_mine_block(service.build_candidate_block(miner_a).block))
        service.apply_block(_mine_block(service.build_candidate_block(miner_b).block))
        service.apply_block(_mine_block(service.build_candidate_block(miner_a).block))

        top_miners_code, top_miners_payload = _run_cli(["--data", str(db_path), "top-miners"])
        top_recipients_code, top_recipients_payload = _run_cli(["--data", str(db_path), "top-recipients"])

        assert top_miners_code == 0
        assert top_miners_payload[0]["miner_address"] == miner_a
        assert top_miners_payload[0]["blocks_mined"] == 2
        assert top_miners_payload[0]["total_miner_subsidy_chipbits"] == 4_000_000_000
        assert top_miners_payload[1]["miner_address"] == miner_b
        assert top_recipients_code == 0
        assert top_recipients_payload[0]["address"] == miner_a
        assert top_recipients_payload[0]["total_rewards_chipbits"] >= top_recipients_payload[1]["total_rewards_chipbits"]


def test_cli_top_nodes_and_node_income_summary_with_rewards() -> None:
    with TemporaryDirectory() as tempdir:
        db_path = Path(tempdir) / "chipcoin.sqlite3"
        service = _make_service(db_path)
        for index in range(3):
            key = wallet_key(index)
            service.node_registry.upsert(
                NodeRecord(
                    node_id=f"node-{index}",
                    payout_address=key.address,
                    owner_pubkey=key.public_key,
                    registered_height=0,
                    last_renewed_height=0,
                )
            )
        service.apply_block(_mine_block(service.build_candidate_block(wallet_key(0).address).block))
        service.apply_block(_mine_block(service.build_candidate_block(wallet_key(0).address).block))

        top_nodes_code, top_nodes_payload = _run_cli(["--data", str(db_path), "top-nodes"])
        node_income_code, node_income_payload = _run_cli(
            ["--data", str(db_path), "node-income-summary", "--address", wallet_key(1).address]
        )

        assert top_nodes_code == 0
        assert top_nodes_payload
        assert top_nodes_payload[0]["total_node_rewards_chipbits"] == 66_666_666
        assert node_income_code == 0
        assert len(node_income_payload) == 1
        assert node_income_payload[0]["payout_address"] == wallet_key(1).address


def test_cli_supply_diagnostics_reflects_immature_coinbase() -> None:
    with TemporaryDirectory() as tempdir:
        db_path = Path(tempdir) / "chipcoin.sqlite3"
        service = _make_service(db_path)
        service.apply_block(_mine_block(service.build_candidate_block(wallet_key(0).address).block))

        economy_code, economy_payload = _run_cli(["--data", str(db_path), "economy-summary"])
        supply_code, supply_payload = _run_cli(["--data", str(db_path), "supply-diagnostics"])

        assert economy_code == 0
        assert economy_payload["total_emitted_supply_chipbits"] == 2_200_000_000
        assert economy_payload["circulating_spendable_supply_chipbits"] == 0
        assert economy_payload["immature_supply_chipbits"] == 2_200_000_000
        assert supply_code == 0
        assert supply_payload["confirmed_unspent_supply_chipbits"] == 2_200_000_000
        assert supply_payload["immature_utxo_count"] == 1


def test_cli_wallet_shortcuts_match_utxos_and_balance() -> None:
    with TemporaryDirectory() as tempdir:
        db_path = Path(tempdir) / "chipcoin.sqlite3"
        wallet_path = Path(tempdir) / "wallet.json"
        service = _make_service(db_path)
        owner = wallet_key(0)
        cli_module._save_wallet_key(wallet_path, owner)
        put_wallet_utxo(service, OutPoint(txid="ee" * 32, index=0), value=500, owner=owner, height=0, is_coinbase=False)

        utxos_code, utxos_payload = _run_cli(["--data", str(db_path), "utxos", "--address", owner.address])
        wallet_utxos_code, wallet_utxos_payload = _run_cli(
            ["--data", str(db_path), "wallet-utxos", "--wallet-file", str(wallet_path)]
        )
        balance_code, balance_payload = _run_cli(["--data", str(db_path), "balance", "--address", owner.address])
        wallet_balance_code, wallet_balance_payload = _run_cli(
            ["--data", str(db_path), "wallet-balance", "--wallet-file", str(wallet_path)]
        )

        assert utxos_code == wallet_utxos_code == 0
        assert balance_code == wallet_balance_code == 0
        assert utxos_payload == wallet_utxos_payload
        assert balance_payload == wallet_balance_payload


def test_cli_register_node_and_renew_node_flow() -> None:
    with TemporaryDirectory() as tempdir:
        db_path = Path(tempdir) / "chipcoin.sqlite3"
        wallet_path = Path(tempdir) / "wallet.json"
        service = _make_service(db_path)
        owner = wallet_key(0)
        cli_module._save_wallet_key(wallet_path, owner)

        register_code, register_payload = _run_cli(
            [
                "--data",
                str(db_path),
                "register-node",
                "--wallet-file",
                str(wallet_path),
                "--node-id",
                "node-a",
                "--payout-address",
                owner.address,
            ]
        )
        registry_before_code, registry_before_payload = _run_cli(["--data", str(db_path), "node-registry"])
        winners_before_code, winners_before_payload = _run_cli(["--data", str(db_path), "next-winners"])

        assert register_code == 0
        assert register_payload["node_id"] == "node-a"
        assert register_payload["payout_address"] == owner.address
        assert register_payload["submitted"] is True
        assert registry_before_code == 0
        assert registry_before_payload == []
        assert winners_before_code == 0
        assert winners_before_payload["active_nodes_count"] == 0

        service.apply_block(_mine_block(service.build_candidate_block("CHCminer").block))

        registry_after_code, registry_after_payload = _run_cli(["--data", str(db_path), "node-registry"])
        winners_after_code, winners_after_payload = _run_cli(["--data", str(db_path), "next-winners"])
        renew_code, renew_payload = _run_cli(
            [
                "--data",
                str(db_path),
                "renew-node",
                "--wallet-file",
                str(wallet_path),
                "--node-id",
                "node-a",
            ]
        )

        assert registry_after_code == 0
        assert len(registry_after_payload) == 1
        assert registry_after_payload[0]["node_id"] == "node-a"
        assert registry_after_payload[0]["eligible_from_height"] == 1
        assert winners_after_code == 0
        assert winners_after_payload["active_nodes_count"] == 1
        assert winners_after_payload["selected_winners"][0]["node_id"] == "node-a"
        assert renew_code == 0
        assert renew_payload["node_id"] == "node-a"
        assert renew_payload["submitted"] is True


def test_cli_register_node_rejects_duplicate_node_id() -> None:
    with TemporaryDirectory() as tempdir:
        db_path = Path(tempdir) / "chipcoin.sqlite3"
        wallet_a_path = Path(tempdir) / "wallet-a.json"
        wallet_b_path = Path(tempdir) / "wallet-b.json"
        service = _make_service(db_path)
        cli_module._save_wallet_key(wallet_a_path, wallet_key(0))
        cli_module._save_wallet_key(wallet_b_path, wallet_key(1))

        first_code, _first_payload = _run_cli(
            [
                "--data",
                str(db_path),
                "register-node",
                "--wallet-file",
                str(wallet_a_path),
                "--node-id",
                "node-dup",
                "--payout-address",
                wallet_key(0).address,
            ]
        )
        service.apply_block(_mine_block(service.build_candidate_block("CHCminer").block))
        second_code, _stdout, second_stderr = _run_cli_with_stderr(
            [
                "--data",
                str(db_path),
                "register-node",
                "--wallet-file",
                str(wallet_b_path),
                "--node-id",
                "node-dup",
                "--payout-address",
                wallet_key(1).address,
            ]
        )

        assert first_code == 0
        assert second_code == 1
        assert "already registered" in json.loads(second_stderr)["error"]


def test_cli_renew_node_rejects_wrong_owner() -> None:
    with TemporaryDirectory() as tempdir:
        db_path = Path(tempdir) / "chipcoin.sqlite3"
        owner_path = Path(tempdir) / "owner.json"
        other_path = Path(tempdir) / "other.json"
        service = _make_service(db_path)
        cli_module._save_wallet_key(owner_path, wallet_key(0))
        cli_module._save_wallet_key(other_path, wallet_key(1))

        register_code, _register_payload = _run_cli(
            [
                "--data",
                str(db_path),
                "register-node",
                "--wallet-file",
                str(owner_path),
                "--node-id",
                "node-owner",
                "--payout-address",
                wallet_key(0).address,
            ]
        )
        service.apply_block(_mine_block(service.build_candidate_block("CHCminer").block))
        renew_code, _stdout, renew_stderr = _run_cli_with_stderr(
            [
                "--data",
                str(db_path),
                "renew-node",
                "--wallet-file",
                str(other_path),
                "--node-id",
                "node-owner",
            ]
        )

        assert register_code == 0
        assert renew_code == 1
        assert "registered node owner" in json.loads(renew_stderr)["error"]


def test_cli_address_history_reports_confirmed_incoming_and_outgoing() -> None:
    with TemporaryDirectory() as tempdir:
        db_path = Path(tempdir) / "chipcoin.sqlite3"
        params = replace(MAINNET_PARAMS, coinbase_maturity=0)
        timestamps = iter(range(1_700_000_000, 1_700_000_200))
        service = NodeService.open_sqlite(db_path, params=params, time_provider=lambda: next(timestamps))
        miner_address = wallet_key(0).address
        recipient = wallet_key(1).address
        first_block = _mine_block(service.build_candidate_block(miner_address).block)
        service.apply_block(first_block)
        spend = signed_payment(
            OutPoint(txid=first_block.transactions[0].txid(), index=0),
            value=2_200_000_000,
            sender=wallet_key(0),
            recipient=recipient,
            amount=100,
            fee=10,
        )
        service.receive_transaction(spend)
        second_block = _mine_block(service.build_candidate_block(miner_address).block)
        service.apply_block(second_block)

        code, payload = _run_cli(["--data", str(db_path), "address-history", "--address", miner_address])

        assert code == 0
        assert payload
        assert any(entry["incoming_chipbits"] > 0 for entry in payload)
        assert any(entry["outgoing_chipbits"] > 0 for entry in payload)
        assert all("net_chc" in entry for entry in payload)
        recipient_code, recipient_payload = _run_cli(["--data", str(db_path), "address-history", "--address", recipient])
        assert recipient_code == 0
        assert any(entry["incoming_chipbits"] > 0 for entry in recipient_payload)


def test_cli_retarget_info_reports_boundary_change() -> None:
    with TemporaryDirectory() as tempdir:
        db_path = Path(tempdir) / "chipcoin.sqlite3"
        params = replace(MAINNET_PARAMS, difficulty_adjustment_window=3)
        timestamps = iter([1_700_000_000 + (index * 10) for index in range(20)])
        service = NodeService.open_sqlite(db_path, params=params, time_provider=lambda: next(timestamps))

        for _ in range(4):
            mined = _mine_block(service.build_candidate_block("CHCminer").block)
            service.apply_block(mined)

        original_open_sqlite = cli_module.NodeService.open_sqlite
        cli_module.NodeService.open_sqlite = lambda _path, **_kwargs: service
        try:
            code, payload = _run_cli(["--data", str(db_path), "retarget-info"])
        finally:
            cli_module.NodeService.open_sqlite = original_open_sqlite

        assert code == 0
        assert payload["difficulty_adjustment_window"] == 3
        assert payload["last_completed_boundary_height"] == 3
        assert payload["bits_before_last_boundary"] == service.get_block_by_height(2).header.bits
        assert payload["bits_after_last_boundary"] == service.get_block_by_height(3).header.bits
        assert payload["current_window"]["actual_timespan_seconds"] is not None


def test_cli_returns_readable_error_for_invalid_raw_tx() -> None:
    async def scenario() -> None:
        _require_local_socket_support()
        with TemporaryDirectory() as tempdir:
            db_path = Path(tempdir) / "chipcoin.sqlite3"
            service = _make_service(db_path)
            runtime = await _start_runtime_with_http(service)
            try:
                code, stdout, stderr = await asyncio.to_thread(
                    _run_cli_with_stderr,
                    ["submit-raw-tx", "--node-url", f"http://127.0.0.1:{runtime.http_bound_port}", "zz"],
                )
                assert code == 1
                assert stdout == ""
                assert "non-hexadecimal" in stderr
            finally:
                await runtime.stop()

    asyncio.run(scenario())


def test_cli_wallet_generate_address_build_and_send_local() -> None:
    with TemporaryDirectory() as tempdir:
        db_path = Path(tempdir) / "chipcoin.sqlite3"
        generated_wallet_path = Path(tempdir) / "generated-wallet.json"
        imported_wallet_path = Path(tempdir) / "imported-wallet.json"
        service = _make_service(db_path)
        owner = wallet_key(0)

        generate_code, generate_payload = _run_cli(["wallet-generate", "--wallet-file", str(generated_wallet_path)])
        import_code, import_payload = _run_cli(
            [
                "wallet-import",
                "--wallet-file",
                str(imported_wallet_path),
                "--private-key-hex",
                serialize_private_key_hex(owner.private_key),
            ]
        )
        funding_outpoint = OutPoint(txid="22" * 32, index=0)
        put_wallet_utxo(service, funding_outpoint, value=125, owner=owner)

        address_code, address_payload = _run_cli(["wallet-address", "--wallet-file", str(imported_wallet_path)])
        build_code, build_payload = _run_cli(
            [
                "--data",
                str(db_path),
                "wallet-build",
                "--wallet-file",
                str(imported_wallet_path),
                "--to",
                wallet_key(1).address,
                "--amount",
                "100",
                "--fee",
                "5",
            ]
        )
        send_code, send_payload = _run_cli(
            [
                "--data",
                str(db_path),
                "wallet-send",
                "--wallet-file",
                str(imported_wallet_path),
                "--to",
                wallet_key(1).address,
                "--amount",
                "100",
                "--fee",
                "5",
            ]
        )

        assert generate_code == 0
        assert import_code == 0
        assert address_code == 0
        assert build_code == 0
        assert send_code == 0
        assert generate_payload["address"].startswith("CHC")
        assert address_payload["address"] == import_payload["address"] == owner.address
        assert build_payload["raw_hex"]
        assert build_payload["fee_chipbits"] == 5
        assert send_payload["fee_chipbits"] == 5
        assert send_payload["mode"] == "local"
        assert service.find_transaction(send_payload["txid"]) is not None


def test_cli_wallet_send_can_submit_over_p2p_boundary() -> None:
    with TemporaryDirectory() as tempdir:
        db_path = Path(tempdir) / "chipcoin.sqlite3"
        wallet_path = Path(tempdir) / "wallet.json"
        service = _make_service(db_path)
        owner = wallet_key(0)
        funding_outpoint = OutPoint(txid="23" * 32, index=0)
        put_wallet_utxo(service, funding_outpoint, value=125, owner=owner)
        cli_module._save_wallet_key(wallet_path, owner)

        sent = {}
        original_send = cli_module._send_transaction_to_peer

        async def fake_send_transaction_to_peer(transaction, peer, *, network):
            sent["txid"] = transaction.txid()
            sent["peer"] = (peer.host, peer.port)
            sent["network"] = network

        cli_module._send_transaction_to_peer = fake_send_transaction_to_peer
        try:
            send_code, send_payload = _run_cli(
                [
                    "--data",
                    str(db_path),
                    "wallet-send",
                    "--wallet-file",
                    str(wallet_path),
                    "--to",
                    wallet_key(1).address,
                    "--amount",
                    "100",
                    "--fee",
                    "5",
                    "--connect",
                    "127.0.0.1:8333",
                ]
            )
        finally:
            cli_module._send_transaction_to_peer = original_send

        assert send_code == 0
        assert send_payload["mode"] == "p2p"
        assert sent["txid"] == send_payload["txid"]
        assert sent["peer"] == ("127.0.0.1", 8333)
        assert sent["network"] == "mainnet"


def test_cli_mine_command_runs_and_produces_a_block() -> None:
    with TemporaryDirectory() as tempdir:
        original_worker = cli_module.MinerWorker
        captured = {}

        class FakeMinerWorker:
            def __init__(self, config):
                captured["config"] = config

            def run(self):
                return {"mining": True, "accepted_blocks": 1, "rejected_blocks": 0, "miner_id": "miner-cli"}

        cli_module.MinerWorker = FakeMinerWorker
        try:
            code, payload = _run_cli(
                [
                    "mine",
                    "--node-url",
                    "http://127.0.0.1:8081",
                    "--miner-address",
                    "CHCminer-cli",
                    "--mining-min-interval-seconds",
                    "0.2",
                    "--run-seconds",
                    "0.2",
                ]
            )

            assert code == 0
            assert payload["mining"] is True
            assert payload["accepted_blocks"] == 1
            assert captured["config"].node_urls == ("http://127.0.0.1:8081",)
            assert captured["config"].mining_min_interval_seconds == 0.2
        finally:
            cli_module.MinerWorker = original_worker
