"""Shared test helpers for deterministic wallet keys and signed transactions."""

from __future__ import annotations

from chipcoin.consensus.models import OutPoint
from chipcoin.consensus.utxo import UtxoEntry
from chipcoin.crypto.keys import parse_private_key_hex
from chipcoin.wallet.models import SpendCandidate, WalletKey
from chipcoin.wallet.signer import TransactionSigner, wallet_key_from_private_key


_TEST_PRIVATE_KEYS = (
    "0000000000000000000000000000000000000000000000000000000000000001",
    "0000000000000000000000000000000000000000000000000000000000000002",
    "0000000000000000000000000000000000000000000000000000000000000003",
)


def wallet_key(index: int = 0) -> WalletKey:
    """Return a deterministic wallet key for test scenarios."""

    return wallet_key_from_private_key(parse_private_key_hex(_TEST_PRIVATE_KEYS[index]))


def put_wallet_utxo(service, outpoint: OutPoint, *, value: int, owner: WalletKey, height: int = 0, is_coinbase: bool = False) -> None:
    """Insert a spendable UTXO owned by a deterministic wallet key."""

    from chipcoin.consensus.models import TxOutput

    service.chainstate.put_utxo(
        outpoint,
        UtxoEntry(
            output=TxOutput(value=value, recipient=owner.address),
            height=height,
            is_coinbase=is_coinbase,
        ),
    )


def signed_payment(
    outpoint: OutPoint,
    *,
    value: int,
    sender: WalletKey | None = None,
    recipient: str | None = None,
    amount: int | None = None,
    fee: int = 0,
):
    """Build a signed one-input payment transaction for test use."""

    from chipcoin.wallet.models import SpendCandidate

    sender_key = wallet_key(0) if sender is None else sender
    recipient_address = wallet_key(1).address if recipient is None else recipient
    signer = TransactionSigner(sender_key)
    built = signer.build_signed_transaction(
        spend_candidates=[
            SpendCandidate(
                txid=outpoint.txid,
                index=outpoint.index,
                amount_chipbits=value,
                recipient=sender_key.address,
            )
        ],
        recipient=recipient_address,
        amount_chipbits=value - fee if amount is None else amount,
        fee_chipbits=fee,
        metadata={"kind": "payment"},
    )
    return built.transaction


def spend_candidates_for_wallet(outpoint: OutPoint, *, value: int, owner: WalletKey | None = None) -> list[SpendCandidate]:
    """Return one spend candidate owned by the chosen test wallet."""

    sender_key = wallet_key(0) if owner is None else owner
    return [
        SpendCandidate(
            txid=outpoint.txid,
            index=outpoint.index,
            amount_chipbits=value,
            recipient=sender_key.address,
        )
    ]
