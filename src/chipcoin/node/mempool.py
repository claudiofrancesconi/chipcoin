"""Node-local mempool admission and eviction policies."""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Iterable

from ..consensus.models import Transaction
from ..consensus.serialization import serialize_transaction
from ..consensus.epoch_settlement import (
    REWARD_ATTESTATION_BUNDLE_KIND,
    REWARD_SETTLE_EPOCH_KIND,
    attestation_identity,
    parse_reward_attestation_bundle_metadata,
)
from ..consensus.nodes import is_register_reward_node_transaction, is_renew_reward_node_transaction, is_special_node_transaction
from ..consensus.utxo import OverlayUtxoView, UtxoView
from ..consensus.validation import ValidationError, is_coinbase_transaction, validate_transaction
from ..crypto.addresses import is_valid_address
from ..pq.policy import (
    DEFAULT_PQ_POLICY_LIMITS,
    PQPolicyError,
    PQPolicyLimits,
    enforce_pq_mempool_precheck,
    is_pq_transaction,
    pq_signature_cost,
    pq_sigop_count,
)
from ..storage.chainstate import ChainStateRepository
from ..storage.mempool import MempoolEntry, MempoolRepository


LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class AcceptedTransaction:
    """Accepted mempool transaction and its computed fee."""

    transaction: Transaction
    fee: int


@dataclass(frozen=True)
class MempoolPolicy:
    """Node-local policy parameters distinct from consensus validity."""

    min_fee_chipbits_normal_tx: int = 1
    max_transaction_size_bytes: int = 100_000
    max_transaction_inputs: int = 128
    max_transaction_outputs: int = 128
    max_mempool_transactions: int = 1_000
    max_special_node_transactions: int = 32
    max_register_reward_node_transactions: int = 8
    max_renew_reward_node_transactions: int = 16
    max_reward_attestation_bundle_transactions: int = 128
    transaction_ttl_seconds: int = 72 * 60 * 60
    pq_limits: PQPolicyLimits = DEFAULT_PQ_POLICY_LIMITS


@dataclass
class PQMempoolMetrics:
    """Cumulative node-local PQ transaction counters."""

    pq_verify_count: int = 0
    pq_verify_failures: int = 0
    pq_verify_duration_seconds_total: float = 0.0
    pq_verify_duration_seconds_max: float = 0.0
    pq_tx_accepted: int = 0
    pq_tx_rejected: int = 0
    pq_malformed: int = 0
    pq_relay: int = 0
    pq_mined: int = 0
    pq_orphan: int = 0

    def as_dict(self) -> dict[str, object]:
        average = 0.0 if self.pq_verify_count == 0 else self.pq_verify_duration_seconds_total / self.pq_verify_count
        return {
            "pq_verify_count": self.pq_verify_count,
            "pq_verify_failures": self.pq_verify_failures,
            "pq_verify_duration_seconds_total": round(self.pq_verify_duration_seconds_total, 6),
            "pq_verify_duration_seconds_avg": round(average, 6),
            "pq_verify_duration_seconds_max": round(self.pq_verify_duration_seconds_max, 6),
            "pq_tx_accepted": self.pq_tx_accepted,
            "pq_tx_rejected": self.pq_tx_rejected,
            "pq_malformed": self.pq_malformed,
            "pq_relay": self.pq_relay,
            "pq_mined": self.pq_mined,
            "pq_orphan": self.pq_orphan,
        }


