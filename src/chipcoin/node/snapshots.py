"""Snapshot export/import helpers for fast node bootstrap."""

from __future__ import annotations

from dataclasses import dataclass
import gzip
import hashlib
import json
from pathlib import Path
import struct

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

from ..consensus.models import BlockHeader, OutPoint, TxOutput
from ..consensus.nodes import NodeRecord
from ..consensus.params import ConsensusParams
from ..consensus.pow import calculate_next_work_required, header_work, verify_proof_of_work
from ..consensus.serialization import deserialize_block_header, serialize_block_header
from ..consensus.utxo import UtxoEntry


SNAPSHOT_KIND = "chipcoin-chainstate-snapshot"
SNAPSHOT_FORMAT_VERSION = 2
SNAPSHOT_FORMAT_VERSION_V1 = 1
SNAPSHOT_FORMAT_VERSION_V2 = 2
SNAPSHOT_SIGNATURE_ALGORITHM = "ed25519"
SNAPSHOT_V2_MAGIC = b"CHCSNP2\n"
SNAPSHOT_V2_HEADER = struct.Struct(">8sQQ")
SNAPSHOT_V2_PAYLOAD_ENCODING = "gzip+json"


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
    valid_signature_count: int = 0
    trusted_signature_count: int = 0
    accepted_signer_pubkeys: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

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


def _normalize_snapshot_payload(payload: dict[str, object]) -> dict[str, object]:
    """Return one deep-copied snapshot payload object."""

    return json.loads(json.dumps(payload))


def _snapshot_body(payload: dict[str, object]) -> dict[str, object]:
    """Return the large mutable chainstate body stored outside metadata in v2."""

    return {
        "headers": payload.get("headers", []),
        "utxos": payload.get("utxos", []),
        "node_registry": payload.get("node_registry", []),
    }


def canonicalize_snapshot_payload(payload: dict[str, object], *, include_checksum: bool = True) -> dict[str, object]:
    """Normalize one snapshot payload for checksum/signature purposes."""

    normalized = _normalize_snapshot_payload(payload)
    metadata = dict(normalized.get("metadata", {}))
    metadata["signatures"] = []
    if not include_checksum:
        metadata["checksum_sha256"] = None
    normalized["metadata"] = metadata
    return normalized


def snapshot_checksum(payload: dict[str, object]) -> str:
    """Compute the canonical checksum for one snapshot payload."""

    normalized = canonicalize_snapshot_payload(payload, include_checksum=False)
    return hashlib.sha256(canonical_json_dumps(normalized)).hexdigest()


def snapshot_signature_digest(payload: dict[str, object]) -> bytes:
    """Compute the Ed25519 signing digest for one snapshot payload."""

    normalized = canonicalize_snapshot_payload(payload, include_checksum=True)
    return hashlib.sha256(canonical_json_dumps(normalized)).digest()


def parse_ed25519_private_key_hex(value: str) -> bytes:
    """Decode one raw Ed25519 private key from hex."""

    try:
        private_key = bytes.fromhex(value)
    except ValueError as exc:
        raise ValueError("Snapshot signer private key hex is invalid.") from exc
    if len(private_key) != 32:
        raise ValueError("Snapshot signer private key must be exactly 32 bytes.")
    return private_key


def parse_ed25519_public_key_hex(value: str) -> bytes:
    """Decode one raw Ed25519 public key from hex."""

    try:
        public_key = bytes.fromhex(value)
    except ValueError as exc:
        raise ValueError("Snapshot trusted public key hex is invalid.") from exc
    if len(public_key) != 32:
        raise ValueError("Snapshot trusted public key must be exactly 32 bytes.")
    return public_key


def ed25519_public_key_hex_from_private_key(private_key: bytes) -> str:
    """Derive one raw Ed25519 public key from private key material."""

    if len(private_key) != 32:
        raise ValueError("Snapshot signer private key must be exactly 32 bytes.")
    derived = ed25519.Ed25519PrivateKey.from_private_bytes(private_key).public_key()
    return derived.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    ).hex()


