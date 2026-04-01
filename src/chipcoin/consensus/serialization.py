"""Deterministic serialization boundaries for consensus-critical structures."""

from __future__ import annotations

from struct import pack, unpack_from

from .models import Block, BlockHeader, ChipbitAmount, OutPoint, Transaction, TxInput, TxOutput


def _encode_varint(value: int) -> bytes:
    """Encode a non-negative integer using a compact deterministic format."""

    if value < 0:
        raise ValueError("varint value cannot be negative")
    if value < 0xFD:
        return bytes((value,))
    if value <= 0xFFFF:
        return b"\xFD" + pack("<H", value)
    if value <= 0xFFFFFFFF:
        return b"\xFE" + pack("<I", value)
    if value <= 0xFFFFFFFFFFFFFFFF:
        return b"\xFF" + pack("<Q", value)
    raise ValueError("varint value exceeds 64-bit range")


def _decode_varint(buffer: bytes, offset: int = 0) -> tuple[int, int]:
    """Decode a varint from a bytes buffer and return value and next offset."""

    if offset >= len(buffer):
        raise ValueError("Unexpected end of payload while decoding varint.")
    prefix = buffer[offset]
    offset += 1
    if prefix < 0xFD:
        return prefix, offset
    if prefix == 0xFD:
        return unpack_from("<H", buffer, offset)[0], offset + 2
    if prefix == 0xFE:
        return unpack_from("<I", buffer, offset)[0], offset + 4
    return unpack_from("<Q", buffer, offset)[0], offset + 8


def _encode_bytes(value: bytes) -> bytes:
    """Encode bytes with a varint length prefix."""

    return _encode_varint(len(value)) + value


def _decode_bytes(buffer: bytes, offset: int = 0) -> tuple[bytes, int]:
    """Decode a length-prefixed byte sequence."""

    length, offset = _decode_varint(buffer, offset)
    end = offset + length
    if end > len(buffer):
        raise ValueError("Unexpected end of payload while decoding bytes.")
    return buffer[offset:end], end


def _encode_string(value: str) -> bytes:
    """Encode a UTF-8 string with a varint length prefix."""

    return _encode_bytes(value.encode("utf-8"))


def _decode_string(buffer: bytes, offset: int = 0) -> tuple[str, int]:
    """Decode a length-prefixed UTF-8 string."""

    raw, offset = _decode_bytes(buffer, offset)
    return raw.decode("utf-8"), offset


def _encode_hash(hex_value: str) -> bytes:
    """Encode a 32-byte hash represented as 64 hexadecimal characters."""

    raw = bytes.fromhex(hex_value)
    if len(raw) != 32:
        raise ValueError("hash values must be exactly 32 bytes")
    return raw


def _encode_metadata(metadata: dict[str, str]) -> bytes:
    """Encode metadata pairs in sorted key order for deterministic output."""

    items = sorted(metadata.items())
    encoded = bytearray()
    encoded.extend(_encode_varint(len(items)))
    for key, value in items:
        encoded.extend(_encode_string(key))
        encoded.extend(_encode_string(value))
    return bytes(encoded)


def _decode_metadata(buffer: bytes, offset: int = 0) -> tuple[dict[str, str], int]:
    """Decode deterministic metadata mapping."""

    count, offset = _decode_varint(buffer, offset)
    metadata: dict[str, str] = {}
    for _ in range(count):
        key, offset = _decode_string(buffer, offset)
        value, offset = _decode_string(buffer, offset)
        metadata[key] = value
    return metadata, offset


def serialize_transaction(transaction: Transaction) -> bytes:
    """Serialize a transaction deterministically for hashing and signing."""

    encoded = bytearray()
    encoded.extend(pack("<I", transaction.version))
    encoded.extend(_encode_varint(len(transaction.inputs)))
    for tx_input in transaction.inputs:
        encoded.extend(_encode_hash(tx_input.previous_output.txid))
        encoded.extend(pack("<I", tx_input.previous_output.index))
        encoded.extend(_encode_bytes(tx_input.signature))
        encoded.extend(_encode_bytes(tx_input.public_key))
        encoded.extend(pack("<I", tx_input.sequence))
    encoded.extend(_encode_varint(len(transaction.outputs)))
    for tx_output in transaction.outputs:
        encoded.extend(pack("<Q", int(tx_output.value)))
        encoded.extend(_encode_string(tx_output.recipient))
    encoded.extend(pack("<I", transaction.locktime))
    encoded.extend(_encode_metadata(transaction.metadata))
    return bytes(encoded)


