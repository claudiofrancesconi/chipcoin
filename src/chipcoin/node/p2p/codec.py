"""Binary framing codec for P2P messages.

Frame layout:

- 4 bytes: network magic
- 12 bytes: ASCII command padded with NUL bytes
- 4 bytes: payload length, little-endian unsigned integer
- 4 bytes: payload checksum = first four bytes of double-SHA256(payload)
- N bytes: typed payload
"""

from __future__ import annotations

from struct import pack, unpack_from

from ...config import MAINNET_CONFIG
from ...consensus.hashes import double_sha256
from ...consensus.serialization import (
    deserialize_block,
    deserialize_block_header,
    deserialize_transaction,
    serialize_block,
    serialize_block_header,
    serialize_transaction,
)
from .errors import ChecksumError, CodecError, MalformedMessageError, WrongNetworkMagicError
from ..messages import (
    AddrMessage,
    BlockMessage,
    EmptyPayload,
    GetBlocksMessage,
    GetDataMessage,
    GetHeadersMessage,
    HeadersMessage,
    InvMessage,
    InventoryVector,
    MessageEnvelope,
    PeerAddress,
    PingMessage,
    PongMessage,
    TransactionMessage,
    VersionMessage,
)


COMMAND_SIZE = 12
FRAME_HEADER_SIZE = 24
DEFAULT_MAGIC = MAINNET_CONFIG.magic
INVENTORY_TYPE_CODES = {"tx": 1, "block": 2}
INVENTORY_TYPE_NAMES = {value: key for key, value in INVENTORY_TYPE_CODES.items()}


def encode_message(message: MessageEnvelope, *, magic: bytes = DEFAULT_MAGIC) -> bytes:
    """Encode a typed message into a framed binary payload."""

    if len(magic) != 4:
        raise CodecError("Network magic must be exactly four bytes.")

    payload = _encode_payload(message)
    command = _encode_command(message.command)
    checksum = double_sha256(payload)[:4]
    return b"".join(
        [
            magic,
            command,
            pack("<I", len(payload)),
            checksum,
            payload,
        ]
    )


def decode_message(frame: bytes, *, expected_magic: bytes = DEFAULT_MAGIC) -> MessageEnvelope:
    """Decode a framed binary payload into a typed message envelope."""

    if len(frame) < FRAME_HEADER_SIZE:
        raise MalformedMessageError("Frame is shorter than the fixed header size.")

    magic = frame[:4]
    if magic != expected_magic:
        raise WrongNetworkMagicError("Unexpected network magic.")

    command = _decode_command(frame[4 : 4 + COMMAND_SIZE])
    payload_length = unpack_from("<I", frame, 16)[0]
    checksum = frame[20:24]
    payload = frame[24:]
    if len(payload) != payload_length:
        raise MalformedMessageError("Frame payload length does not match header length.")
    if double_sha256(payload)[:4] != checksum:
        raise ChecksumError("Frame checksum does not match payload.")
    return _decode_payload(command, payload)


