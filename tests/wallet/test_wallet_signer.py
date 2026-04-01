from chipcoin.consensus.models import OutPoint, TxOutput
from chipcoin.consensus.validation import ValidationContext, validate_transaction
from chipcoin.consensus.params import MAINNET_PARAMS
from chipcoin.consensus.utxo import InMemoryUtxoView, UtxoEntry
from chipcoin.wallet.selection import select_inputs
from chipcoin.wallet.signer import TransactionSigner
from tests.helpers import spend_candidates_for_wallet, wallet_key


def test_select_inputs_returns_change_amount() -> None:
    candidates = [
        *spend_candidates_for_wallet(OutPoint(txid="11" * 32, index=0), value=30),
        *spend_candidates_for_wallet(OutPoint(txid="22" * 32, index=0), value=80),
    ]

    selection = select_inputs(candidates, 90)

    assert selection.total_input_chipbits == 110
    assert selection.change_chipbits == 20
    assert len(selection.selected) == 2


def test_transaction_signer_builds_valid_signed_transaction() -> None:
    owner = wallet_key(0)
    recipient = wallet_key(1).address
    outpoint = OutPoint(txid="33" * 32, index=0)
    signer = TransactionSigner(owner)
    built = signer.build_signed_transaction(
        spend_candidates=spend_candidates_for_wallet(outpoint, value=125, owner=owner),
        recipient=recipient,
        amount_chipbits=100,
        fee_chipbits=5,
        metadata={"kind": "payment"},
    )
    context = ValidationContext(
        height=2,
        median_time_past=0,
        params=MAINNET_PARAMS,
        utxo_view=InMemoryUtxoView.from_entries(
            [
                (
                    outpoint,
                    UtxoEntry(
                        output=TxOutput(value=125, recipient=owner.address),
                        height=1,
                        is_coinbase=False,
                    ),
                )
            ]
        ),
    )

    fee = validate_transaction(built.transaction, context)

    assert fee == 5
    assert built.change_chipbits == 20
    assert built.transaction.outputs[0].recipient == recipient
    assert built.transaction.outputs[1].recipient == owner.address


def test_transaction_signer_rejects_invalid_recipient_address() -> None:
    owner = wallet_key(0)
    outpoint = OutPoint(txid="44" * 32, index=0)
    signer = TransactionSigner(owner)

    try:
        signer.build_signed_transaction(
            spend_candidates=spend_candidates_for_wallet(outpoint, value=125, owner=owner),
            recipient="CHC-not-an-address",
            amount_chipbits=100,
            fee_chipbits=5,
        )
    except ValueError as exc:
        assert "Recipient" in str(exc)
        return

    raise AssertionError("Expected invalid recipient address to be rejected.")
