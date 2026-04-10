from dataclasses import replace
import json
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

from chipcoin.consensus.models import Block
from chipcoin.consensus.pow import verify_proof_of_work
from chipcoin.node.snapshots import read_snapshot_payload, sign_snapshot_payload, snapshot_checksum, write_snapshot_file
from chipcoin.node.service import NodeService
from chipcoin.node.sync import SyncManager


def _make_service(database_path: Path, *, start_time: int) -> NodeService:
    timestamps = iter(range(start_time, start_time + 50_000))
    return NodeService.open_sqlite(database_path, time_provider=lambda: next(timestamps))


def _mine_block(block: Block) -> Block:
    for nonce in range(2_000_000):
        header = replace(block.header, nonce=nonce)
        if verify_proof_of_work(header):
            return replace(block, header=header)
    raise AssertionError("Expected to find a valid nonce for the easy target.")


def _mine_chain(service: NodeService, count: int, miner_address: str) -> list[Block]:
    blocks: list[Block] = []
    for _ in range(count):
        block = _mine_block(service.build_candidate_block(miner_address).block)
        service.apply_block(block)
        blocks.append(block)
    return blocks


def _ed25519_keypair() -> tuple[bytes, bytes]:
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
    return private_bytes, public_bytes


def _export_snapshot_v1(service: NodeService, path: Path) -> None:
    service.export_snapshot_file(path, format_version=1)


def test_snapshot_export_import_roundtrip_preserves_anchor_and_utxo_state() -> None:
    with TemporaryDirectory() as tempdir:
        source = _make_service(Path(tempdir) / "source.sqlite3", start_time=1_700_000_000)
        _mine_chain(source, 4, "CHCminer-source")
        snapshot_path = Path(tempdir) / "snapshot.json"

        metadata = source.export_snapshot_file(snapshot_path)

        target = _make_service(Path(tempdir) / "target.sqlite3", start_time=1_700_001_000)
        imported = target.import_snapshot_file(snapshot_path)

        assert imported["checksum_sha256"] == metadata["checksum_sha256"]
        assert target.chain_tip() is not None
        assert source.chain_tip() is not None
        assert target.chain_tip().block_hash == source.chain_tip().block_hash
        assert target.chain_tip().height == source.chain_tip().height
        assert target.snapshot_anchor() is not None
        assert target.snapshot_anchor().block_hash == source.chain_tip().block_hash
        assert target.chainstate.list_utxos() == source.chainstate.list_utxos()
        assert target.node_registry.list_records() == source.node_registry.list_records()
        assert metadata["format_version"] == 2
        assert snapshot_path.read_bytes().startswith(b"CHCSNP2\n")


def test_snapshot_v2_signed_import_roundtrip() -> None:
    with TemporaryDirectory() as tempdir:
        source = _make_service(Path(tempdir) / "source.sqlite3", start_time=1_700_000_000)
        _mine_chain(source, 3, "CHCminer-source")
        snapshot_path = Path(tempdir) / "snapshot-v2.bin"
        source.export_snapshot_file(snapshot_path)
        payload = read_snapshot_payload(snapshot_path)
        private_key, public_key = _ed25519_keypair()
        write_snapshot_file(snapshot_path, sign_snapshot_payload(payload, private_key=private_key))

        target = _make_service(Path(tempdir) / "target.sqlite3", start_time=1_700_001_000)
        imported = target.import_snapshot_file(snapshot_path, trust_mode="enforce", trusted_keys=(public_key,))

        assert imported["format_version"] == 2
        assert imported["trusted_signature_count"] == 1
        assert target.chain_tip() is not None
        assert target.chain_tip().block_hash == source.chain_tip().block_hash


def test_sync_manager_downloads_only_delta_after_snapshot_import() -> None:
    with TemporaryDirectory() as tempdir:
        source = _make_service(Path(tempdir) / "source.sqlite3", start_time=1_700_000_000)
        initial_blocks = _mine_chain(source, 6, "CHCminer-source")
        snapshot_path = Path(tempdir) / "snapshot.json"
        source.export_snapshot_file(snapshot_path)

        target = _make_service(Path(tempdir) / "target.sqlite3", start_time=1_700_001_000)
        target.import_snapshot_file(snapshot_path)
        additional_blocks = _mine_chain(source, 2, "CHCminer-source")

        result = SyncManager(node=target).synchronize(source)

        assert result.headers_received == 2
        assert result.blocks_fetched == 2
        assert target.chain_tip() is not None
        assert target.chain_tip().block_hash == additional_blocks[-1].block_hash()
        assert target.snapshot_anchor() is not None
        assert target.snapshot_anchor().block_hash == initial_blocks[-1].block_hash()


