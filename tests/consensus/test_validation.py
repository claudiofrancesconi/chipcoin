from chipcoin.consensus.economics import block_subsidy, terminal_correction_height
from chipcoin.consensus.merkle import merkle_root
from chipcoin.consensus.models import Block, BlockHeader, OutPoint, Transaction, TxInput, TxOutput
from chipcoin.consensus.params import MAINNET_PARAMS
from chipcoin.consensus.utxo import InMemoryUtxoView, UtxoEntry
from chipcoin.consensus.validation import (
    ContextualValidationError,
    StatelessValidationError,
    ValidationContext,
    is_coinbase_mature,
    transaction_signature_digest,
    validate_block,
    validate_transaction,
)
from chipcoin.wallet.signer import TransactionSigner
from tests.helpers import signed_payment, wallet_key


def _expect_raises(exc_type: type[BaseException], fn) -> None:
    try:
        fn()
    except exc_type:
        return
    raise AssertionError(f"Expected {exc_type.__name__} to be raised.")


def _mine_easy_header(previous_block_hash: str, merkle_root_hex: str, *, timestamp: int = 1_700_000_000) -> BlockHeader:
    for nonce in range(2_000_000):
        header = BlockHeader(
            version=1,
            previous_block_hash=previous_block_hash,
            merkle_root=merkle_root_hex,
            timestamp=timestamp,
            bits=MAINNET_PARAMS.genesis_bits,
            nonce=nonce,
        )
        from chipcoin.consensus.pow import verify_proof_of_work

        if verify_proof_of_work(header):
            return header
    raise AssertionError("Expected to find a valid header nonce for the easy target.")


def _coinbase_transaction(value: int, recipient: str = "CHCminer") -> Transaction:
    return Transaction(
        version=1,
        inputs=(),
        outputs=(TxOutput(value=value, recipient=recipient),),
        metadata={"coinbase": "true"},
    )


def _spend_transaction(previous_outpoint: OutPoint, *, input_value: int = 100, fee: int = 10, sender=None) -> Transaction:
    return signed_payment(previous_outpoint, value=input_value, sender=sender or wallet_key(0), fee=fee)


def test_validate_transaction_accepts_balanced_spend_and_returns_fee() -> None:
    previous_outpoint = OutPoint(txid="11" * 32, index=0)
    sender = wallet_key(0)
    utxo_view = InMemoryUtxoView.from_entries(
        [
            (
                previous_outpoint,
                UtxoEntry(
                    output=TxOutput(value=100, recipient=sender.address),
                    height=1,
                    is_coinbase=False,
                ),
            )
        ]
    )
    transaction = _spend_transaction(previous_outpoint, fee=10, sender=sender)
    context = ValidationContext(
        height=5,
        median_time_past=0,
        params=MAINNET_PARAMS,
        utxo_view=utxo_view,
    )

    fee = validate_transaction(transaction, context)

    assert fee == 10


def test_validate_transaction_rejects_overspend() -> None:
    previous_outpoint = OutPoint(txid="22" * 32, index=0)
    sender = wallet_key(0)
    utxo_view = InMemoryUtxoView.from_entries(
        [
            (
                previous_outpoint,
                UtxoEntry(
                    output=TxOutput(value=50, recipient=sender.address),
                    height=1,
                    is_coinbase=False,
                ),
            )
        ]
    )
    signer = TransactionSigner(sender)
    unsigned = Transaction(
        version=1,
        inputs=(TxInput(previous_output=previous_outpoint),),
        outputs=(TxOutput(value=51, recipient=wallet_key(1).address),),
        metadata={"kind": "payment"},
    )
    digest = transaction_signature_digest(
        unsigned,
        0,
        previous_output=TxOutput(value=50, recipient=sender.address),
    )
    transaction = Transaction(
        version=unsigned.version,
        inputs=(
            TxInput(
                previous_output=previous_outpoint,
                signature=signer.sign(digest),
                public_key=sender.public_key,
            ),
        ),
        outputs=unsigned.outputs,
        locktime=unsigned.locktime,
        metadata=unsigned.metadata,
    )
    context = ValidationContext(
        height=2,
        median_time_past=0,
        params=MAINNET_PARAMS,
        utxo_view=utxo_view,
    )

    _expect_raises(ContextualValidationError, lambda: validate_transaction(transaction, context))


