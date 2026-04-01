from chipcoin.consensus.models import Block, BlockHeader, OutPoint, Transaction, TxInput, TxOutput
from chipcoin.consensus.serialization import serialize_block, serialize_transaction


def test_transaction_serialization_is_stable() -> None:
    transaction = Transaction(
        version=1,
        inputs=(
            TxInput(
                previous_output=OutPoint(txid="AB" * 32, index=1),
                signature=b"\x01\x02",
                public_key=b"\x03\x04\x05",
                sequence=0xFFFFFFFE,
            ),
        ),
        outputs=(TxOutput(value=25, recipient="CHCabc"),),
        locktime=9,
        metadata={"purpose": "test"},
    )

    first = serialize_transaction(transaction)
    second = serialize_transaction(transaction)

    assert first == second
    assert len(first) > 0


def test_block_serialization_includes_header_and_transactions() -> None:
    transaction = Transaction(
        version=1,
        inputs=(),
        outputs=(TxOutput(value=50, recipient="CHCminer"),),
        metadata={"coinbase": "true"},
    )
    header = BlockHeader(
        version=1,
        previous_block_hash="00" * 32,
        merkle_root="11" * 32,
        timestamp=1,
        bits=0x207FFFFF,
        nonce=0,
    )
    block = Block(header=header, transactions=(transaction,))

    encoded = serialize_block(block)

    assert len(encoded) > 80
    assert encoded[:4] == (1).to_bytes(4, byteorder="little", signed=False)