def sign_snapshot_payload(payload: dict[str, object], *, private_key: bytes) -> dict[str, object]:
    """Append or replace one Ed25519 signature on a snapshot payload."""

    if len(private_key) != 32:
        raise ValueError("Snapshot signer private key must be exactly 32 bytes.")
    normalized = _normalize_snapshot_payload(payload)
    metadata = dict(normalized.get("metadata", {}))
    digest = snapshot_signature_digest(normalized)
    signer = ed25519.Ed25519PrivateKey.from_private_bytes(private_key)
    public_key_hex = signer.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    ).hex()
    signature_record = {
        "algorithm": SNAPSHOT_SIGNATURE_ALGORITHM,
        "public_key_hex": public_key_hex,
        "signature_hex": signer.sign(digest).hex(),
    }
    signatures = metadata.get("signatures")
    if not isinstance(signatures, list):
        signatures = []
    filtered_signatures = []
    for entry in signatures:
        if isinstance(entry, dict) and entry.get("public_key_hex") == public_key_hex:
            continue
        filtered_signatures.append(entry)
    filtered_signatures.append(signature_record)
    metadata["signatures"] = filtered_signatures
    metadata["checksum_sha256"] = snapshot_checksum(normalized)
    normalized["metadata"] = metadata
    return normalized


def write_snapshot_file(path: Path, payload: dict[str, object]) -> None:
    """Write one snapshot payload with a canonical checksum."""

    normalized = _normalize_snapshot_payload(payload)
    metadata = dict(normalized.get("metadata", {}))
    metadata["checksum_sha256"] = snapshot_checksum(normalized)
    normalized["metadata"] = metadata
    path.parent.mkdir(parents=True, exist_ok=True)
    format_version = int(metadata.get("format_version", SNAPSHOT_FORMAT_VERSION_V1))
    if format_version == SNAPSHOT_FORMAT_VERSION_V1:
        path.write_bytes(canonical_json_dumps(normalized) + b"\n")
        return
    if format_version != SNAPSHOT_FORMAT_VERSION_V2:
        raise ValueError(f"unsupported snapshot format version for writing: {format_version}")
    body_bytes = canonical_json_dumps(_snapshot_body(normalized))
    compressed_body = gzip.compress(body_bytes, compresslevel=6, mtime=0)
    metadata["payload_encoding"] = SNAPSHOT_V2_PAYLOAD_ENCODING
    metadata["payload_sha256"] = hashlib.sha256(body_bytes).hexdigest()
    metadata["compressed_payload_sha256"] = hashlib.sha256(compressed_body).hexdigest()
    metadata.setdefault(
        "compatibility",
        {
            "min_reader_format_version": SNAPSHOT_FORMAT_VERSION_V1,
            "preferred_reader_format_version": SNAPSHOT_FORMAT_VERSION_V2,
        },
    )
    metadata["checksum_sha256"] = snapshot_checksum({**normalized, "metadata": metadata})
    metadata_bytes = canonical_json_dumps(metadata)
    header = SNAPSHOT_V2_HEADER.pack(SNAPSHOT_V2_MAGIC, len(metadata_bytes), len(compressed_body))
    path.write_bytes(header + metadata_bytes + compressed_body)