def _encode_payload(message: MessageEnvelope) -> bytes:
    """Encode a typed payload for a given command."""

    command = message.command
    payload = message.payload

    if command in {"verack", "getaddr"}:
        if not isinstance(payload, EmptyPayload):
            raise CodecError(f"{command} expects an empty payload.")
        return b""
    if command == "version":
        if not isinstance(payload, VersionMessage):
            raise CodecError("version expects VersionMessage payload.")
        return b"".join(
            [
                pack("<I", payload.protocol_version),
                _encode_string(payload.network),
                _encode_string(payload.node_id),
                pack("<I", payload.start_height),
                _encode_string(payload.user_agent),
                pack("<?", payload.relay),
            ]
        )
    if command == "ping":
        if not isinstance(payload, PingMessage):
            raise CodecError("ping expects PingMessage payload.")
        return pack("<Q", payload.nonce)
    if command == "pong":
        if not isinstance(payload, PongMessage):
            raise CodecError("pong expects PongMessage payload.")
        return pack("<Q", payload.nonce)
    if command == "inv":
        if not isinstance(payload, InvMessage):
            raise CodecError("inv expects InvMessage payload.")
        return _encode_inventory(payload.items)
    if command == "getdata":
        if not isinstance(payload, GetDataMessage):
            raise CodecError("getdata expects GetDataMessage payload.")
        return _encode_inventory(payload.items)
    if command == "tx":
        if not isinstance(payload, TransactionMessage):
            raise CodecError("tx expects TransactionMessage payload.")
        return serialize_transaction(payload.transaction)
    if command == "block":
        if not isinstance(payload, BlockMessage):
            raise CodecError("block expects BlockMessage payload.")
        return serialize_block(payload.block)
    if command == "getheaders":
        if not isinstance(payload, GetHeadersMessage):
            raise CodecError("getheaders expects GetHeadersMessage payload.")
        return _encode_hash_locator(payload.protocol_version, payload.locator_hashes, payload.stop_hash)
    if command == "headers":
        if not isinstance(payload, HeadersMessage):
            raise CodecError("headers expects HeadersMessage payload.")
        encoded = bytearray()
        encoded.extend(_encode_varint(len(payload.headers)))
        for header in payload.headers:
            encoded.extend(serialize_block_header(header))
        return bytes(encoded)
    if command == "getblocks":
        if not isinstance(payload, GetBlocksMessage):
            raise CodecError("getblocks expects GetBlocksMessage payload.")
        return _encode_hash_locator(payload.protocol_version, payload.locator_hashes, payload.stop_hash)
    if command == "addr":
        if not isinstance(payload, AddrMessage):
            raise CodecError("addr expects AddrMessage payload.")
        encoded = bytearray()
        encoded.extend(_encode_varint(len(payload.addresses)))
        for address in payload.addresses:
            encoded.extend(pack("<I", address.timestamp))
            encoded.extend(pack("<Q", address.services))
            encoded.extend(_encode_string(address.host))
            encoded.extend(pack("<H", address.port))
        return bytes(encoded)
    raise CodecError(f"Unsupported command: {command}")