class MempoolManager:
    """Validate and stage unconfirmed transactions."""

    def __init__(
        self,
        *,
        repository: MempoolRepository,
        chainstate: ChainStateRepository,
        validation_context_factory,
        time_provider,
        known_chain_transaction_lookup=None,
        policy: MempoolPolicy | None = None,
    ) -> None:
        self.repository = repository
        self.chainstate = chainstate
        self.validation_context_factory = validation_context_factory
        self.time_provider = time_provider
        self.known_chain_transaction_lookup = known_chain_transaction_lookup
        self.policy = policy or MempoolPolicy()
        self.pq_metrics = PQMempoolMetrics()

    def accept(self, transaction: Transaction, *, added_at: int | None = None) -> AcceptedTransaction:
        """Validate and add a transaction to the mempool."""

        pq_candidate = is_pq_transaction(transaction)
        if is_coinbase_transaction(transaction):
            raise ValidationError("Coinbase transactions are not valid mempool entries.")
        try:
            self.preflight(transaction)
            txid = transaction.txid()
            if self.repository.get(txid) is not None:
                raise ValidationError("Transaction is already present in the mempool.")
            if self._is_known_on_chain(txid):
                raise ValidationError("Transaction is already confirmed in the active chain.")

            replacement_txids = self._reward_attestation_bundle_replacement_txids(transaction)
            replacement_txid_set = set(replacement_txids)
            effective_entries = [
                entry
                for entry in self.repository.list_all()
                if entry.transaction.txid() not in replacement_txid_set
            ]

            self._enforce_special_node_mempool_policy(transaction, entries=effective_entries)
            self._enforce_reward_attestation_bundle_mempool_policy(transaction, entries=effective_entries)
            self._ensure_no_mempool_double_spend(transaction, entries=effective_entries)

            snapshot = self._snapshot_with_mempool_applied()
            context = self.validation_context_factory(snapshot)
            fee_chipbits = validate_transaction(transaction, context)
            self._enforce_policy(transaction, fee_chipbits)
            accepted_at = self.time_provider() if added_at is None else added_at
            for replaced_txid in replacement_txids:
                self.repository.remove(replaced_txid)
            self.repository.add(transaction, fee=fee_chipbits, added_at=accepted_at)
            self._prune_expired(now=self.time_provider())
            self._evict_if_needed()
            if pq_candidate:
                self.pq_metrics.pq_tx_accepted += 1
            return AcceptedTransaction(transaction=transaction, fee=fee_chipbits)
        except ValidationError:
            if pq_candidate:
                self.pq_metrics.pq_tx_rejected += 1
            raise

    def list_transactions(self) -> list[MempoolEntry]:
        """Return current mempool entries in processing order."""

        return self.repository.list_all()

    def preflight(self, transaction: Transaction) -> None:
        """Reject obviously non-standard transactions before contextual validation."""

        self._enforce_pre_validation_policy(transaction)

    def reconcile(self, *, extra_transactions: Iterable[Transaction] | None = None) -> None:
        """Rebuild mempool contents against the current active chain and policy."""

        preserved_entries = sorted(self.repository.list_all(), key=lambda entry: (entry.added_at, entry.transaction.txid()))
        self.repository.clear()
        pending_entries: list[MempoolEntry] = []
        pending_view = OverlayUtxoView(self.chainstate)
        next_height = self.validation_context_factory(pending_view).height

        def readmit_fast(transaction: Transaction, *, added_at: int) -> None:
            if self._is_expired(added_at, self.time_provider()):
                _log_reconcile_drop(transaction, reason="expired")
                return
            try:
                self._enforce_pre_validation_policy(transaction)
            except ValidationError as exc:
                _log_reconcile_drop(transaction, reason=str(exc))
                return
            txid = transaction.txid()
            if self._is_known_on_chain(txid):
                _log_reconcile_drop(transaction, reason="confirmed_on_chain")
                return
            try:
                self._enforce_special_node_mempool_policy(transaction, entries=pending_entries)
                self._enforce_reward_attestation_bundle_mempool_policy(transaction, entries=pending_entries)
                self._ensure_no_mempool_double_spend(transaction, entries=pending_entries)
                context = self.validation_context_factory(pending_view)
                fee_chipbits = validate_transaction(transaction, context)
                self._enforce_policy(transaction, fee_chipbits)
                pending_view.apply_transaction(transaction, next_height)
                self.repository.add(transaction, fee=fee_chipbits, added_at=added_at)
                pending_entries.append(MempoolEntry(transaction=transaction, fee=fee_chipbits, added_at=added_at))
            except ValidationError as exc:
                _log_reconcile_drop(transaction, reason=str(exc))
                return

        for entry in preserved_entries:
            readmit_fast(entry.transaction, added_at=entry.added_at)
        if extra_transactions is not None:
            now = self.time_provider()
            for transaction in extra_transactions:
                readmit_fast(transaction, added_at=now)
        self._evict_if_needed()

    def remove_many(self, txids: list[str]) -> None:
        """Remove confirmed or evicted transactions."""

        for txid in txids:
            self.repository.remove(txid)

    def clear(self) -> None:
        """Clear all mempool contents."""

        self.repository.clear()

    def _snapshot_with_mempool_applied(self) -> UtxoView:
        """Build an in-memory chainstate snapshot with current mempool transactions applied."""

        view = OverlayUtxoView(self.chainstate)
        next_height = self.validation_context_factory(view).height
        for entry in self.repository.list_all():
            view.apply_transaction(entry.transaction, next_height)
        return view

    def _ensure_no_mempool_double_spend(self, transaction: Transaction, *, entries: list[MempoolEntry] | None = None) -> None:
        """Reject transactions that conflict with inputs already reserved in mempool."""

        reserved_outpoints = {
            tx_input.previous_output
            for entry in (self.repository.list_all() if entries is None else entries)
            for tx_input in entry.transaction.inputs
        }
        for tx_input in transaction.inputs:
            if tx_input.previous_output in reserved_outpoints:
                raise ValidationError("Transaction conflicts with an existing mempool spend.")

    def _enforce_policy(self, transaction: Transaction, fee_chipbits: int) -> None:
        """Apply mempool-standardness checks beyond pure consensus validity."""

        self._enforce_pre_validation_policy(transaction)
        if fee_chipbits < 0:
            raise ValidationError("Transaction fee cannot be negative.")
        if not _is_zero_fee_native_reward_transaction(transaction) and not is_special_node_transaction(transaction) and fee_chipbits < self.policy.min_fee_chipbits_normal_tx:
            raise ValidationError("Transaction fee is below the configured mempool minimum.")

    def _enforce_pre_validation_policy(self, transaction: Transaction) -> None:
        """Apply cheap mempool checks before building UTXO overlays or verifying signatures."""

        serialized_size = len(serialize_transaction(transaction))
        if serialized_size > self.policy.max_transaction_size_bytes:
            raise ValidationError("Transaction exceeds mempool size policy.")
        if len(transaction.inputs) > self.policy.max_transaction_inputs:
            raise ValidationError("Transaction exceeds mempool input-count policy.")
        if len(transaction.outputs) > self.policy.max_transaction_outputs:
            raise ValidationError("Transaction exceeds mempool output-count policy.")

        try:
            enforce_pq_mempool_precheck(transaction, limits=self.policy.pq_limits)
        except PQPolicyError as exc:
            self.pq_metrics.pq_malformed += 1
            LOGGER.info(
                "pq tx rejected stage=preflight reason=%s txid=%s version=%s inputs=%s outputs=%s pq_sigops=%s pq_cost=%s",
                exc,
                _txid_or_unknown(transaction),
                transaction.version,
                len(transaction.inputs),
                len(transaction.outputs),
                pq_sigop_count(transaction),
                pq_signature_cost(transaction, self.policy.pq_limits),
            )
            raise ValidationError(str(exc)) from exc

        for output in transaction.outputs:
            if int(output.value) <= 0:
                raise ValidationError("Transaction outputs must be positive for mempool policy.")
            if not is_valid_address(output.recipient):
                raise ValidationError("Transaction output recipient is not a valid CHC address.")

    def record_pq_verify(self, *, duration_seconds: float, verified: bool) -> None:
        """Record one ML-DSA verification attempt after cheap checks have passed."""

        self.pq_metrics.pq_verify_count += 1
        if not verified:
            self.pq_metrics.pq_verify_failures += 1
        self.pq_metrics.pq_verify_duration_seconds_total += duration_seconds
        self.pq_metrics.pq_verify_duration_seconds_max = max(
            self.pq_metrics.pq_verify_duration_seconds_max,
            duration_seconds,
        )

    def record_pq_relay(self, transaction: Transaction) -> None:
        """Record one peer-relayed PQ transaction accepted by this node."""

        if is_pq_transaction(transaction):
            self.pq_metrics.pq_relay += 1

    def record_pq_mined(self, transactions: Iterable[Transaction]) -> None:
        """Record PQ transactions confirmed in blocks accepted by this node."""

        self.pq_metrics.pq_mined += sum(1 for transaction in transactions if is_pq_transaction(transaction))

    def record_pq_orphan(self, transactions: Iterable[Transaction]) -> None:
        """Record PQ transactions returned from disconnected branches during reorg."""

        self.pq_metrics.pq_orphan += sum(1 for transaction in transactions if is_pq_transaction(transaction))

    def pq_metrics_snapshot(self) -> dict[str, object]:
        """Return current cumulative PQ mempool and verifier counters."""

        return self.pq_metrics.as_dict()

    def _enforce_special_node_mempool_policy(self, transaction: Transaction, *, entries: list[MempoolEntry] | None = None) -> None:
        """Limit zero-IO node-registry control traffic before it can poison templates."""

        if not is_special_node_transaction(transaction):
            return

        candidate_entries = self.repository.list_all() if entries is None else entries
        special_entries = [entry for entry in candidate_entries if is_special_node_transaction(entry.transaction)]
        if len(special_entries) >= self.policy.max_special_node_transactions:
            raise ValidationError("Mempool special-node transaction limit exceeded.")

        if is_register_reward_node_transaction(transaction):
            register_entries = [
                entry
                for entry in special_entries
                if is_register_reward_node_transaction(entry.transaction)
            ]
            if len(register_entries) >= self.policy.max_register_reward_node_transactions:
                raise ValidationError("Mempool register_reward_node transaction limit exceeded.")
            self._ensure_no_duplicate_register_reward_node_action(transaction, register_entries)
            return

        if is_renew_reward_node_transaction(transaction):
            renew_entries = [
                entry
                for entry in special_entries
                if is_renew_reward_node_transaction(entry.transaction)
            ]
            if len(renew_entries) >= self.policy.max_renew_reward_node_transactions:
                raise ValidationError("Mempool renew_reward_node transaction limit exceeded.")
            self._ensure_no_duplicate_renew_reward_node_action(transaction, renew_entries)

    def _ensure_no_duplicate_register_reward_node_action(self, transaction: Transaction, entries: list[MempoolEntry]) -> None:
        """Reject duplicate pending registrations for the same registry identity."""

        metadata = transaction.metadata
        node_id = metadata.get("node_id")
        owner_pubkey_hex = metadata.get("owner_pubkey_hex")
        node_pubkey_hex = metadata.get("node_pubkey_hex")
        for entry in entries:
            existing = entry.transaction.metadata
            if existing.get("node_id") == node_id:
                raise ValidationError("Mempool already contains a register_reward_node transaction for this node_id.")
            if existing.get("owner_pubkey_hex") == owner_pubkey_hex:
                raise ValidationError("Mempool already contains a register_reward_node transaction for this owner_pubkey.")
            if existing.get("node_pubkey_hex") == node_pubkey_hex:
                raise ValidationError("Mempool already contains a register_reward_node transaction for this node_pubkey.")

    def _ensure_no_duplicate_renew_reward_node_action(self, transaction: Transaction, entries: list[MempoolEntry]) -> None:
        """Reject duplicate pending renewals for the same node and epoch."""

        metadata = transaction.metadata
        node_id = metadata.get("node_id")
        renewal_epoch = metadata.get("renewal_epoch")
        for entry in entries:
            existing = entry.transaction.metadata
            if existing.get("node_id") == node_id and existing.get("renewal_epoch") == renewal_epoch:
                raise ValidationError("Mempool already contains a renew_reward_node transaction for this node_id and epoch.")

    def _enforce_reward_attestation_bundle_mempool_policy(
        self,
        transaction: Transaction,
        *,
        entries: list[MempoolEntry] | None = None,
    ) -> None:
        """Limit pending reward attestations before repeated bundle validation burns CPU."""

        if transaction.metadata.get("kind") != REWARD_ATTESTATION_BUNDLE_KIND:
            return

        try:
            bundle = parse_reward_attestation_bundle_metadata(transaction.metadata)
        except (KeyError, TypeError, ValueError) as exc:
            raise ValidationError(str(exc)) from exc

        candidate_entries = self.repository.list_all() if entries is None else entries
        bundle_entries = [
            entry
            for entry in candidate_entries
            if entry.transaction.metadata.get("kind") == REWARD_ATTESTATION_BUNDLE_KIND
        ]
        if len(bundle_entries) >= self.policy.max_reward_attestation_bundle_transactions:
            raise ValidationError("Mempool reward_attestation_bundle transaction limit exceeded.")

        bundle_key = (bundle.epoch_index, bundle.bundle_window_index, bundle.bundle_submitter_node_id)
        existing_attestations: set[tuple[int, int, str, str]] = set()
        for entry in bundle_entries:
            try:
                existing = parse_reward_attestation_bundle_metadata(entry.transaction.metadata)
            except (KeyError, TypeError, ValueError):
                continue
            existing_key = (existing.epoch_index, existing.bundle_window_index, existing.bundle_submitter_node_id)
            if existing_key == bundle_key:
                message = (
                    "Mempool already contains a reward_attestation_bundle transaction "
                    "for this epoch, window, and submitter."
                )
                raise ValidationError(message)
            existing_attestations.update(
                attestation_identity(attestation)
                for attestation in existing.attestations
            )

        if any(
            attestation_identity(attestation) in existing_attestations
            for attestation in bundle.attestations
        ):
            raise ValidationError("Mempool already contains this reward attestation.")

    def _reward_attestation_bundle_replacement_txids(self, transaction: Transaction) -> list[str]:
        """Return pending bundle txids that a strictly better aggregate can replace."""

        if transaction.metadata.get("kind") != REWARD_ATTESTATION_BUNDLE_KIND:
            return []
        try:
            incoming = parse_reward_attestation_bundle_metadata(transaction.metadata)
        except (KeyError, TypeError, ValueError):
            return []
        incoming_identities = {attestation_identity(attestation) for attestation in incoming.attestations}
        if len(incoming_identities) != len(incoming.attestations) or len(incoming_identities) <= 1:
            return []

        replacement_txids: list[str] = []
        replaced_identities: set[tuple[int, int, str, str]] = set()
        for entry in self.repository.list_all():
            if entry.transaction.metadata.get("kind") != REWARD_ATTESTATION_BUNDLE_KIND:
                continue
            try:
                existing = parse_reward_attestation_bundle_metadata(entry.transaction.metadata)
            except (KeyError, TypeError, ValueError):
                continue
            if (
                existing.epoch_index != incoming.epoch_index
                or existing.bundle_window_index != incoming.bundle_window_index
            ):
                continue
            existing_identities = {attestation_identity(attestation) for attestation in existing.attestations}
            if not existing_identities:
                continue
            if existing_identities.issubset(incoming_identities):
                replacement_txids.append(entry.transaction.txid())
                replaced_identities.update(existing_identities)
                continue
            if existing_identities & incoming_identities:
                return []

        if len(replacement_txids) > 1 or len(incoming_identities) > len(replaced_identities):
            return replacement_txids
        return []

    def _prune_expired(self, *, now: int) -> None:
        """Remove mempool entries older than the configured TTL."""

        for entry in self.repository.list_all():
            if self._is_expired(entry.added_at, now):
                self.repository.remove(entry.transaction.txid())

    def _evict_if_needed(self) -> None:
        """Evict lowest-priority entries when the mempool exceeds capacity."""

        entries = self.repository.list_all()
        if len(entries) <= self.policy.max_mempool_transactions:
            return
        eviction_order = sorted(entries, key=lambda entry: (entry.fee, entry.added_at, entry.transaction.txid()))
        for entry in eviction_order[: len(entries) - self.policy.max_mempool_transactions]:
            self.repository.remove(entry.transaction.txid())

    def _readmit(self, transaction: Transaction, *, added_at: int) -> None:
        """Best-effort re-admission used after blocks or reorgs."""

        if self._is_expired(added_at, self.time_provider()):
            return
        try:
            self.accept(transaction, added_at=added_at)
        except ValidationError:
            return

    def _is_expired(self, added_at: int, now: int) -> bool:
        """Return whether a mempool entry has exceeded policy TTL."""

        return now - added_at >= self.policy.transaction_ttl_seconds

    def _is_known_on_chain(self, txid: str) -> bool:
        """Return whether a transaction is already confirmed in the active chain."""

        if self.known_chain_transaction_lookup is None:
            return False
        return self.known_chain_transaction_lookup(txid) is not None