def read_snapshot_payload(path: Path) -> dict[str, object]:
    """Load one snapshot file into its canonical logical payload object."""

    raw = path.read_bytes()
    stripped = raw.lstrip()
    if stripped.startswith(b"{"):
        return json.loads(raw.decode("utf-8"))
    if len(raw) < SNAPSHOT_V2_HEADER.size:
        raise ValueError("snapshot file is too short")
    magic, metadata_length, payload_length = SNAPSHOT_V2_HEADER.unpack(raw[: SNAPSHOT_V2_HEADER.size])
    if magic != SNAPSHOT_V2_MAGIC:
        raise ValueError("unsupported snapshot binary container")
    offset = SNAPSHOT_V2_HEADER.size
    metadata_bytes = raw[offset : offset + metadata_length]
    payload_bytes = raw[offset + metadata_length : offset + metadata_length + payload_length]
    if len(metadata_bytes) != metadata_length or len(payload_bytes) != payload_length:
        raise ValueError("snapshot v2 container is truncated")
    metadata = json.loads(metadata_bytes.decode("utf-8"))
    if not isinstance(metadata, dict):
        raise ValueError("snapshot v2 metadata must be an object")
    payload_encoding = metadata.get("payload_encoding")
    if payload_encoding != SNAPSHOT_V2_PAYLOAD_ENCODING:
        raise ValueError("unsupported snapshot v2 payload encoding")
    compressed_payload_sha256 = metadata.get("compressed_payload_sha256")
    if isinstance(compressed_payload_sha256, str) and hashlib.sha256(payload_bytes).hexdigest() != compressed_payload_sha256:
        raise ValueError("snapshot compressed payload checksum mismatch")
    try:
        body_bytes = gzip.decompress(payload_bytes)
    except Exception as exc:
        raise ValueError("snapshot v2 compressed payload is invalid") from exc
    payload_sha256 = metadata.get("payload_sha256")
    if not isinstance(payload_sha256, str) or hashlib.sha256(body_bytes).hexdigest() != payload_sha256:
        raise ValueError("snapshot payload checksum mismatch")
    body = json.loads(body_bytes.decode("utf-8"))
    if not isinstance(body, dict):
        raise ValueError("snapshot v2 payload body must be an object")
    return {"metadata": metadata, **body}


def load_snapshot_file(
    path: Path,
    *,
    network: str,
    params: ConsensusParams,
    trust_mode: str = "off",
    trusted_keys: tuple[bytes, ...] = (),
) -> LoadedSnapshot:
    """Load, decode, and validate one snapshot from disk."""

    payload = read_snapshot_payload(path)
    return decode_snapshot_payload(payload, network=network, params=params, trust_mode=trust_mode, trusted_keys=trusted_keys)


def decode_snapshot_payload(
    payload: dict[str, object],
    *,
    network: str,
    params: ConsensusParams,
    trust_mode: str = "off",
    trusted_keys: tuple[bytes, ...] = (),
) -> LoadedSnapshot:
    """Validate one snapshot payload and decode its contents."""

    metadata = payload.get("metadata")
    if not isinstance(metadata, dict):
        raise ValueError("snapshot metadata is required")
    if metadata.get("kind") != SNAPSHOT_KIND:
        raise ValueError("unsupported snapshot kind")
    format_version = int(metadata.get("format_version", -1))
    if format_version not in {SNAPSHOT_FORMAT_VERSION_V1, SNAPSHOT_FORMAT_VERSION_V2}:
        raise ValueError("unsupported snapshot format version")
    if metadata.get("network") != network:
        raise ValueError("snapshot network does not match configured node network")
    expected_checksum = metadata.get("checksum_sha256")
    if not isinstance(expected_checksum, str) or not expected_checksum:
        raise ValueError("snapshot checksum is required")
    actual_checksum = snapshot_checksum(payload)
    if actual_checksum != expected_checksum:
        raise ValueError("snapshot checksum mismatch")
    valid_signature_count, trusted_signature_count, accepted_signer_pubkeys, trust_warnings = _validate_snapshot_signatures(
        payload,
        trust_mode=trust_mode,
        trusted_keys=trusted_keys,
    )

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
        valid_signature_count=valid_signature_count,
        trusted_signature_count=trusted_signature_count,
        accepted_signer_pubkeys=accepted_signer_pubkeys,
        warnings=trust_warnings,
    )