def _decode_payload(command: str, payload: bytes) -> MessageEnvelope:
    """Decode a typed payload for a command."""

    if command in {"verack", "getaddr"}:
        return MessageEnvelope(command=command, payload=EmptyPayload())
    if command == "version":
        protocol_version = unpack_from("<I", payload, 0)[0]
        offset = 4
        network, offset = _decode_string(payload, offset)
        node_id, offset = _decode_string(payload, offset)
        start_height = unpack_from("<I", payload, offset)[0]
        offset += 4
        user_agent, offset = _decode_string(payload, offset)
        relay = bool(unpack_from("<?", payload, offset)[0])
        offset += 1
        _ensure_fully_consumed(command, payload, offset)
        return MessageEnvelope(
            command="version",
            payload=VersionMessage(
                protocol_version=protocol_version,
                network=network,
                node_id=node_id,
                start_height=start_height,
                user_agent=user_agent,
                relay=relay,
            ),
        )
    if command == "ping":
        return _decode_fixed_nonce(command="ping", payload=payload, message_type=PingMessage)
    if command == "pong":
        return _decode_fixed_nonce(command="pong", payload=payload, message_type=PongMessage)
    if command == "inv":
        items, offset = _decode_inventory(payload, 0)
        _ensure_fully_consumed(command, payload, offset)
        return MessageEnvelope(command="inv", payload=InvMessage(items=tuple(items)))
    if command == "getdata":
        items, offset = _decode_inventory(payload, 0)
        _ensure_fully_consumed(command, payload, offset)
        return MessageEnvelope(command="getdata", payload=GetDataMessage(items=tuple(items)))
    if command == "tx":
        transaction, offset = deserialize_transaction(payload)
        _ensure_fully_consumed(command, payload, offset)
        return MessageEnvelope(command="tx", payload=TransactionMessage(transaction=transaction))
    if command == "block":
        block, offset = deserialize_block(payload)
        _ensure_fully_consumed(command, payload, offset)
        return MessageEnvelope(command="block", payload=BlockMessage(block=block))
    if command == "getheaders":
        version, locator_hashes, stop_hash, offset = _decode_hash_locator(payload, 0)
        _ensure_fully_consumed(command, payload, offset)
        return MessageEnvelope(
            command="getheaders",
            payload=GetHeadersMessage(
                protocol_version=version,
                locator_hashes=tuple(locator_hashes),
                stop_hash=stop_hash,
            ),
        )
    if command == "headers":
        count, offset = _decode_varint(payload, 0)
        headers = []
        for _ in range(count):
            header, offset = deserialize_block_header(payload, offset)
            headers.append(header)
        _ensure_fully_consumed(command, payload, offset)
        return MessageEnvelope(command="headers", payload=HeadersMessage(headers=tuple(headers)))
    if command == "getblocks":
        version, locator_hashes, stop_hash, offset = _decode_hash_locator(payload, 0)
        _ensure_fully_consumed(command, payload, offset)
        return MessageEnvelope(
            command="getblocks",
            payload=GetBlocksMessage(
                protocol_version=version,
                locator_hashes=tuple(locator_hashes),
                stop_hash=stop_hash,
            ),
        )
    if command == "addr":
        count, offset = _decode_varint(payload, 0)
        addresses = []
        for _ in range(count):
            timestamp = unpack_from("<I", payload, offset)[0]
            offset += 4
            services = unpack_from("<Q", payload, offset)[0]
            offset += 8
            host, offset = _decode_string(payload, offset)
            port = unpack_from("<H", payload, offset)[0]
            offset += 2
            addresses.append(PeerAddress(host=host, port=port, services=services, timestamp=timestamp))
        _ensure_fully_consumed(command, payload, offset)
        return MessageEnvelope(command="addr", payload=AddrMessage(addresses=tuple(addresses)))
    raise CodecError(f"Unsupported command: {command}")


def _encode_command(command: str) -> bytes:
    """Encode a command into its fixed-width frame header field."""

    encoded = command.encode("ascii")
    if len(encoded) > COMMAND_SIZE:
        raise CodecError("Command name exceeds fixed-width header field.")
    return encoded.ljust(COMMAND_SIZE, b"\x00")


def _decode_command(command_bytes: bytes) -> str:
    """Decode an ASCII command from a fixed-width header field."""

    command = command_bytes.rstrip(b"\x00").decode("ascii")
    if not command:
        raise CodecError("Frame command is empty.")
    return command


def _encode_varint(value: int) -> bytes:
    """Encode a non-negative integer using compact varint representation."""

    if value < 0:
        raise CodecError("varint value cannot be negative")
    if value < 0xFD:
        return bytes((value,))
    if value <= 0xFFFF:
        return b"\xFD" + pack("<H", value)
    if value <= 0xFFFFFFFF:
        return b"\xFE" + pack("<I", value)
    if value <= 0xFFFFFFFFFFFFFFFF:
        return b"\xFF" + pack("<Q", value)
    raise CodecError("varint value exceeds 64-bit range")


def _decode_varint(buffer: bytes, offset: int) -> tuple[int, int]:
    """Decode a compact varint from a buffer."""

    if offset >= len(buffer):
        raise CodecError("Unexpected end of payload while decoding varint.")
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
    """Encode raw bytes using a varint length prefix."""

    return _encode_varint(len(value)) + value


def _decode_bytes(buffer: bytes, offset: int) -> tuple[bytes, int]:
    """Decode raw bytes using a varint length prefix."""

    length, offset = _decode_varint(buffer, offset)
    end = offset + length
    if end > len(buffer):
        raise CodecError("Unexpected end of payload while decoding bytes.")
    return buffer[offset:end], end