def test_validate_transaction_rejects_duplicate_inputs_statelessly() -> None:
    sender = wallet_key(0)
    previous_outpoint = OutPoint(txid="33" * 32, index=0)
    transaction = Transaction(
        version=1,
        inputs=(
            TxInput(previous_output=previous_outpoint, signature=b"\x01", public_key=sender.public_key),
            TxInput(previous_output=previous_outpoint, signature=b"\x01", public_key=sender.public_key),
        ),
        outputs=(TxOutput(value=1, recipient=wallet_key(1).address),),
    )
    context = ValidationContext(
        height=1,
        median_time_past=0,
        params=MAINNET_PARAMS,
        utxo_view=InMemoryUtxoView(),
    )

    _expect_raises(StatelessValidationError, lambda: validate_transaction(transaction, context))


def test_coinbase_maturity_rule_is_enforced_separately() -> None:
    entry = UtxoEntry(
        output=TxOutput(value=50, recipient="CHCminer"),
        height=100,
        is_coinbase=True,
    )

    assert is_coinbase_mature(entry, 200, MAINNET_PARAMS) is True
    assert is_coinbase_mature(entry, 199, MAINNET_PARAMS) is False


def test_validate_block_accepts_valid_coinbase_and_fee_accounting() -> None:
    previous_outpoint = OutPoint(txid="44" * 32, index=0)
    sender = wallet_key(0)
    spend_entry = UtxoEntry(
        output=TxOutput(value=100, recipient=sender.address),
        height=1,
        is_coinbase=False,
    )
    spend_transaction = _spend_transaction(previous_outpoint, fee=10, sender=sender)
    fees = 10
    coinbase = _coinbase_transaction(block_subsidy(5, MAINNET_PARAMS) + fees)
    transactions = (coinbase, spend_transaction)
    header = _mine_easy_header("55" * 32, merkle_root([tx.txid() for tx in transactions]))
    block = Block(header=header, transactions=transactions)
    context = ValidationContext(
        height=5,
        median_time_past=1_699_999_000,
        params=MAINNET_PARAMS,
        utxo_view=InMemoryUtxoView.from_entries([(previous_outpoint, spend_entry)]),
        expected_previous_block_hash="55" * 32,
        expected_bits=MAINNET_PARAMS.genesis_bits,
    )

    total_fees = validate_block(block, context)

    assert total_fees == fees


def test_validate_block_rejects_coinbase_overclaim() -> None:
    coinbase = _coinbase_transaction(block_subsidy(3, MAINNET_PARAMS) + 1)
    transactions = (coinbase,)
    header = _mine_easy_header("66" * 32, merkle_root([tx.txid() for tx in transactions]))
    block = Block(header=header, transactions=transactions)
    context = ValidationContext(
        height=3,
        median_time_past=1_699_999_000,
        params=MAINNET_PARAMS,
        utxo_view=InMemoryUtxoView(),
        expected_previous_block_hash="66" * 32,
        expected_bits=MAINNET_PARAMS.genesis_bits,
    )

    _expect_raises(ContextualValidationError, lambda: validate_block(block, context))


def test_validate_block_accepts_terminal_correction_coinbase() -> None:
    height = terminal_correction_height(MAINNET_PARAMS)
    coinbase = _coinbase_transaction(block_subsidy(height, MAINNET_PARAMS))
    transactions = (coinbase,)
    header = _mine_easy_header("67" * 32, merkle_root([tx.txid() for tx in transactions]))
    block = Block(header=header, transactions=transactions)
    context = ValidationContext(
        height=height,
        median_time_past=1_699_999_000,
        params=MAINNET_PARAMS,
        utxo_view=InMemoryUtxoView(),
        expected_previous_block_hash="67" * 32,
        expected_bits=MAINNET_PARAMS.genesis_bits,
    )

    assert validate_block(block, context) == 0


