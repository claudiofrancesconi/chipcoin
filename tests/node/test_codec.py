from chipcoin.consensus.merkle import merkle_root
from chipcoin.consensus.models import Block, BlockHeader, OutPoint, Transaction, TxInput, TxOutput
from chipcoin.config import DEVNET_CONFIG, MAINNET_CONFIG
from chipcoin.node.messages import (
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
from chipcoin.node.p2p.codec import CodecError, decode_message, encode_message
from chipcoin.node.p2p.errors import ChecksumError, WrongNetworkMagicError


def _sample_transaction() -> Transaction:
    return Transaction(
        version=1,
        inputs=(
            TxInput(
                previous_output=OutPoint(txid="11" * 32, index=0),
                signature=b"\x30\x44",
                public_key=b"\x02" + (b"\x66" * 32),
            ),
        ),
        outputs=(TxOutput(value=90, recipient="CHCrecipient"),),
        metadata={"kind": "payment"},
    )


def _sample_block() -> Block:
    transaction = _sample_transaction()
    header = BlockHeader(
        version=1,
        previous_block_hash="00" * 32,
        merkle_root=merkle_root([transaction.txid()]),
        timestamp=1_700_000_000,
        bits=0x207FFFFF,
        nonce=0,
    )
    return Block(header=header, transactions=(transaction,))


def test_version_message_roundtrip() -> None:
    message = MessageEnvelope(
        command="version",
        payload=VersionMessage(
            protocol_version=70015,
            network="mainnet",
            node_id="node-a",
            start_height=42,
            user_agent="/chipcoin:0.1.0/",
            relay=True,
        ),
    )

    decoded = decode_message(encode_message(message, magic=MAINNET_CONFIG.magic), expected_magic=MAINNET_CONFIG.magic)

    assert decoded == message


def test_empty_message_roundtrip_for_verack_and_getaddr() -> None:
    verack = MessageEnvelope(command="verack", payload=EmptyPayload())
    getaddr = MessageEnvelope(command="getaddr", payload=EmptyPayload())

    assert decode_message(encode_message(verack, magic=MAINNET_CONFIG.magic), expected_magic=MAINNET_CONFIG.magic) == verack
    assert decode_message(encode_message(getaddr, magic=MAINNET_CONFIG.magic), expected_magic=MAINNET_CONFIG.magic) == getaddr


def test_ping_and_pong_roundtrip() -> None:
    ping = MessageEnvelope(command="ping", payload=PingMessage(nonce=12345))
    pong = MessageEnvelope(command="pong", payload=PongMessage(nonce=12345))

    assert decode_message(encode_message(ping, magic=MAINNET_CONFIG.magic), expected_magic=MAINNET_CONFIG.magic) == ping
    assert decode_message(encode_message(pong, magic=MAINNET_CONFIG.magic), expected_magic=MAINNET_CONFIG.magic) == pong


def test_inventory_messages_roundtrip() -> None:
    items = (
        InventoryVector(object_type="tx", object_hash="22" * 32),
        InventoryVector(object_type="block", object_hash="33" * 32),
    )
    inv = MessageEnvelope(command="inv", payload=InvMessage(items=items))
    getdata = MessageEnvelope(command="getdata", payload=GetDataMessage(items=items))

    assert decode_message(encode_message(inv, magic=MAINNET_CONFIG.magic), expected_magic=MAINNET_CONFIG.magic) == inv
    assert decode_message(encode_message(getdata, magic=MAINNET_CONFIG.magic), expected_magic=MAINNET_CONFIG.magic) == getdata


def test_transaction_and_block_messages_roundtrip() -> None:
    transaction = _sample_transaction()
    block = _sample_block()
    tx_message = MessageEnvelope(command="tx", payload=TransactionMessage(transaction=transaction))
    block_message = MessageEnvelope(command="block", payload=BlockMessage(block=block))

    assert decode_message(encode_message(tx_message, magic=MAINNET_CONFIG.magic), expected_magic=MAINNET_CONFIG.magic) == tx_message
    assert decode_message(encode_message(block_message, magic=MAINNET_CONFIG.magic), expected_magic=MAINNET_CONFIG.magic) == block_message


def test_header_locator_messages_roundtrip() -> None:
    locator_hashes = ("44" * 32, "55" * 32)
    stop_hash = "66" * 32
    getheaders = MessageEnvelope(
        command="getheaders",
        payload=GetHeadersMessage(
            protocol_version=70015,
            locator_hashes=locator_hashes,
            stop_hash=stop_hash,
        ),
    )
    getblocks = MessageEnvelope(
        command="getblocks",
        payload=GetBlocksMessage(
            protocol_version=70015,
            locator_hashes=locator_hashes,
            stop_hash=stop_hash,
        ),
    )

    assert decode_message(encode_message(getheaders, magic=MAINNET_CONFIG.magic), expected_magic=MAINNET_CONFIG.magic) == getheaders
    assert decode_message(encode_message(getblocks, magic=MAINNET_CONFIG.magic), expected_magic=MAINNET_CONFIG.magic) == getblocks


def test_headers_message_roundtrip() -> None:
    header_one = BlockHeader(
        version=1,
        previous_block_hash="77" * 32,
        merkle_root="88" * 32,
        timestamp=100,
        bits=0x207FFFFF,
        nonce=1,
    )
    header_two = BlockHeader(
        version=1,
        previous_block_hash="99" * 32,
        merkle_root="aa" * 32,
        timestamp=101,
        bits=0x207FFFFF,
        nonce=2,
    )
    message = MessageEnvelope(command="headers", payload=HeadersMessage(headers=(header_one, header_two)))

    assert decode_message(encode_message(message, magic=MAINNET_CONFIG.magic), expected_magic=MAINNET_CONFIG.magic) == message


def test_addr_message_roundtrip() -> None:
    message = MessageEnvelope(
        command="addr",
        payload=AddrMessage(
            addresses=(
                PeerAddress(host="127.0.0.1", port=8333, services=1, timestamp=1000),
                PeerAddress(host="seed.example.org", port=8333, services=0, timestamp=1001),
            )
        ),
    )

    assert decode_message(encode_message(message, magic=MAINNET_CONFIG.magic), expected_magic=MAINNET_CONFIG.magic) == message


def test_decode_rejects_bad_checksum() -> None:
    message = MessageEnvelope(command="ping", payload=PingMessage(nonce=7))
    frame = bytearray(encode_message(message, magic=MAINNET_CONFIG.magic))
    frame[-1] ^= 0x01

    try:
        decode_message(bytes(frame), expected_magic=MAINNET_CONFIG.magic)
    except ChecksumError:
        return
    raise AssertionError("Expected checksum validation to fail.")


def test_decode_rejects_wrong_magic() -> None:
    message = MessageEnvelope(command="ping", payload=PingMessage(nonce=7))
    frame = bytearray(encode_message(message, magic=MAINNET_CONFIG.magic))
    frame[0] ^= 0x01

    try:
        decode_message(bytes(frame), expected_magic=MAINNET_CONFIG.magic)
    except WrongNetworkMagicError:
        return
    raise AssertionError("Expected magic validation to fail.")


def test_devnet_magic_roundtrip() -> None:
    message = MessageEnvelope(command="ping", payload=PingMessage(nonce=7))

    decoded = decode_message(
        encode_message(message, magic=DEVNET_CONFIG.magic),
        expected_magic=DEVNET_CONFIG.magic,
    )

    assert decoded == message


def test_devnet_frame_is_rejected_by_mainnet_magic() -> None:
    message = MessageEnvelope(command="ping", payload=PingMessage(nonce=7))
    frame = encode_message(message, magic=DEVNET_CONFIG.magic)

    try:
        decode_message(frame, expected_magic=MAINNET_CONFIG.magic)
    except WrongNetworkMagicError:
        return
    raise AssertionError("Expected wrong-network magic validation to fail.")