def serialize_transaction_for_signing(
    transaction: Transaction,
    input_index: int,
    *,
    previous_output_value: int,
    previous_output_recipient: str,
) -> bytes:
    """Serialize the canonical payload signed by one transaction input."""

    if input_index < 0 or input_index >= len(transaction.inputs):
        raise IndexError("Transaction input index is out of range.")
    stripped = Transaction(
        version=transaction.version,
        inputs=tuple(
            TxInput(previous_output=tx_input.previous_output, sequence=tx_input.sequence)
            for tx_input in transaction.inputs
        ),
        outputs=transaction.outputs,
        locktime=transaction.locktime,
        metadata=transaction.metadata,
    )
    encoded = bytearray(serialize_transaction(stripped))
    encoded.extend(pack("<I", input_index))
    encoded.extend(pack("<Q", previous_output_value))
    encoded.extend(_encode_string(previous_output_recipient))
    encoded.extend(pack("<I", 1))
    return bytes(encoded)


def serialize_block_header(header: BlockHeader) -> bytes:
    """Serialize a block header deterministically for PoW hashing."""

    return b"".join(
        [
            pack("<I", header.version),
            _encode_hash(header.previous_block_hash),
            _encode_hash(header.merkle_root),
            pack("<I", header.timestamp),
            pack("<I", header.bits),
            pack("<I", header.nonce),
        ]
    )


def serialize_block(block: Block) -> bytes:
    """Serialize a full block for storage or wire transfer."""

    encoded = bytearray()
    encoded.extend(serialize_block_header(block.header))
    encoded.extend(_encode_varint(len(block.transactions)))
    for transaction in block.transactions:
        encoded.extend(serialize_transaction(transaction))
    return bytes(encoded)


def deserialize_transaction(payload: bytes, offset: int = 0) -> tuple[Transaction, int]:
    """Decode a transaction from bytes and return it with the next offset."""

    version = unpack_from("<I", payload, offset)[0]
    offset += 4
    input_count, offset = _decode_varint(payload, offset)
    inputs = []
    for _ in range(input_count):
        previous_txid = payload[offset : offset + 32].hex()
        offset += 32
        previous_index = unpack_from("<I", payload, offset)[0]
        offset += 4
        signature, offset = _decode_bytes(payload, offset)
        public_key, offset = _decode_bytes(payload, offset)
        sequence = unpack_from("<I", payload, offset)[0]
        offset += 4
        inputs.append(
            TxInput(
                previous_output=OutPoint(txid=previous_txid, index=previous_index),
                signature=signature,
                public_key=public_key,
                sequence=sequence,
            )
        )
    output_count, offset = _decode_varint(payload, offset)
    outputs = []
    for _ in range(output_count):
        value = unpack_from("<Q", payload, offset)[0]
        offset += 8
        recipient, offset = _decode_string(payload, offset)
        outputs.append(TxOutput(value=ChipbitAmount(value), recipient=recipient))
    locktime = unpack_from("<I", payload, offset)[0]
    offset += 4
    metadata, offset = _decode_metadata(payload, offset)
    return (
        Transaction(
            version=version,
            inputs=tuple(inputs),
            outputs=tuple(outputs),
            locktime=locktime,
            metadata=metadata,
        ),
        offset,
    )


def deserialize_block_header(payload: bytes, offset: int = 0) -> tuple[BlockHeader, int]:
    """Decode a block header from bytes and return it with the next offset."""

    header = BlockHeader(
        version=unpack_from("<I", payload, offset)[0],
        previous_block_hash=payload[offset + 4 : offset + 36].hex(),
        merkle_root=payload[offset + 36 : offset + 68].hex(),
        timestamp=unpack_from("<I", payload, offset + 68)[0],
        bits=unpack_from("<I", payload, offset + 72)[0],
        nonce=unpack_from("<I", payload, offset + 76)[0],
    )
    return header, offset + 80


def deserialize_block(payload: bytes, offset: int = 0) -> tuple[Block, int]:
    """Decode a full block from bytes and return it with the next offset."""

    header, offset = deserialize_block_header(payload, offset)
    transaction_count, offset = _decode_varint(payload, offset)
    transactions = []
    for _ in range(transaction_count):
        transaction, offset = deserialize_transaction(payload, offset)
        transactions.append(transaction)
    return Block(header=header, transactions=tuple(transactions)), offset