def test_snapshot_anchor_mismatch_is_rejected_before_delta_sync() -> None:
    with TemporaryDirectory() as tempdir:
        trusted = _make_service(Path(tempdir) / "trusted.sqlite3", start_time=1_700_000_000)
        attacker = _make_service(Path(tempdir) / "attacker.sqlite3", start_time=1_700_001_000)
        _mine_chain(trusted, 3, "CHCtrusted")
        _mine_chain(attacker, 3, "CHCattacker")
        snapshot_path = Path(tempdir) / "snapshot.json"
        trusted.export_snapshot_file(snapshot_path)

        target = _make_service(Path(tempdir) / "target.sqlite3", start_time=1_700_002_000)
        target.import_snapshot_file(snapshot_path)

        with pytest.raises(ValueError, match="snapshot anchor mismatch"):
            SyncManager(node=target).synchronize(attacker)


def test_snapshot_bootstrap_persists_across_restart_without_replay() -> None:
    with TemporaryDirectory() as tempdir:
        source = _make_service(Path(tempdir) / "source.sqlite3", start_time=1_700_000_000)
        _mine_chain(source, 5, "CHCminer-source")
        snapshot_path = Path(tempdir) / "snapshot.json"
        source.export_snapshot_file(snapshot_path)

        db_path = Path(tempdir) / "target.sqlite3"
        target = _make_service(db_path, start_time=1_700_001_000)
        target.import_snapshot_file(snapshot_path)

        restarted = _make_service(db_path, start_time=1_700_002_000)

        assert restarted.chain_tip() is not None
        assert restarted.chain_tip().block_hash == source.chain_tip().block_hash
        assert restarted.snapshot_anchor() is not None
        assert restarted.snapshot_anchor().block_hash == source.chain_tip().block_hash


def test_snapshot_import_rejects_anchor_hash_mismatch() -> None:
    with TemporaryDirectory() as tempdir:
        source = _make_service(Path(tempdir) / "source.sqlite3", start_time=1_700_000_000)
        _mine_chain(source, 2, "CHCminer-source")
        snapshot_path = Path(tempdir) / "snapshot.json"
        _export_snapshot_v1(source, snapshot_path)
        payload = read_snapshot_payload(snapshot_path)
        payload["metadata"]["snapshot_block_hash"] = "11" * 32
        payload["metadata"]["checksum_sha256"] = snapshot_checksum(payload)
        write_snapshot_file(snapshot_path, payload)

        target = _make_service(Path(tempdir) / "target.sqlite3", start_time=1_700_001_000)
        with pytest.raises(ValueError, match="snapshot anchor hash"):
            target.import_snapshot_file(snapshot_path)


def test_snapshot_import_rejects_tampered_checksum() -> None:
    with TemporaryDirectory() as tempdir:
        source = _make_service(Path(tempdir) / "source.sqlite3", start_time=1_700_000_000)
        _mine_chain(source, 2, "CHCminer-source")
        snapshot_path = Path(tempdir) / "snapshot.json"
        _export_snapshot_v1(source, snapshot_path)
        payload = read_snapshot_payload(snapshot_path)
        payload["metadata"]["checksum_sha256"] = "00" * 32
        snapshot_path.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")

        target = _make_service(Path(tempdir) / "target.sqlite3", start_time=1_700_001_000)
        with pytest.raises(ValueError, match="snapshot checksum mismatch"):
            target.import_snapshot_file(snapshot_path)


def test_snapshot_import_rejects_divergent_embedded_header_chain() -> None:
    with TemporaryDirectory() as tempdir:
        source = _make_service(Path(tempdir) / "source.sqlite3", start_time=1_700_000_000)
        _mine_chain(source, 3, "CHCminer-source")
        snapshot_path = Path(tempdir) / "snapshot.json"
        _export_snapshot_v1(source, snapshot_path)
        payload = read_snapshot_payload(snapshot_path)
        payload["headers"][1]["raw_hex"] = payload["headers"][0]["raw_hex"]
        payload["headers"][1]["block_hash"] = payload["headers"][0]["block_hash"]
        payload["metadata"]["checksum_sha256"] = snapshot_checksum(payload)
        write_snapshot_file(snapshot_path, payload)

        target = _make_service(Path(tempdir) / "target.sqlite3", start_time=1_700_001_000)
        with pytest.raises(ValueError, match="connected main chain|difficulty|cumulative work"):
            target.import_snapshot_file(snapshot_path)


