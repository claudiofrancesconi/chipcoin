from chipcoin.consensus.models import Block, BlockHeader, OutPoint, Transaction, TxInput, TxOutput
from chipcoin.consensus.utxo import InMemoryUtxoView, UtxoEntry


def test_apply_transaction_spends_inputs_and_creates_outputs() -> None:
    previous_outpoint = OutPoint(txid="11" * 32, index=0)
    view = InMemoryUtxoView.from_entries(
        [
            (
                previous_outpoint,
                UtxoEntry(
                    output=TxOutput(value=75, recipient="CHCalice"),
                    height=3,
                    is_coinbase=False,
                ),
            )
        ]
    )
    transaction = Transaction(
        version=1,
        inputs=(
            TxInput(
                previous_output=previous_outpoint,
                signature=b"\x01",
                public_key=b"\x02",
            ),
        ),
        outputs=(
            TxOutput(value=50, recipient="CHCbob"),
            TxOutput(value=20, recipient="CHCchange"),
        ),
    )

    view.apply_transaction(transaction, height=4)

    assert view.get(previous_outpoint) is None
    assert view.get(OutPoint(txid=transaction.txid(), index=0)) is not None
    assert view.get(OutPoint(txid=transaction.txid(), index=1)) is not None


def test_apply_block_marks_first_transaction_outputs_as_coinbase() -> None:
    coinbase = Transaction(
        version=1,
        inputs=(),
        outputs=(TxOutput(value=50, recipient="CHCminer"),),
        metadata={"coinbase": "true"},
    )
    regular = Transaction(
        version=1,
        inputs=(),
        outputs=(TxOutput(value=1, recipient="CHCnobody"),),
    )
    block = Block(
        header=BlockHeader(
            version=1,
            previous_block_hash="00" * 32,
            merkle_root="11" * 32,
            timestamp=1,
            bits=0x207FFFFF,
            nonce=0,
        ),
        transactions=(coinbase, regular),
    )
    view = InMemoryUtxoView()

    view.apply_block(block, height=9)

    assert view.get(OutPoint(txid=coinbase.txid(), index=0)).is_coinbase is True
    assert view.get(OutPoint(txid=regular.txid(), index=0)).is_coinbase is False
