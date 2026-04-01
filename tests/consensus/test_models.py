from chipcoin.consensus.models import Block, BlockHeader, OutPoint, Transaction, TxInput, TxOutput
from chipcoin.consensus.serialization import serialize_block_header, serialize_transaction


def _sample_transaction(*, metadata: dict[str, str] | None = None) -> Transaction:
    return Transaction(
        version=1,
        inputs=(
            TxInput(
                previous_output=OutPoint(txid="11" * 32, index=2),
                signature=b"\x30\x44",
                public_key=b"\x02" + (b"\x99" * 32),
            ),
        ),
        outputs=(
            TxOutput(value=1250, recipient="CHCrecipient1"),
            TxOutput(value=500, recipient="CHCrecipient2"),
        ),
        locktime=0,
        metadata=metadata or {},
    )


def test_transaction_id_is_deterministic_across_metadata_insertion_order() -> None:
    tx_a = _sample_transaction(metadata={"note": "alpha", "kind": "payment"})
    tx_b = _sample_transaction(metadata={"kind": "payment", "note": "alpha"})

    assert tx_a.txid() == tx_b.txid()
    assert serialize_transaction(tx_a) == serialize_transaction(tx_b)


def test_block_hash_delegates_to_header_hash() -> None:
    header = BlockHeader(
        version=1,
        previous_block_hash="00" * 32,
        merkle_root="22" * 32,
        timestamp=1_700_000_000,
        bits=0x207FFFFF,
        nonce=7,
    )
    block = Block(header=header, transactions=(_sample_transaction(),))

    assert block.block_hash() == header.block_hash()


def test_block_header_serialization_has_fixed_size() -> None:
    header = BlockHeader(
        version=1,
        previous_block_hash="AA" * 32,
        merkle_root="BB" * 32,
        timestamp=123456,
        bits=0x1D00FFFF,
        nonce=42,
    )

    assert len(serialize_block_header(header)) == 80