def test_snapshot_sync_rejects_invalid_post_anchor_block_sequence() -> None:
    with TemporaryDirectory() as tempdir:
        source = _make_service(Path(tempdir) / "source.sqlite3", start_time=1_700_000_000)
        _mine_chain(source, 4, "CHCminer-source")
        snapshot_path = Path(tempdir) / "snapshot.json"
        source.export_snapshot_file(snapshot_path)
        target = _make_service(Path(tempdir) / "target.sqlite3", start_time=1_700_001_000)
        target.import_snapshot_file(snapshot_path)
        next_block = _mine_block(source.build_candidate_block("CHCminer-source").block)
        source.apply_block(next_block)

        class InvalidDeltaPeer:
            def handle_getheaders(self, request, *, limit=2000):
                return source.handle_getheaders(request, limit=limit)

            def get_block_by_hash(self, block_hash: str):
                block = source.get_block_by_hash(block_hash)
                if block is None:
                    return None
                bad_coinbase = replace(
                    block.transactions[0],
                    outputs=tuple(
                        replace(output, recipient="CHCtampered") if index == 0 else output
                        for index, output in enumerate(block.transactions[0].outputs)
                    ),
                )
                return replace(block, transactions=(bad_coinbase,) + block.transactions[1:])

        with pytest.raises(Exception, match="Merkle|coinbase|validation|weight|previous"):
            SyncManager(node=target).synchronize(InvalidDeltaPeer())


def test_signed_snapshot_is_accepted_in_enforce_mode() -> None:
    with TemporaryDirectory() as tempdir:
        source = _make_service(Path(tempdir) / "source.sqlite3", start_time=1_700_000_000)
        _mine_chain(source, 2, "CHCminer-source")
        snapshot_path = Path(tempdir) / "snapshot.json"
        _export_snapshot_v1(source, snapshot_path)
        payload = read_snapshot_payload(snapshot_path)
        private_key, public_key = _ed25519_keypair()
        write_snapshot_file(snapshot_path, sign_snapshot_payload(payload, private_key=private_key))

        target = _make_service(Path(tempdir) / "target.sqlite3", start_time=1_700_001_000)
        imported = target.import_snapshot_file(snapshot_path, trust_mode="enforce", trusted_keys=(public_key,))

        assert imported["trusted_signature_count"] == 1
        assert imported["valid_signature_count"] == 1
        assert target.chain_tip() is not None
        assert target.chain_tip().height == 1


def test_unsigned_snapshot_is_rejected_in_enforce_mode() -> None:
    with TemporaryDirectory() as tempdir:
        source = _make_service(Path(tempdir) / "source.sqlite3", start_time=1_700_000_000)
        _mine_chain(source, 1, "CHCminer-source")
        snapshot_path = Path(tempdir) / "snapshot.json"
        source.export_snapshot_file(snapshot_path)
        _, public_key = _ed25519_keypair()

        target = _make_service(Path(tempdir) / "target.sqlite3", start_time=1_700_001_000)
        with pytest.raises(ValueError, match="at least one signature"):
            target.import_snapshot_file(snapshot_path, trust_mode="enforce", trusted_keys=(public_key,))


def test_invalid_snapshot_signature_is_rejected() -> None:
    with TemporaryDirectory() as tempdir:
        source = _make_service(Path(tempdir) / "source.sqlite3", start_time=1_700_000_000)
        _mine_chain(source, 2, "CHCminer-source")
        snapshot_path = Path(tempdir) / "snapshot.json"
        _export_snapshot_v1(source, snapshot_path)
        payload = read_snapshot_payload(snapshot_path)
        private_key, public_key = _ed25519_keypair()
        signed = sign_snapshot_payload(payload, private_key=private_key)
        signed["metadata"]["signatures"][0]["signature_hex"] = "00" * 64
        write_snapshot_file(snapshot_path, signed)

        target = _make_service(Path(tempdir) / "target.sqlite3", start_time=1_700_001_000)
        with pytest.raises(ValueError, match="signature is invalid"):
            target.import_snapshot_file(snapshot_path, trust_mode="enforce", trusted_keys=(public_key,))