def test_validate_block_rejects_double_spend_within_block() -> None:
    previous_outpoint = OutPoint(txid="77" * 32, index=0)
    sender = wallet_key(0)
    utxo_entry = UtxoEntry(
        output=TxOutput(value=100, recipient=sender.address),
        height=1,
        is_coinbase=False,
    )
    first_spend = _spend_transaction(previous_outpoint, fee=10, sender=sender)
    second_spend = _spend_transaction(previous_outpoint, fee=20, sender=sender)
    coinbase = _coinbase_transaction(block_subsidy(6, MAINNET_PARAMS) + 30)
    transactions = (coinbase, first_spend, second_spend)
    header = _mine_easy_header("88" * 32, merkle_root([tx.txid() for tx in transactions]))
    block = Block(header=header, transactions=transactions)
    context = ValidationContext(
        height=6,
        median_time_past=1_699_999_000,
        params=MAINNET_PARAMS,
        utxo_view=InMemoryUtxoView.from_entries([(previous_outpoint, utxo_entry)]),
        expected_previous_block_hash="88" * 32,
        expected_bits=MAINNET_PARAMS.genesis_bits,
    )

    _expect_raises(ContextualValidationError, lambda: validate_block(block, context))


def test_validate_block_rejects_immature_coinbase_spend() -> None:
    matured_height = 50
    previous_outpoint = OutPoint(txid="99" * 32, index=0)
    sender = wallet_key(0)
    utxo_entry = UtxoEntry(
        output=TxOutput(value=50, recipient=sender.address),
        height=matured_height,
        is_coinbase=True,
    )
    spend = signed_payment(previous_outpoint, value=50, sender=sender, amount=40, fee=10)
    coinbase = _coinbase_transaction(block_subsidy(120, MAINNET_PARAMS) + 10)
    transactions = (coinbase, spend)
    header = _mine_easy_header("AA" * 32, merkle_root([tx.txid() for tx in transactions]))
    block = Block(header=header, transactions=transactions)
    context = ValidationContext(
        height=120,
        median_time_past=1_699_999_000,
        params=MAINNET_PARAMS,
        utxo_view=InMemoryUtxoView.from_entries([(previous_outpoint, utxo_entry)]),
        expected_previous_block_hash="AA" * 32,
        expected_bits=MAINNET_PARAMS.genesis_bits,
    )

    _expect_raises(ContextualValidationError, lambda: validate_block(block, context))


def test_validate_transaction_rejects_invalid_signature() -> None:
    previous_outpoint = OutPoint(txid="AB" * 32, index=0)
    sender = wallet_key(0)
    utxo_view = InMemoryUtxoView.from_entries(
        [
            (
                previous_outpoint,
                UtxoEntry(
                    output=TxOutput(value=100, recipient=sender.address),
                    height=1,
                    is_coinbase=False,
                ),
            )
        ]
    )
    valid_transaction = signed_payment(previous_outpoint, value=100, sender=sender, fee=10)
    invalid_input = TxInput(
        previous_output=valid_transaction.inputs[0].previous_output,
        signature=valid_transaction.inputs[0].signature[:-1] + bytes((valid_transaction.inputs[0].signature[-1] ^ 0x01,)),
        public_key=valid_transaction.inputs[0].public_key,
        sequence=valid_transaction.inputs[0].sequence,
    )
    invalid_transaction = Transaction(
        version=valid_transaction.version,
        inputs=(invalid_input,),
        outputs=valid_transaction.outputs,
        locktime=valid_transaction.locktime,
        metadata=valid_transaction.metadata,
    )
    context = ValidationContext(
        height=5,
        median_time_past=0,
        params=MAINNET_PARAMS,
        utxo_view=utxo_view,
    )

    _expect_raises(ContextualValidationError, lambda: validate_transaction(invalid_transaction, context))