def _encode_string(value: str) -> bytes:
    """Encode a UTF-8 string using a varint length prefix."""

    return _encode_bytes(value.encode("utf-8"))


def _decode_string(buffer: bytes, offset: int) -> tuple[str, int]:
    """Decode a UTF-8 string using a varint length prefix."""

    raw, offset = _decode_bytes(buffer, offset)
    return raw.decode("utf-8"), offset


def _encode_hash(hash_hex: str) -> bytes:
    """Encode a 32-byte hash from hexadecimal."""

    raw = bytes.fromhex(hash_hex)
    if len(raw) != 32:
        raise CodecError("Hashes must be exactly 32 bytes.")
    return raw


def _decode_hash(buffer: bytes, offset: int) -> tuple[str, int]:
    """Decode a 32-byte hash into hexadecimal form."""

    end = offset + 32
    if end > len(buffer):
        raise CodecError("Unexpected end of payload while decoding hash.")
    return buffer[offset:end].hex(), end


def _encode_inventory(items: tuple[InventoryVector, ...]) -> bytes:
    """Encode an inventory vector list."""

    encoded = bytearray()
    encoded.extend(_encode_varint(len(items)))
    for item in items:
        encoded.extend(pack("<I", INVENTORY_TYPE_CODES[item.object_type]))
        encoded.extend(_encode_hash(item.object_hash))
    return bytes(encoded)


def _decode_inventory(buffer: bytes, offset: int) -> tuple[list[InventoryVector], int]:
    """Decode an inventory vector list."""

    count, offset = _decode_varint(buffer, offset)
    items = []
    for _ in range(count):
        object_type_code = unpack_from("<I", buffer, offset)[0]
        offset += 4
        object_hash, offset = _decode_hash(buffer, offset)
        try:
            object_type = INVENTORY_TYPE_NAMES[object_type_code]
        except KeyError as exc:
            raise CodecError(f"Unknown inventory type code: {object_type_code}") from exc
        items.append(InventoryVector(object_type=object_type, object_hash=object_hash))
    return items, offset


def _encode_hash_locator(protocol_version: int, locator_hashes: tuple[str, ...], stop_hash: str) -> bytes:
    """Encode a block-locator based request payload."""

    encoded = bytearray()
    encoded.extend(pack("<I", protocol_version))
    encoded.extend(_encode_varint(len(locator_hashes)))
    for locator_hash in locator_hashes:
        encoded.extend(_encode_hash(locator_hash))
    encoded.extend(_encode_hash(stop_hash))
    return bytes(encoded)


def _decode_hash_locator(buffer: bytes, offset: int) -> tuple[int, list[str], str, int]:
    """Decode a block-locator based request payload."""

    protocol_version = unpack_from("<I", buffer, offset)[0]
    offset += 4
    count, offset = _decode_varint(buffer, offset)
    locator_hashes = []
    for _ in range(count):
        locator_hash, offset = _decode_hash(buffer, offset)
        locator_hashes.append(locator_hash)
    stop_hash, offset = _decode_hash(buffer, offset)
    return protocol_version, locator_hashes, stop_hash, offset


def _decode_fixed_nonce(*, command: str, payload: bytes, message_type):
    """Decode a fixed-size 64-bit nonce payload."""

    if len(payload) != 8:
        raise CodecError(f"{command} payload must be exactly eight bytes.")
    nonce = unpack_from("<Q", payload, 0)[0]
    return MessageEnvelope(command=command, payload=message_type(nonce=nonce))


def _ensure_fully_consumed(command: str, payload: bytes, offset: int) -> None:
    """Reject payloads that contain trailing undecoded bytes."""

    if offset != len(payload):
        raise CodecError(f"{command} payload contains trailing bytes.")