def build_snapshot_payload(
    *,
    network: str,
    params: ConsensusParams,
    created_at: int,
    headers: tuple[SnapshotHeaderRecord, ...],
    utxos: tuple[tuple[OutPoint, UtxoEntry], ...],
    node_registry_records: tuple[NodeRecord, ...],
    format_version: int = SNAPSHOT_FORMAT_VERSION,
) -> dict[str, object]:
    """Build one canonical snapshot payload from live service state."""

    if not headers:
        raise ValueError("cannot export a snapshot without headers")
    tip_record = headers[-1]
    payload = {
        "metadata": {
            "kind": SNAPSHOT_KIND,
            "format_version": format_version,
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
            "signatures": [],
            "payload_encoding": SNAPSHOT_V2_PAYLOAD_ENCODING,
            "payload_sha256": None,
            "compressed_payload_sha256": None,
            "compatibility": {
                "min_reader_format_version": SNAPSHOT_FORMAT_VERSION_V1,
                "preferred_reader_format_version": SNAPSHOT_FORMAT_VERSION_V2,
            },
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
    if format_version == SNAPSHOT_FORMAT_VERSION_V1:
        payload["metadata"]["payload_encoding"] = None
        payload["metadata"]["payload_sha256"] = None
        payload["metadata"]["compressed_payload_sha256"] = None
        payload["metadata"]["compatibility"] = {
            "min_reader_format_version": SNAPSHOT_FORMAT_VERSION_V1,
            "preferred_reader_format_version": SNAPSHOT_FORMAT_VERSION_V1,
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


def _validate_snapshot_signatures(
    payload: dict[str, object],
    *,
    trust_mode: str,
    trusted_keys: tuple[bytes, ...],
) -> tuple[int, int, tuple[str, ...], tuple[str, ...]]:
    """Validate the snapshot signature set against the chosen trust policy."""

    if trust_mode not in {"off", "warn", "enforce"}:
        raise ValueError("snapshot trust mode must be one of: off, warn, enforce")
    metadata = payload.get("metadata")
    if not isinstance(metadata, dict):
        raise ValueError("snapshot metadata is required")
    signatures = metadata.get("signatures", [])
    if signatures is None:
        signatures = []
    if not isinstance(signatures, list):
        raise ValueError("snapshot signatures must be a list")
    digest = snapshot_signature_digest(payload)
    trusted_key_set = {key.hex() for key in trusted_keys}
    valid_signature_count = 0
    trusted_signature_count = 0
    accepted_signer_pubkeys: list[str] = []
    warnings: list[str] = []
    for raw_signature in signatures:
        if not isinstance(raw_signature, dict):
            raise ValueError("snapshot signature entry must be an object")
        algorithm = raw_signature.get("algorithm")
        if algorithm != SNAPSHOT_SIGNATURE_ALGORITHM:
            raise ValueError("unsupported snapshot signature algorithm")
        public_key_hex = raw_signature.get("public_key_hex")
        signature_hex = raw_signature.get("signature_hex")
        if not isinstance(public_key_hex, str) or not public_key_hex:
            raise ValueError("snapshot signature public_key_hex is required")
        if not isinstance(signature_hex, str) or not signature_hex:
            raise ValueError("snapshot signature_hex is required")
        public_key = parse_ed25519_public_key_hex(public_key_hex)
        try:
            signature = bytes.fromhex(signature_hex)
        except ValueError as exc:
            if trust_mode == "warn":
                warnings.append("snapshot_signature_encoding_invalid")
                continue
            raise ValueError("snapshot signature_hex is invalid") from exc
        try:
            ed25519.Ed25519PublicKey.from_public_bytes(public_key).verify(signature, digest)
        except Exception as exc:
            if trust_mode == "warn":
                warnings.append("snapshot_signature_invalid")
                continue
            raise ValueError("snapshot signature is invalid for the canonical payload") from exc
        valid_signature_count += 1
        accepted_signer_pubkeys.append(public_key_hex)
        if public_key_hex in trusted_key_set:
            trusted_signature_count += 1
        elif trusted_key_set and trust_mode == "warn":
            warnings.append("snapshot_signer_not_trusted")
    if trust_mode == "enforce":
        if not signatures:
            raise ValueError("snapshot must contain at least one signature in enforce mode")
        if not trusted_signature_count:
            raise ValueError("snapshot does not contain a valid signature from a trusted signer")
    elif trust_mode == "warn":
        if not signatures:
            warnings.append("snapshot_unsigned_but_accepted_due_to_warn_mode")
        elif valid_signature_count == 0:
            warnings.append("snapshot_unverified_but_accepted_due_to_warn_mode")
        elif trusted_key_set and trusted_signature_count == 0:
            warnings.append("snapshot_untrusted_signer_but_accepted_due_to_warn_mode")
    return (
        valid_signature_count,
        trusted_signature_count,
        tuple(sorted(set(accepted_signer_pubkeys))),
        tuple(dict.fromkeys(warnings)),
    )