def _is_zero_fee_native_reward_transaction(transaction: Transaction) -> bool:
    """Return whether a transaction is a native zero-IO reward control payload."""

    return transaction.metadata.get("kind") in {REWARD_ATTESTATION_BUNDLE_KIND, REWARD_SETTLE_EPOCH_KIND}


def _txid_or_unknown(transaction: Transaction) -> str:
    try:
        return transaction.txid()
    except Exception:  # noqa: BLE001 - logging must not hide original policy failure
        return "-"


def _log_reconcile_drop(transaction: Transaction, *, reason: str) -> None:
    """Log special-node mempool drops during chain reconciliation."""

    kind = transaction.metadata.get("kind")
    if not kind:
        return
    if not is_special_node_transaction(transaction):
        return
    fields = _special_node_log_fields(transaction)
    LOGGER.info(
        "mempool reconcile dropped special-node transaction txid=%s tx_type=%s reason=%s%s",
        _txid_or_unknown(transaction),
        kind,
        reason,
        fields,
    )


def _special_node_log_fields(transaction: Transaction) -> str:
    """Return compact metadata fields for special-node diagnostics."""

    try:
        if transaction.metadata.get("kind") == REWARD_ATTESTATION_BUNDLE_KIND:
            bundle = parse_reward_attestation_bundle_metadata(transaction.metadata)
            return (
                f" epoch={bundle.epoch_index}"
                f" window={bundle.bundle_window_index}"
                f" submitter={bundle.bundle_submitter_node_id}"
                f" attestations={len(bundle.attestations)}"
            )
    except Exception:  # noqa: BLE001 - diagnostics must not hide the original drop reason
        return ""
    node_id = transaction.metadata.get("node_id")
    if isinstance(node_id, str) and node_id:
        return f" node_id={node_id}"
    return ""
