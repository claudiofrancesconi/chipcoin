"""Snapshot export/import helpers for fast node bootstrap."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path

from ..consensus.models import BlockHeader, OutPoint, TxOutput
from ..consensus.nodes import NodeRecord
from ..consensus.params import ConsensusParams
from ..consensus.pow import calculate_next_work_required, header_work, verify_proof_of_work
from ..consensus.serialization import deserialize_block_header, serialize_block_header
from ..consensus.utxo import UtxoEntry


SNAPSHOT_KIND = "chipcoin-chainstate-snapshot"
SNAPSHOT_FORMAT_VERSION = 1


@dataclass(frozen=True)
class SnapshotAnchor:
    """Trusted anchor embedded in a fast-sync snapshot."""

    height: int
    block_hash: str


@dataclass(frozen=True)
class SnapshotHeaderRecord:
    """One serialized main-chain header stored inside a snapshot."""

    header: BlockHeader
    height: int
    cumulative_work: int


@dataclass(frozen=True)
class LoadedSnapshot:
    """Decoded and verified snapshot contents."""

    metadata: dict[str, object]
    headers: tuple[SnapshotHeaderRecord, ...]
    utxos: tuple[tuple[OutPoint, UtxoEntry], ...]
    node_registry_records: tuple[NodeRecord, ...]

    @property
    def anchor(self) -> SnapshotAnchor:
        """Return the snapshot's trusted chain anchor."""

        return SnapshotAnchor(
            height=int(self.metadata["snapshot_height"]),
            block_hash=str(self.metadata["snapshot_block_hash"]),
        )


def canonical_json_dumps(payload: object) -> bytes:
    """Return deterministic JSON bytes for hashing and persistence."""

    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def snapshot_checksum(payload: dict[str, object]) -> str:
    """Compute the canonical checksum for one snapshot payload."""

    normalized = json.loads(json.dumps(payload))
    metadata = dict(normalized.get("metadata", {}))
    metadata["checksum_sha256"] = None
    normalized["metadata"] = metadata
    return hashlib.sha256(canonical_json_dumps(normalized)).hexdigest()


def write_snapshot_file(path: Path, payload: dict[str, object]) -> None:
    """Write one snapshot payload with a canonical checksum."""

    normalized = json.loads(json.dumps(payload))
    metadata = dict(normalized.get("metadata", {}))
    metadata["checksum_sha256"] = snapshot_checksum(normalized)
    normalized["metadata"] = metadata
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json_dumps(normalized) + b"\n")


def load_snapshot_file(path: Path, *, network: str, params: ConsensusParams) -> LoadedSnapshot:
    """Load, decode, and validate one snapshot from disk."""

    payload = json.loads(path.read_text(encoding="utf-8"))
    return decode_snapshot_payload(payload, network=network, params=params)


def decode_snapshot_payload(payload: dict[str, object], *, network: str, params: ConsensusParams) -> LoadedSnapshot:
    """Validate one snapshot payload and decode its contents."""

    metadata = payload.get("metadata")
    if not isinstance(metadata, dict):
        raise ValueError("snapshot metadata is required")
    if metadata.get("kind") != SNAPSHOT_KIND:
        raise ValueError("unsupported snapshot kind")
    if int(metadata.get("format_version", -1)) != SNAPSHOT_FORMAT_VERSION:
        raise ValueError("unsupported snapshot format version")
    if metadata.get("network") != network:
        raise ValueError("snapshot network does not match configured node network")
    expected_checksum = metadata.get("checksum_sha256")
    if not isinstance(expected_checksum, str) or not expected_checksum:
        raise ValueError("snapshot checksum is required")
    actual_checksum = snapshot_checksum(payload)
    if actual_checksum != expected_checksum:
        raise ValueError("snapshot checksum mismatch")

    raw_headers = payload.get("headers")
    if not isinstance(raw_headers, list) or not raw_headers:
        raise ValueError("snapshot must contain main-chain headers")
    headers: list[SnapshotHeaderRecord] = []
    for raw_record in raw_headers:
        if not isinstance(raw_record, dict):
            raise ValueError("snapshot header record must be an object")
        raw_hex = raw_record.get("raw_hex")
        if not isinstance(raw_hex, str):
            raise ValueError("snapshot header raw_hex is required")
        header, offset = deserialize_block_header(bytes.fromhex(raw_hex))
        if offset != len(bytes.fromhex(raw_hex)):
            raise ValueError("snapshot header contains trailing bytes")
        headers.append(
            SnapshotHeaderRecord(
                header=header,
                height=int(raw_record["height"]),
                cumulative_work=int(raw_record["cumulative_work"]),
            )
        )
    _validate_snapshot_headers(headers, params=params)
    anchor_height = int(metadata.get("snapshot_height", -1))
    anchor_hash = metadata.get("snapshot_block_hash")
    if anchor_height != headers[-1].height:
        raise ValueError("snapshot height does not match header chain height")
    if anchor_hash != headers[-1].header.block_hash():
        raise ValueError("snapshot anchor hash does not match last header")

    raw_utxos = payload.get("utxos")
    if not isinstance(raw_utxos, list):
        raise ValueError("snapshot utxos must be a list")
    utxos: list[tuple[OutPoint, UtxoEntry]] = []
    for raw_entry in raw_utxos:
        if not isinstance(raw_entry, dict):
            raise ValueError("snapshot utxo entry must be an object")
        utxos.append(
            (
                OutPoint(txid=str(raw_entry["txid"]), index=int(raw_entry["output_index"])),
                UtxoEntry(
                    output=TxOutput(
                        value=int(raw_entry["value_chipbits"]),
                        recipient=str(raw_entry["recipient"]),
                    ),
                    height=int(raw_entry["height"]),
                    is_coinbase=bool(raw_entry["is_coinbase"]),
                ),
            )
        )

    raw_registry = payload.get("node_registry")
    if not isinstance(raw_registry, list):
        raise ValueError("snapshot node_registry must be a list")
    node_registry_records: list[NodeRecord] = []
    for raw_record in raw_registry:
        if not isinstance(raw_record, dict):
            raise ValueError("snapshot node registry record must be an object")
        node_registry_records.append(
            NodeRecord(
                node_id=str(raw_record["node_id"]),
                payout_address=str(raw_record["payout_address"]),
                owner_pubkey=bytes.fromhex(str(raw_record["owner_pubkey_hex"])),
                registered_height=int(raw_record["registered_height"]),
                last_renewed_height=int(raw_record["last_renewed_height"]),
            )
        )

    return LoadedSnapshot(
        metadata=dict(metadata),
        headers=tuple(headers),
        utxos=tuple(utxos),
        node_registry_records=tuple(node_registry_records),
    )