def test_unknown_snapshot_signer_is_rejected_in_enforce_mode() -> None:
    with TemporaryDirectory() as tempdir:
        source = _make_service(Path(tempdir) / "source.sqlite3", start_time=1_700_000_000)
        _mine_chain(source, 2, "CHCminer-source")
        snapshot_path = Path(tempdir) / "snapshot.json"
        _export_snapshot_v1(source, snapshot_path)
        payload = read_snapshot_payload(snapshot_path)
        private_key, _ = _ed25519_keypair()
        _, trusted_public_key = _ed25519_keypair()
        write_snapshot_file(snapshot_path, sign_snapshot_payload(payload, private_key=private_key))

        target = _make_service(Path(tempdir) / "target.sqlite3", start_time=1_700_001_000)
        with pytest.raises(ValueError, match="trusted signer"):
            target.import_snapshot_file(snapshot_path, trust_mode="enforce", trusted_keys=(trusted_public_key,))


def test_tampered_signed_snapshot_is_rejected() -> None:
    with TemporaryDirectory() as tempdir:
        source = _make_service(Path(tempdir) / "source.sqlite3", start_time=1_700_000_000)
        _mine_chain(source, 2, "CHCminer-source")
        snapshot_path = Path(tempdir) / "snapshot.json"
        _export_snapshot_v1(source, snapshot_path)
        payload = read_snapshot_payload(snapshot_path)
        private_key, public_key = _ed25519_keypair()
        signed = sign_snapshot_payload(payload, private_key=private_key)
        signed["utxos"][0]["recipient"] = "CHCtampered"
        signed["metadata"]["checksum_sha256"] = snapshot_checksum(signed)
        write_snapshot_file(snapshot_path, signed)

        target = _make_service(Path(tempdir) / "target.sqlite3", start_time=1_700_001_000)
        with pytest.raises(ValueError, match="signature is invalid"):
            target.import_snapshot_file(snapshot_path, trust_mode="enforce", trusted_keys=(public_key,))


def test_tampered_v2_snapshot_container_is_rejected() -> None:
    with TemporaryDirectory() as tempdir:
        source = _make_service(Path(tempdir) / "source.sqlite3", start_time=1_700_000_000)
        _mine_chain(source, 2, "CHCminer-source")
        snapshot_path = Path(tempdir) / "snapshot-v2.bin"
        source.export_snapshot_file(snapshot_path)
        raw = bytearray(snapshot_path.read_bytes())
        raw[-1] ^= 0x01
        snapshot_path.write_bytes(bytes(raw))

        target = _make_service(Path(tempdir) / "target.sqlite3", start_time=1_700_001_000)
        with pytest.raises(ValueError, match="compressed payload checksum mismatch|payload checksum mismatch|compressed payload is invalid"):
            target.import_snapshot_file(snapshot_path)


def test_snapshot_v1_backward_compatibility_import_is_retained() -> None:
    with TemporaryDirectory() as tempdir:
        source = _make_service(Path(tempdir) / "source.sqlite3", start_time=1_700_000_000)
        _mine_chain(source, 2, "CHCminer-source")
        snapshot_path = Path(tempdir) / "snapshot-v1.json"
        _export_snapshot_v1(source, snapshot_path)

        target = _make_service(Path(tempdir) / "target.sqlite3", start_time=1_700_001_000)
        imported = target.import_snapshot_file(snapshot_path)

        assert imported["format_version"] == 1
        assert target.chain_tip() is not None
        assert target.chain_tip().block_hash == source.chain_tip().block_hash


def test_status_shows_snapshot_trust_metadata_after_bootstrap() -> None:
    with TemporaryDirectory() as tempdir:
        source = _make_service(Path(tempdir) / "source.sqlite3", start_time=1_700_000_000)
        _mine_chain(source, 2, "CHCminer-source")
        snapshot_path = Path(tempdir) / "snapshot.json"
        source.export_snapshot_file(snapshot_path)
        payload = read_snapshot_payload(snapshot_path)
        private_key, public_key = _ed25519_keypair()
        write_snapshot_file(snapshot_path, sign_snapshot_payload(payload, private_key=private_key))

        target = _make_service(Path(tempdir) / "target.sqlite3", start_time=1_700_001_000)
        target.import_snapshot_file(snapshot_path, trust_mode="enforce", trusted_keys=(public_key,))
        status = target.status()

        assert status["bootstrap_mode"] == "snapshot"
        assert status["snapshot_anchor_height"] == 1
        assert status["snapshot_anchor_hash"] == source.chain_tip().block_hash
        assert status["snapshot_trust_mode"] == "enforce"
        assert status["snapshot_signature_verified"] is True
        assert status["accepted_snapshot_signer_pubkeys"] == [public_key.hex()]
        assert status["snapshot_trust_warnings"] == []
        assert status["sync_phase"] == "snapshot_imported"
        assert status["sync"]["phase"] == "snapshot_imported"
