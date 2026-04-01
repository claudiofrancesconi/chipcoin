"""Wallet-side key handling and transaction signing boundary."""

from __future__ import annotations

from dataclasses import replace

from ..consensus.models import ChipbitAmount, OutPoint, Transaction, TxInput, TxOutput
from ..consensus.nodes import special_node_transaction_signature_digest
from ..consensus.validation import transaction_signature_digest
from ..crypto.addresses import is_valid_address, public_key_to_address
from ..crypto.keys import derive_public_key, generate_private_key, serialize_public_key_hex
from ..crypto.signatures import sign_digest
from .models import BuiltTransaction, SpendCandidate, WalletKey
from .selection import select_inputs


def wallet_key_from_private_key(private_key: bytes, *, compressed: bool = True) -> WalletKey:
    """Build a wallet key record from raw private key material."""

    public_key = derive_public_key(private_key, compressed=compressed)
    return WalletKey(
        private_key=private_key,
        public_key=public_key,
        address=public_key_to_address(public_key),
        compressed=compressed,
    )


def generate_wallet_key(*, compressed: bool = True) -> WalletKey:
    """Generate a new wallet key pair."""

    return wallet_key_from_private_key(generate_private_key(), compressed=compressed)


class TransactionSigner:
    """Sign digests and transactions outside the node core."""

    def __init__(self, wallet_key: WalletKey) -> None:
        self.wallet_key = wallet_key

    def sign(self, digest: bytes) -> bytes:
        """Sign a digest with wallet-controlled key material."""

        return sign_digest(self.wallet_key.private_key, digest)

    def build_signed_transaction(
        self,
        *,
        spend_candidates: list[SpendCandidate],
        recipient: str,
        amount_chipbits: int,
        fee_chipbits: int,
        change_recipient: str | None = None,
        locktime: int = 0,
        metadata: dict[str, str] | None = None,
    ) -> BuiltTransaction:
        """Construct and sign a transaction spending wallet-owned UTXOs."""

        if amount_chipbits <= 0:
            raise ValueError("Amount must be positive.")
        if fee_chipbits < 0:
            raise ValueError("Fee cannot be negative.")
        if not is_valid_address(recipient):
            raise ValueError("Recipient must be a valid CHC address.")
        resolved_change_recipient = self.wallet_key.address if change_recipient is None else change_recipient
        if not is_valid_address(resolved_change_recipient):
            raise ValueError("Change recipient must be a valid CHC address.")

        selection = select_inputs(spend_candidates, amount_chipbits + fee_chipbits)
        inputs = tuple(
            TxInput(previous_output=OutPoint(txid=candidate.txid, index=candidate.index))
            for candidate in selection.selected
        )
        outputs = [TxOutput(value=ChipbitAmount(amount_chipbits), recipient=recipient)]
        if selection.change_chipbits > 0:
            outputs.append(
                TxOutput(
                    value=ChipbitAmount(selection.change_chipbits),
                    recipient=resolved_change_recipient,
                )
            )

        transaction = Transaction(
            version=1,
            inputs=inputs,
            outputs=tuple(outputs),
            locktime=locktime,
            metadata={} if metadata is None else dict(metadata),
        )

        signed_inputs = []
        for input_index, candidate in enumerate(selection.selected):
            if candidate.recipient != self.wallet_key.address:
                raise ValueError("Spend candidate recipient does not belong to this wallet key.")
            digest = transaction_signature_digest(
                transaction,
                input_index,
                previous_output=TxOutput(value=ChipbitAmount(candidate.amount_chipbits), recipient=candidate.recipient),
            )
            signature = self.sign(digest)
            signed_inputs.append(
                replace(
                    transaction.inputs[input_index],
                    signature=signature,
                    public_key=self.wallet_key.public_key,
                )
            )

        signed_transaction = replace(transaction, inputs=tuple(signed_inputs))
        return BuiltTransaction(
            transaction=signed_transaction,
            fee_chipbits=fee_chipbits,
            change_chipbits=selection.change_chipbits,
        )

    def build_register_node_transaction(self, *, node_id: str, payout_address: str) -> Transaction:
        """Construct and sign a special `register_node` transaction."""

        if not node_id:
            raise ValueError("Node id must not be empty.")
        if not is_valid_address(payout_address):
            raise ValueError("Payout address must be a valid CHC address.")
        metadata = {
            "kind": "register_node",
            "node_id": node_id,
            "payout_address": payout_address,
            "owner_pubkey_hex": serialize_public_key_hex(self.wallet_key.public_key),
            "owner_signature_hex": "",
        }
        unsigned = Transaction(version=1, inputs=(), outputs=(), metadata=metadata)
        signed_metadata = dict(metadata)
        signed_metadata["owner_signature_hex"] = self.sign(special_node_transaction_signature_digest(unsigned)).hex()
        return Transaction(version=1, inputs=(), outputs=(), metadata=signed_metadata)

    def build_renew_node_transaction(self, *, node_id: str, renewal_epoch: int) -> Transaction:
        """Construct and sign a special `renew_node` transaction."""

        if not node_id:
            raise ValueError("Node id must not be empty.")
        metadata = {
            "kind": "renew_node",
            "node_id": node_id,
            "renewal_epoch": str(renewal_epoch),
            "owner_pubkey_hex": serialize_public_key_hex(self.wallet_key.public_key),
            "owner_signature_hex": "",
        }
        unsigned = Transaction(version=1, inputs=(), outputs=(), metadata=metadata)
        signed_metadata = dict(metadata)
        signed_metadata["owner_signature_hex"] = self.sign(special_node_transaction_signature_digest(unsigned)).hex()
        return Transaction(version=1, inputs=(), outputs=(), metadata=signed_metadata)