def build_snapshot_payload(
    *,
    network: str,
    params: ConsensusParams,
    created_at: int,
    headers: tuple[SnapshotHeaderRecord, ...],
    utxos: tuple[tuple[OutPoint, UtxoEntry], ...],
    node_registry_records: tuple[NodeRecord, ...],
) -> dict[str, object]:
    """Build one canonical snapshot payload from live service state."""

    if not headers:
        raise ValueError("cannot export a snapshot without headers")
    tip_record = headers[-1]
    payload = {
        "metadata": {
            "kind": SNAPSHOT_KIND,
            "format_version": SNAPSHOT_FORMAT_VERSION,
            "network": network,
            "snapshot_height": tip_record.height,
            "snapshot_block_hash": tip_record.header.block_hash(),
            "snapshot_block_timestamp": tip_record.header.timestamp,
            "created_at": created_at,
            "header_count": len(headers),
            "utxo_count": len(utxos),
            "node_registry_count": len(node_registry_records),
            "consensus": {
                "genesis_bits": params.genesis_bits,
                "difficulty_adjustment_window": params.difficulty_adjustment_window,
                "target_block_time_seconds": params.target_block_time_seconds,
                "coinbase_maturity": params.coinbase_maturity,
                "max_block_weight": params.max_block_weight,
            },
            "checksum_sha256": None,
            "signature_algorithm": None,
            "signature": None,
        },
        "headers": [
            {
                "height": record.height,
                "block_hash": record.header.block_hash(),
                "cumulative_work": str(record.cumulative_work),
                "raw_hex": serialize_block_header(record.header).hex(),
            }
            for record in headers
        ],
        "utxos": [
            {
                "txid": outpoint.txid,
                "output_index": outpoint.index,
                "value_chipbits": int(entry.output.value),
                "recipient": entry.output.recipient,
                "height": entry.height,
                "is_coinbase": entry.is_coinbase,
            }
            for outpoint, entry in utxos
        ],
        "node_registry": [
            {
                "node_id": record.node_id,
                "payout_address": record.payout_address,
                "owner_pubkey_hex": record.owner_pubkey.hex(),
                "registered_height": record.registered_height,
                "last_renewed_height": record.last_renewed_height,
            }
            for record in node_registry_records
        ],
    }
    payload["metadata"]["checksum_sha256"] = snapshot_checksum(payload)
    return payload


def _validate_snapshot_headers(records: list[SnapshotHeaderRecord], *, params: ConsensusParams) -> None:
    """Validate a snapshot's embedded main-chain header path."""

    previous_header = None
    previous_cumulative_work = 0
    validated_headers: list[BlockHeader] = []
    for index, record in enumerate(records):
        header = record.header
        if record.height != index:
            raise ValueError("snapshot headers must form a contiguous height sequence")
        if header.timestamp < 0:
            raise ValueError("snapshot header timestamp cannot be negative")
        if not verify_proof_of_work(header):
            raise ValueError("snapshot header proof of work is invalid")
        if previous_header is None:
            if header.previous_block_hash != "00" * 32:
                raise ValueError("snapshot genesis header must anchor to the zero hash")
            expected_bits = params.genesis_bits
        else:
            if header.previous_block_hash != previous_header.block_hash():
                raise ValueError("snapshot headers do not form a connected main chain")
            if header.timestamp < previous_header.timestamp:
                raise ValueError("snapshot header timestamp is below the previous header timestamp")
            expected_bits = previous_header.bits
            if index % params.difficulty_adjustment_window == 0:
                window_start_height = max(0, index - params.difficulty_adjustment_window)
                first_header = validated_headers[window_start_height]
                actual_timespan_seconds = max(1, previous_header.timestamp - first_header.timestamp)
                expected_bits = calculate_next_work_required(
                    previous_bits=previous_header.bits,
                    actual_timespan_seconds=actual_timespan_seconds,
                    params=params,
                )
        if header.bits != expected_bits:
            raise ValueError("snapshot header difficulty does not match expected retarget rules")
        previous_cumulative_work += header_work(header)
        if record.cumulative_work != previous_cumulative_work:
            raise ValueError("snapshot cumulative work is inconsistent with embedded headers")
        previous_header = header
        validated_headers.append(header)
