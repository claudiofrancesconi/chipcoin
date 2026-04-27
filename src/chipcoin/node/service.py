"""Node runtime facade coordinating local consensus, storage, and sync APIs."""

from __future__ import annotations

from decimal import Decimal, ROUND_DOWN
from dataclasses import dataclass, replace
import hashlib
import json
from pathlib import Path
import secrets

from ..config import get_network_config
from ..consensus.models import Block, OutPoint, Transaction, TxOutput
from ..consensus.epoch_settlement import (
    REWARD_ATTESTATION_BUNDLE_KIND,
    REWARD_SETTLE_EPOCH_KIND,
    analyze_reward_settlement,
    build_reward_settlement,
    build_reward_settlement_transaction,
    candidate_check_windows,
    epoch_close_height,
    epoch_seed,
    parse_reward_attestation_bundle_metadata,
    parse_reward_settlement_metadata,
    verifier_committee,
)
from ..consensus.nodes import (
    InMemoryNodeRegistryView,
    active_node_records,
    apply_special_node_transaction,
    is_special_node_transaction,
    current_epoch,
    reward_node_eligible_from_height,
    reward_node_is_active,
    reward_node_warmup_complete_epoch,
    reward_node_warmup_complete_height,
    reward_node_warmup_satisfied,
    select_rewarded_nodes,
)
from ..consensus.params import ConsensusParams
from ..consensus.pow import bits_to_target, calculate_next_work_required, header_work
from ..consensus.serialization import deserialize_block, deserialize_transaction, serialize_transaction
from ..consensus.economics import (
    CHCBITS_PER_CHC,
    is_epoch_reward_height,
    miner_subsidy_chipbits,
    node_reward_pool_chipbits,
    renew_reward_node_fee_chipbits,
    reward_registered_node_count,
    register_reward_node_fee_chipbits,
    REWARD_NODE_FEE_TARGET_COUNT,
    REWARD_NODE_MIN_REGISTER_FEE_CHIPBITS,
    REWARD_NODE_MIN_RENEW_FEE_CHIPBITS,
    subsidy_split_chipbits,
    total_subsidy_through_height,
)
from ..consensus.utxo import InMemoryUtxoView
from ..consensus.validation import ValidationContext, ValidationError, block_weight_units, is_coinbase_transaction, validate_block
from ..storage.blocks import SQLiteBlockRepository
from ..storage.chainstate import SQLiteChainStateRepository
from ..storage.db import initialize_database
from ..storage.headers import ChainTip, SQLiteHeaderRepository
from ..storage.mempool import SQLiteMempoolRepository
from ..storage.native_rewards import (
    SQLiteEpochSettlementRepository,
    SQLiteRewardAttestationRepository,
    StoredEpochSettlement,
    StoredRewardAttestationBundle,
)
from ..storage.node_registry import SQLiteNodeRegistryRepository
from ..storage.peers import SQLitePeerRepository
from ..utils.time import unix_time
from .mempool import AcceptedTransaction, MempoolManager, MempoolPolicy
from .messages import GetBlocksMessage, GetHeadersMessage, HeadersMessage, InvMessage, InventoryVector
from .mining import BlockTemplate, MiningCoordinator, build_coinbase_transaction, transaction_weight_units
from .peers import PeerInfo, PeerManager
from .snapshots import (
    LoadedSnapshot,
    SnapshotAnchor,
    SnapshotHeaderRecord,
    build_snapshot_payload,
    load_snapshot_file,
    read_snapshot_payload,
    write_snapshot_file,
)
from ..wallet.models import SpendCandidate


@dataclass(frozen=True)
class ChainActivationResult:
    """Summary of activating one stored branch as the new active chain."""

    activated_tip: str
    applied_blocks: int
    reorged: bool
    reorg_depth: int
    old_tip: str | None
    new_tip: str
    common_ancestor: str | None
    disconnected_blocks: int
    readded_transaction_count: int


@dataclass(frozen=True)
class MiningTemplateRecord:
    """One node-issued mining template cached for later submission."""

    template_id: str
    created_at: int
    expires_at: int
    previous_block_hash: str
    height: int
    version: int
    bits: int
    target_hex: str
    payout_address: str
    miner_id: str
    coinbase_outputs: tuple[TxOutput, ...]
    non_coinbase_transactions: tuple[Transaction, ...]
    coinbase_value_chipbits: int
    miner_reward_chipbits: int
    node_reward_total_chipbits: int


def _peer_sort_key(peer: dict[str, object]) -> tuple[int, int, str, int]:
    """Order peers by worst score, then more disconnects, then endpoint."""

    score = int(peer["score"]) if isinstance(peer.get("score"), int) else 0
    disconnects = int(peer["disconnect_count"]) if isinstance(peer.get("disconnect_count"), int) else 0
    return (score, -disconnects, str(peer["host"]), int(peer["port"]))


def _disconnect_sort_key(peer: dict[str, object]) -> tuple[int, int, str, int]:
    """Order peers by disconnect count, then by worsening score and endpoint."""

    disconnects = int(peer["disconnect_count"]) if isinstance(peer.get("disconnect_count"), int) else 0
    score = int(peer["score"]) if isinstance(peer.get("score"), int) else 0
    return (disconnects, -score, str(peer["host"]), int(peer["port"]))


def _worst_operator_status(statuses) -> str:
    """Return the most severe operator-check status."""

    rank = {"ok": 0, "warn": 1, "fail": 2}
    worst = "ok"
    for status in statuses:
        value = str(status)
        if rank.get(value, 2) > rank[worst]:
            worst = value if value in rank else "fail"
    return worst


def _operator_status_message(status: str) -> str:
    """Return a concise operator-check summary message."""

    if status == "ok":
        return "Node is ready for public testnet operation."
    if status == "warn":
        return "Node is operational but needs operator attention before public testnet use."
    return "Node is not ready for public testnet operation."


def _stable_digest(payload: object) -> str:
    """Return one deterministic digest for cross-node reward comparisons."""

    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _format_chipbits_as_chc(chipbits: int) -> str:
    """Return one fixed-precision CHC string for operator-facing economics data."""

    whole = chipbits // CHCBITS_PER_CHC
    fractional = chipbits % CHCBITS_PER_CHC
    return f"{whole}.{fractional:08d}"


def _misbehavior_sort_key(peer: dict[str, object]) -> tuple[int, str, int]:
    """Order peers by misbehavior score and endpoint."""

    misbehavior = int(peer["misbehavior_score"]) if isinstance(peer.get("misbehavior_score"), int) else 0
    return (misbehavior, str(peer["host"]), int(peer["port"]))


def _peer_state(peer: PeerInfo, *, now: int) -> str:
    """Return one coarse operational peer state for diagnostics."""

    if peer.ban_until is not None and peer.ban_until > now:
        return "banned"
    if (peer.success_count or 0) > 0 and (peer.failure_count or 0) <= (peer.success_count or 0):
        return "good"
    if (peer.failure_count or 0) > 0 or (peer.backoff_until or 0) > now or (peer.score or 0) < 0:
        return "questionable"
    return peer.source or "discovered"


def _remaining_seconds(target: int | None, *, now: int) -> int:
    """Return non-negative remaining seconds until a timestamp expires."""

    if target is None or target <= now:
        return 0
    return target - now


class NodeService:
    """Local node orchestrator for validation, chain persistence, and sync."""

    def __init__(
        self,
        *,
        network: str,
        params: ConsensusParams,
        headers,
        blocks,
        chainstate,
        node_registry,
        reward_attestations,
        reward_settlements,
        mempool_repository,
        peer_repository=None,
        peerbook: PeerManager | None = None,
        time_provider=unix_time,
        connection=None,
    ) -> None:
        self.network = network
        self.params = params
        self.headers = headers
        self.blocks = blocks
        self.chainstate = chainstate
        self.node_registry = node_registry
        self.reward_attestations = reward_attestations
        self.reward_settlements = reward_settlements
        self.peer_repository = peer_repository
        self.peerbook = peerbook or PeerManager()
        self.time_provider = time_provider
        self.connection = connection
        self.mempool = MempoolManager(
            repository=mempool_repository,
            chainstate=chainstate,
            validation_context_factory=self._validation_context_for_view,
            time_provider=time_provider,
            known_chain_transaction_lookup=self._find_transaction_in_active_chain,
            policy=MempoolPolicy(),
        )
        self.mining = MiningCoordinator(params=params, time_provider=time_provider)
        self._runtime_sync_status: dict[str, object] | None = None
        self._mining_template_ttl_seconds = 15
        self._mining_templates: dict[str, MiningTemplateRecord] = {}
        self._template_cache_tip_hash: str | None = None
        self._node_source_id = secrets.token_hex(8)

    @classmethod
    def open_sqlite(
        cls,
        path: Path,
        *,
        network: str = "mainnet",
        params: ConsensusParams | None = None,
        time_provider=unix_time,
    ) -> "NodeService":
        """Open a local node backed by a SQLite database."""

        resolved_params = get_network_config(network).params if params is None else params
        connection = initialize_database(path)
        return cls(
            network=network,
            params=resolved_params,
            headers=SQLiteHeaderRepository(connection),
            blocks=SQLiteBlockRepository(connection),
            chainstate=SQLiteChainStateRepository(connection),
            node_registry=SQLiteNodeRegistryRepository(connection),
            reward_attestations=SQLiteRewardAttestationRepository(connection),
            reward_settlements=SQLiteEpochSettlementRepository(connection),
            mempool_repository=SQLiteMempoolRepository(connection),
            peer_repository=SQLitePeerRepository(connection),
            time_provider=time_provider,
            connection=connection,
        )

    def start(self) -> None:
        """Local-only startup placeholder."""

        return None

    def export_snapshot_payload(self, *, format_version: int = 2) -> dict[str, object]:
        """Return a deterministic snapshot payload for fast bootstrap."""

        tip = self.chain_tip()
        if tip is None:
            raise ValueError("cannot export a snapshot from an empty chain")
        headers: list[SnapshotHeaderRecord] = []
        blocks: list[Block] = []
        for height in range(tip.height + 1):
            block_hash = self.headers.get_hash_at_height(height)
            if block_hash is None:
                raise ValueError(f"missing main-chain header at height {height}")
            record = self.headers.get_record(block_hash)
            if record is None or record.cumulative_work is None:
                raise ValueError(f"missing main-chain header metadata at height {height}")
            block = self.blocks.get(block_hash)
            if block is None:
                raise ValueError(f"missing main-chain block at height {height}")
            headers.append(
                SnapshotHeaderRecord(
                    header=record.header,
                    height=height,
                    cumulative_work=record.cumulative_work,
                )
            )
            blocks.append(block)
        return build_snapshot_payload(
            network=self.network,
            params=self.params,
            created_at=self.time_provider(),
            headers=tuple(headers),
            blocks=tuple(blocks),
            utxos=tuple(self.chainstate.list_utxos()),
            node_registry_records=tuple(self.node_registry.list_records()),
            reward_attestation_bundles=tuple(self.reward_attestations.list_bundles()),
            epoch_settlements=tuple(self.reward_settlements.list_settlements()),
            format_version=format_version,
        )

    def export_snapshot_file(self, path: Path, *, format_version: int = 2) -> dict[str, object]:
        """Write a fast-sync snapshot to disk and return its metadata."""

        payload = self.export_snapshot_payload(format_version=format_version)
        write_snapshot_file(path, payload)
        return dict(read_snapshot_payload(path)["metadata"])

    def import_snapshot_file(
        self,
        path: Path,
        *,
        reset_existing: bool = False,
        trust_mode: str = "off",
        trusted_keys: tuple[bytes, ...] = (),
    ) -> dict[str, object]:
        """Load a snapshot from disk and replace local chainstate with its anchor state."""

        loaded = load_snapshot_file(
            path,
            network=self.network,
            params=self.params,
            trust_mode=trust_mode,
            trusted_keys=trusted_keys,
        )
        self.import_snapshot(loaded, reset_existing=reset_existing)
        self._set_chain_meta("snapshot_trust_mode", trust_mode)
        metadata = dict(loaded.metadata)
        metadata["valid_signature_count"] = loaded.valid_signature_count
        metadata["trusted_signature_count"] = loaded.trusted_signature_count
        metadata["accepted_signer_pubkeys"] = list(loaded.accepted_signer_pubkeys)
        metadata["warnings"] = list(loaded.warnings)
        return metadata

    def import_snapshot(self, snapshot: LoadedSnapshot, *, reset_existing: bool = False) -> None:
        """Replace local persistent chainstate with one verified snapshot."""

        if self.connection is None:
            raise ValueError("snapshot import requires a writable SQLite-backed node service")
        existing_tip = self.chain_tip()
        if existing_tip is not None and not reset_existing:
            raise ValueError("snapshot import requires an empty chain or reset_existing=True")
        with self.connection:
            self.connection.execute("DELETE FROM mempool_transactions")
            self.connection.execute("DELETE FROM blocks")
            self.connection.execute("DELETE FROM headers")
            self.connection.execute("DELETE FROM utxos")
            self.connection.execute("DELETE FROM node_registry")
            self.connection.execute("DELETE FROM reward_attestation_entries")
            self.connection.execute("DELETE FROM reward_attestation_bundles")
            self.connection.execute("DELETE FROM epoch_settlement_entries")
            self.connection.execute("DELETE FROM epoch_settlements")
            self.connection.execute(
                "DELETE FROM chain_meta WHERE key LIKE 'snapshot_%' OR key = 'chain_tip_hash'"
            )
        for record in snapshot.headers:
            self.headers.put(
                record.header,
                height=record.height,
                cumulative_work=record.cumulative_work,
                is_main_chain=True,
            )
        for block in snapshot.blocks:
            self.blocks.put(block)
        self.headers.set_tip(snapshot.anchor.block_hash, snapshot.anchor.height)
        self.chainstate.replace_all(list(snapshot.utxos))
        self.node_registry.replace_all(list(snapshot.node_registry_records))
        self.reward_attestations.replace_all(list(snapshot.reward_attestation_bundles))
        self.reward_settlements.replace_all(list(snapshot.epoch_settlements))
        self._set_chain_meta("snapshot_height", str(snapshot.anchor.height))
        self._set_chain_meta("snapshot_block_hash", snapshot.anchor.block_hash)
        self._set_chain_meta("snapshot_checksum_sha256", str(snapshot.metadata["checksum_sha256"]))
        self._set_chain_meta("snapshot_format_version", str(snapshot.metadata["format_version"]))
        self._set_chain_meta("snapshot_signature_verified", "true" if snapshot.valid_signature_count > 0 else "false")
        self._set_chain_meta("snapshot_valid_signature_count", str(snapshot.valid_signature_count))
        self._set_chain_meta("snapshot_trusted_signature_count", str(snapshot.trusted_signature_count))
        self._set_chain_meta("snapshot_accepted_signer_pubkeys", json.dumps(list(snapshot.accepted_signer_pubkeys)))
        self._set_chain_meta("snapshot_trust_warnings", json.dumps(list(snapshot.warnings)))
        self.invalidate_mining_templates()
        self.set_runtime_sync_status(None)

    def snapshot_anchor(self) -> SnapshotAnchor | None:
        """Return the trusted snapshot anchor when this node was bootstrapped from one."""

        height = self._get_chain_meta("snapshot_height")
        block_hash = self._get_chain_meta("snapshot_block_hash")
        if height is None or block_hash is None:
            return None
        return SnapshotAnchor(height=int(height), block_hash=block_hash)

    def receive_transaction(self, transaction: Transaction) -> AcceptedTransaction:
        """Validate and stage a transaction into the local mempool."""

        accepted = self.mempool.accept(transaction)
        self.invalidate_mining_templates()
        return accepted

    def decode_raw_transaction(self, raw_hex: str) -> Transaction:
        """Decode one raw serialized transaction encoded as hex."""

        encoded = bytes.fromhex(raw_hex)
        transaction, offset = deserialize_transaction(encoded)
        if offset != len(encoded):
            raise ValueError("Raw transaction contains trailing bytes.")
        return transaction

    def build_candidate_block(self, miner_address: str) -> BlockTemplate:
        """Construct a local candidate block from current chain state and mempool."""

        tip = self.headers.get_tip()
        height = 0 if tip is None else tip.height + 1
        previous_block_hash = "00" * 32 if tip is None else tip.block_hash
        expected_bits = self._expected_bits_for_height(height)
        mempool_entries = self.mempool.list_transactions()
        preferred_settlement = self._preferred_native_reward_settlement_transaction(height=height, mempool_entries=mempool_entries)
        if preferred_settlement is not None:
            mempool_entries = [
                entry
                for entry in mempool_entries
                if entry.transaction.txid() != preferred_settlement.txid()
            ]
        return self.mining.build_block_template(
            previous_block_hash=previous_block_hash,
            height=height,
            miner_address=miner_address,
            bits=expected_bits,
            mempool_entries=mempool_entries,
            node_registry_view=self.node_registry.snapshot(),
            confirmed_transaction_ids=self._known_confirmed_transaction_ids(),
            system_transactions=() if preferred_settlement is None else (preferred_settlement,),
        )

    def mining_status(self) -> dict[str, object]:
        """Return one miner-facing status payload for remote template workers."""

        tip = self.chain_tip()
        best_height = -1 if tip is None else tip.height
        best_tip_hash = "00" * 32 if tip is None else tip.block_hash
        target_bits = self.expected_next_bits()
        sync_status = self.sync_status()
        snapshot_anchor = self.snapshot_anchor()
        return {
            "network": self.network,
            "best_tip_hash": best_tip_hash,
            "best_height": best_height,
            "bootstrap_mode": "full" if snapshot_anchor is None else "snapshot",
            "snapshot_anchor_height": None if snapshot_anchor is None else snapshot_anchor.height,
            "snapshot_anchor_hash": None if snapshot_anchor is None else snapshot_anchor.block_hash,
            "snapshot_trust_mode": self._get_chain_meta("snapshot_trust_mode") or "off",
            "sync_phase": str(sync_status.get("phase", sync_status.get("mode", "idle"))),
            "local_height": sync_status.get("local_height"),
            "remote_height": sync_status.get("remote_height"),
            "current_sync_peers": sync_status.get("current_sync_peers", ()),
            "target_bits": target_bits,
            "target_hex": self._format_target(target_bits),
            "difficulty": self._difficulty_ratio(target_bits),
            "current_time": self.time_provider(),
            "template_ttl_seconds": self._mining_template_ttl_seconds,
            "node_id": self._node_source_id,
        }

    def get_block_template(
        self,
        *,
        payout_address: str,
        miner_id: str,
        template_mode: str = "full_block",
    ) -> dict[str, object]:
        """Build one miner-facing block template without requiring chain sync."""

        if template_mode not in {"full_block", "header_and_coinbase_data"}:
            raise ValueError("template_mode must be full_block or header_and_coinbase_data")
        self._expire_stale_templates()
        candidate = self.build_candidate_block(payout_address)
        now = self.time_provider()
        template_id = secrets.token_hex(16)
        coinbase = candidate.block.transactions[0]
        non_coinbase_transactions = candidate.block.transactions[1:]
        record = MiningTemplateRecord(
            template_id=template_id,
            created_at=now,
            expires_at=now + self._mining_template_ttl_seconds,
            previous_block_hash=candidate.block.header.previous_block_hash,
            height=candidate.height,
            version=candidate.block.header.version,
            bits=candidate.block.header.bits,
            target_hex=self._format_target(candidate.block.header.bits),
            payout_address=payout_address,
            miner_id=miner_id,
            coinbase_outputs=coinbase.outputs,
            non_coinbase_transactions=non_coinbase_transactions,
            coinbase_value_chipbits=int(coinbase.outputs[0].value) if coinbase.outputs else 0,
            miner_reward_chipbits=int(coinbase.outputs[0].value) if coinbase.outputs else 0,
            node_reward_total_chipbits=sum(int(output.value) for output in coinbase.outputs[1:]),
        )
        self._template_cache_tip_hash = candidate.block.header.previous_block_hash
        self._mining_templates[template_id] = record
        return {
            "template_id": template_id,
            "template_mode": template_mode,
            "network": self.network,
            "previous_block_hash": record.previous_block_hash,
            "height": record.height,
            "version": record.version,
            "bits": record.bits,
            "target": record.target_hex,
            "curtime": now,
            "mintime": now,
            "template_expiry": record.expires_at,
            "coinbase_value_chipbits": record.coinbase_value_chipbits,
            "miner_reward_chipbits": record.miner_reward_chipbits,
            "node_reward_total_chipbits": record.node_reward_total_chipbits,
            "payout_address": payout_address,
            "node_reward_outputs": [
                {"recipient": output.recipient, "amount_chipbits": int(output.value)}
                for output in record.coinbase_outputs[1:]
            ],
            "transactions": [
                {
                    "txid": transaction.txid(),
                    "raw_hex": serialize_transaction(transaction).hex(),
                    "weight_units": transaction_weight_units(transaction),
                }
                for transaction in non_coinbase_transactions
            ],
            "coinbase_tx": {
                "version": coinbase.version,
                "outputs": [
                    {"recipient": output.recipient, "amount_chipbits": int(output.value)}
                    for output in coinbase.outputs
                ],
                "metadata": {"coinbase": "true", "height": str(record.height)},
            },
            "block_weight_limit": self.params.max_block_weight,
            "consensus": {
                "network": self.network,
                "rule_set": f"{self.network}-v1",
                "coinbase_maturity": self.params.coinbase_maturity,
                "max_block_weight": self.params.max_block_weight,
            },
            "node_id": self._node_source_id,
        }

    def submit_mined_block(
        self,
        *,
        template_id: str,
        serialized_block_hex: str,
        miner_id: str,
    ) -> dict[str, object]:
        """Fallback submit path for non-runtime use."""

        prepared = self.prepare_mined_block_submission(
            template_id=template_id,
            serialized_block_hex=serialized_block_hex,
            miner_id=miner_id,
        )
        if prepared["accepted"] is False:
            return prepared
        block = prepared["block"]
        try:
            self.apply_block(block)
        except ValidationError as exc:
            self.discard_mining_template(template_id)
            return {"accepted": False, "reason": f"validation_error:{exc}", "block_hash": None, "became_tip": False}
        finally:
            self._mining_templates.pop(template_id, None)
        new_tip = self.chain_tip()
        return {
            "accepted": True,
            "reason": "accepted",
            "block_hash": block.block_hash(),
            "became_tip": bool(new_tip is not None and new_tip.block_hash == block.block_hash()),
        }

    def prepare_mined_block_submission(
        self,
        *,
        template_id: str,
        serialized_block_hex: str,
        miner_id: str,
    ) -> dict[str, object]:
        """Decode and template-validate one submitted mined block."""

        self._expire_stale_templates()
        record = self._mining_templates.get(template_id)
        if record is None:
            return {"accepted": False, "reason": "unknown_or_expired_template", "block_hash": None, "became_tip": False}
        if record.miner_id != miner_id:
            return {"accepted": False, "reason": "miner_id_mismatch", "block_hash": None, "became_tip": False}
        tip = self.chain_tip()
        current_previous_hash = "00" * 32 if tip is None else tip.block_hash
        if current_previous_hash != record.previous_block_hash:
            return {"accepted": False, "reason": "stale_template", "block_hash": None, "became_tip": False}
        try:
            payload = bytes.fromhex(serialized_block_hex)
            block, offset = deserialize_block(payload)
        except ValueError as exc:
            return {"accepted": False, "reason": f"decode_error:{exc}", "block_hash": None, "became_tip": False}
        if offset != len(payload):
            return {"accepted": False, "reason": "decode_error:trailing_bytes", "block_hash": None, "became_tip": False}
        if block.header.previous_block_hash != record.previous_block_hash:
            return {"accepted": False, "reason": "stale_template", "block_hash": None, "became_tip": False}
        if block.header.bits != record.bits:
            return {"accepted": False, "reason": "bits_mismatch", "block_hash": None, "became_tip": False}
        if not block.transactions:
            return {"accepted": False, "reason": "missing_coinbase", "block_hash": None, "became_tip": False}
        if block.transactions[0].outputs != record.coinbase_outputs:
            return {"accepted": False, "reason": "coinbase_outputs_mismatch", "block_hash": None, "became_tip": False}
        expected_txids = [transaction.txid() for transaction in record.non_coinbase_transactions]
        actual_txids = [transaction.txid() for transaction in block.transactions[1:]]
        if actual_txids != expected_txids:
            return {"accepted": False, "reason": "transaction_set_mismatch", "block_hash": None, "became_tip": False}
        return {"accepted": True, "block": block, "template_id": template_id}

    def discard_mining_template(self, template_id: str) -> None:
        """Remove one cached mining template."""

        self._mining_templates.pop(template_id, None)

    def invalidate_mining_templates(self) -> None:
        """Remove all cached mining templates after any state mutation."""

        self._mining_templates.clear()
        self._template_cache_tip_hash = None

    def expected_next_bits(self) -> int:
        """Return the compact target required for the next candidate block."""

        tip = self.headers.get_tip()
        next_height = 0 if tip is None else tip.height + 1
        return self._expected_bits_for_height(next_height)

    def _expire_stale_templates(self) -> None:
        """Drop cached templates that no longer match the active tip or TTL."""

        now = self.time_provider()
        expired_ids = [
            template_id
            for template_id, record in self._mining_templates.items()
            if record.expires_at <= now
        ]
        for template_id in expired_ids:
            self._mining_templates.pop(template_id, None)

    def apply_block(self, block: Block) -> int:
        """Validate and persist a block into local chain state."""

        tip = self.headers.get_tip()
        height = 0 if tip is None else tip.height + 1
        previous_hash = "00" * 32 if tip is None else tip.block_hash
        previous_cumulative_work = 0
        if tip is not None:
            tip_record = self.headers.get_record(tip.block_hash)
            if tip_record is not None and tip_record.cumulative_work is not None:
                previous_cumulative_work = tip_record.cumulative_work

        snapshot = InMemoryUtxoView.from_entries(self.chainstate.list_utxos())
        context = ValidationContext(
            height=height,
            median_time_past=0 if tip is None else self.headers.get(tip.block_hash).timestamp,
            params=self.params,
            utxo_view=snapshot,
            node_registry_view=self.node_registry.snapshot(),
            reward_attestation_identities=frozenset(self.reward_attestations.attestation_identities()),
            reward_attestation_bundles=tuple(stored.bundle for stored in self.reward_attestations.list_bundles()),
            settled_epoch_indexes=frozenset(self.reward_settlements.settled_epoch_indexes()),
            epoch_seed_by_index=self._epoch_seed_map(height),
            expected_previous_block_hash=previous_hash,
            expected_bits=self._expected_bits_for_height(height),
        )
        total_fees = validate_block(block, context)

        self.headers.put(
            block.header,
            height=height,
            cumulative_work=previous_cumulative_work + header_work(block.header),
            is_main_chain=True,
        )
        self.blocks.put(block)
        self.chainstate.apply_block(block, height)
        self._apply_node_registry_block(block, height)
        self._apply_native_reward_block(block, height)
        self.headers.set_tip(block.block_hash(), height)
        self.mempool.reconcile()
        self.invalidate_mining_templates()
        return total_fees

    def chain_tip(self) -> ChainTip | None:
        """Return the current local chain tip."""

        return self.headers.get_tip()

    def build_block_locator(self, max_count: int = 32) -> tuple[str, ...]:
        """Return block locator hashes for header-first synchronization."""

        return self.headers.list_locator_hashes(max_count=max_count)

    def handle_getheaders(self, request: GetHeadersMessage, *, limit: int = 2000) -> HeadersMessage:
        """Handle a getheaders request against the active main chain."""

        headers = self.headers.get_headers_after(request.locator_hashes, request.stop_hash, limit=limit)
        return HeadersMessage(headers=headers)

    def handle_getblocks(self, request: GetBlocksMessage, *, limit: int = 500) -> InvMessage:
        """Handle a getblocks request by announcing matching block inventory."""

        headers = self.headers.get_headers_after(request.locator_hashes, request.stop_hash, limit=limit)
        return InvMessage(
            items=tuple(
                InventoryVector(object_type="block", object_hash=header.block_hash())
                for header in headers
            )
        )

    def get_block_by_hash(self, block_hash: str) -> Block | None:
        """Return a stored block by hash."""

        return self.blocks.get(block_hash)

    def get_block_by_height(self, height: int) -> Block | None:
        """Return the active-chain block at a given height."""

        block_hash = self.headers.get_hash_at_height(height)
        if block_hash is None:
            return None
        return self.blocks.get(block_hash)

    def activate_chain(self, tip_hash: str) -> ChainActivationResult:
        """Validate and activate a stored branch ending at the supplied tip."""

        previous_tip = self.headers.get_tip()
        path_hashes = self.headers.path_to_root(tip_hash)
        snapshot_anchor = self.snapshot_anchor()
        if snapshot_anchor is not None:
            if snapshot_anchor.height >= len(path_hashes) or path_hashes[snapshot_anchor.height] != snapshot_anchor.block_hash:
                raise ValueError("candidate chain does not match the trusted snapshot anchor")
        old_tip_hash = None if previous_tip is None else previous_tip.block_hash
        old_path = [] if previous_tip is None else self.headers.path_to_root(previous_tip.block_hash)
        common_prefix = 0
        for old_hash, new_hash in zip(old_path, path_hashes):
            if old_hash != new_hash:
                break
            common_prefix += 1
        common_ancestor = path_hashes[common_prefix - 1] if common_prefix > 0 else None
        disconnected_hashes = old_path[common_prefix:]
        reorged = old_tip_hash is not None and old_tip_hash != tip_hash and old_tip_hash not in path_hashes
        if snapshot_anchor is None:
            utxo_view = InMemoryUtxoView()
            node_registry_view = InMemoryNodeRegistryView()
            reward_attestation_bundles: list[StoredRewardAttestationBundle] = []
            reward_settlements: list[StoredEpochSettlement] = []
            reward_attestation_identities: set[tuple[int, int, str, str]] = set()
            settled_epoch_indexes: set[int] = set()
            previous_hash = "00" * 32
            median_time_past = 0
            validated_headers = []
            start_height = 0
        else:
            utxo_view = InMemoryUtxoView.from_entries(self.chainstate.list_utxos())
            node_registry_view = self.node_registry.snapshot()
            reward_attestation_bundles = self.reward_attestations.list_bundles()
            reward_settlements = self.reward_settlements.list_settlements()
            reward_attestation_identities = self.reward_attestations.attestation_identities()
            settled_epoch_indexes = self.reward_settlements.settled_epoch_indexes()
            previous_hash = snapshot_anchor.block_hash
            anchor_header = self.headers.get(snapshot_anchor.block_hash)
            if anchor_header is None:
                raise ValueError("missing trusted snapshot anchor header")
            median_time_past = anchor_header.timestamp
            validated_headers = []
            for height in range(snapshot_anchor.height + 1):
                anchor_hash = path_hashes[height]
                header = self.headers.get(anchor_hash)
                if header is None:
                    raise ValueError(f"missing trusted snapshot header at height {height}")
                validated_headers.append(header)
            start_height = snapshot_anchor.height + 1
        applied_blocks = 0

        for height in range(start_height, len(path_hashes)):
            block_hash = path_hashes[height]
            block = self.blocks.get(block_hash)
            if block is None:
                raise ValueError(f"Cannot activate chain without stored block: {block_hash}")
            context = ValidationContext(
                height=height,
                median_time_past=median_time_past,
                params=self.params,
                utxo_view=utxo_view,
                node_registry_view=node_registry_view,
                reward_attestation_identities=frozenset(reward_attestation_identities),
                reward_attestation_bundles=tuple(stored.bundle for stored in reward_attestation_bundles),
                settled_epoch_indexes=frozenset(settled_epoch_indexes),
                epoch_seed_by_index=self._epoch_seed_map(height, path_hashes=path_hashes),
                expected_previous_block_hash=previous_hash,
                expected_bits=self._expected_bits_for_candidate_height(height, validated_headers),
            )
            validate_block(block, context)
            utxo_view.apply_block(block, height)
            self._apply_node_registry_block(block, height, registry_view=node_registry_view)
            self._collect_native_reward_block(
                block,
                height,
                attestation_bundles=reward_attestation_bundles,
                settled_epochs=reward_settlements,
                attestation_identities=reward_attestation_identities,
                settled_epoch_indexes=settled_epoch_indexes,
            )
            validated_headers.append(block.header)
            previous_hash = block_hash
            median_time_past = block.header.timestamp
            applied_blocks += 1

        self.chainstate.replace_all(utxo_view.list_entries())
        self.node_registry.replace_all(node_registry_view.list_records())
        self.reward_attestations.replace_all(reward_attestation_bundles)
        self.reward_settlements.replace_all(reward_settlements)
        self.headers.set_main_chain(path_hashes)
        disconnected_transactions = self._disconnected_branch_transactions(previous_tip, tip_hash)
        self.mempool.reconcile(extra_transactions=disconnected_transactions)
        return ChainActivationResult(
            activated_tip=tip_hash,
            applied_blocks=applied_blocks,
            reorged=reorged,
            reorg_depth=len(disconnected_hashes),
            old_tip=old_tip_hash,
            new_tip=tip_hash,
            common_ancestor=common_ancestor,
            disconnected_blocks=len(disconnected_hashes),
            readded_transaction_count=len(disconnected_transactions),
        )

    def list_mempool_transactions(self) -> list[Transaction]:
        """Return staged mempool transactions."""

        return [entry.transaction for entry in self.mempool.list_transactions()]

    def submit_raw_transaction(self, raw_hex: str) -> AcceptedTransaction:
        """Decode and submit a raw serialized transaction encoded as hex."""

        return self.receive_transaction(self.decode_raw_transaction(raw_hex))

    def find_transaction(self, txid: str) -> dict | None:
        """Find a transaction in the mempool or active chain."""

        for entry in self.mempool.list_transactions():
            if entry.transaction.txid() == txid:
                return {
                    "location": "mempool",
                    "transaction": entry.transaction,
                    "block_hash": None,
                    "height": None,
                }

        tip = self.chain_tip()
        if tip is None:
            return None
        for height in range(tip.height + 1):
            block = self.get_block_by_height(height)
            if block is None:
                continue
            for transaction in block.transactions:
                if transaction.txid() == txid:
                    return {
                        "location": "chain",
                        "transaction": transaction,
                        "block_hash": block.block_hash(),
                        "height": height,
                    }
        return None

    def get_transaction(self, txid: str) -> Transaction | None:
        """Return a transaction object by identifier when known."""

        result = self.find_transaction(txid)
        return None if result is None else result["transaction"]

    def add_peer(self, host: str, port: int, *, source: str = "discovered") -> PeerInfo:
        """Add a peer to the in-memory local peerbook."""

        existing = next(
            (peer for peer in self.peerbook.list_all(network=self.network) if peer.host == host and peer.port == port),
            None,
        )
        now = self.time_provider()
        peer = PeerInfo(
            host=host,
            port=port,
            network=self.network,
            source=source if existing is None or existing.source is None else existing.source,
            first_seen=now if existing is None or existing.first_seen is None else existing.first_seen,
            last_seen=now,
        )
        self.peerbook.add(peer)
        if self.peer_repository is not None:
            self.peer_repository.add(peer)
        return peer

    def remove_peer(self, host: str, port: int) -> None:
        """Remove a peer from the local peerbook and persistence."""

        peer = PeerInfo(host=host, port=port, network=self.network)
        self.peerbook.remove(peer)
        if self.peer_repository is not None:
            self.peer_repository.remove(host=host, port=port, network=self.network)

    def list_peers(self) -> list[PeerInfo]:
        """Return peers from the local peerbook."""

        persisted = [] if self.peer_repository is None else self.peer_repository.list_known(network=self.network)
        for peer in persisted:
            self.peerbook.add(peer)
        return self.peerbook.list_all(network=self.network)

    def peer_diagnostics(self) -> list[dict[str, object]]:
        """Return deterministic peer diagnostics for CLI/debug output."""

        return [self._peer_diagnostics_payload(peer) for peer in self.list_peers()]

    def peer_detail(self, node_id: str) -> dict[str, object] | None:
        """Return one peer diagnostic record for a node identifier when known."""

        for peer in self.list_peers():
            if peer.node_id == node_id:
                return self._peer_diagnostics_payload(peer)
        return None

    def peer_summary(self) -> dict[str, object]:
        """Return aggregated peer error and connectivity diagnostics."""

        peers = self.peer_diagnostics()
        by_error_class: dict[str, int] = {}
        by_penalty_reason: dict[str, int] = {}
        by_network: dict[str, int] = {}
        by_direction: dict[str, int] = {}
        by_source: dict[str, int] = {}
        by_state: dict[str, int] = {}
        by_handshake_status = {"complete": 0, "incomplete": 0, "unknown": 0}
        backoff_peers = []
        recent_errors = []
        worst_peer = None
        highest_misbehavior_peer = None
        most_disconnected_peer = None
        banned_peer_count = 0

        for peer in peers:
            error_class = peer["protocol_error_class"]
            if isinstance(error_class, str):
                by_error_class[error_class] = by_error_class.get(error_class, 0) + 1
            penalty_reason = peer["last_penalty_reason"]
            if isinstance(penalty_reason, str):
                by_penalty_reason[penalty_reason] = by_penalty_reason.get(penalty_reason, 0) + 1
            network = str(peer["network"])
            by_network[network] = by_network.get(network, 0) + 1
            direction = peer["direction"]
            if isinstance(direction, str):
                by_direction[direction] = by_direction.get(direction, 0) + 1
            source = peer["source"]
            if isinstance(source, str):
                by_source[source] = by_source.get(source, 0) + 1
            peer_state = peer["peer_state"]
            if isinstance(peer_state, str):
                by_state[peer_state] = by_state.get(peer_state, 0) + 1
            handshake_complete = peer["handshake_complete"]
            if handshake_complete is True:
                by_handshake_status["complete"] += 1
            elif handshake_complete is False:
                by_handshake_status["incomplete"] += 1
            else:
                by_handshake_status["unknown"] += 1
            if isinstance(peer["backoff_until"], int) and peer["backoff_until"] > 0:
                backoff_peers.append(peer)
            if peer["last_error_at"] is not None:
                recent_errors.append(peer)
            if worst_peer is None or _peer_sort_key(peer) < _peer_sort_key(worst_peer):
                worst_peer = peer
            if highest_misbehavior_peer is None or _misbehavior_sort_key(peer) > _misbehavior_sort_key(highest_misbehavior_peer):
                highest_misbehavior_peer = peer
            if most_disconnected_peer is None or _disconnect_sort_key(peer) > _disconnect_sort_key(most_disconnected_peer):
                most_disconnected_peer = peer
            if peer["banned"] is True:
                banned_peer_count += 1

        backoff_peers.sort(key=lambda peer: (peer["backoff_until"], peer["host"], peer["port"]))
        recent_errors.sort(
            key=lambda peer: (
                -(peer["last_error_at"] if isinstance(peer["last_error_at"], int) else -1),
                peer["host"],
                peer["port"],
            )
        )
        non_banned_peer_count = len(peers) - banned_peer_count
        good_peer_count = by_state.get("good", 0)
        questionable_peer_count = by_state.get("questionable", 0)
        manual_peer_count = by_source.get("manual", 0)
        seed_peer_count = by_source.get("seed", 0)
        discovered_peer_count = by_source.get("discovered", 0)
        peer_warnings: list[str] = []
        if not peers:
            peer_health = "empty"
            peer_warnings.append("no_known_peers")
        elif non_banned_peer_count == 0:
            peer_health = "all_banned"
            peer_warnings.append("all_known_peers_banned")
        elif backoff_peers:
            peer_health = "degraded"
            peer_warnings.append("backoff_peers_present")
        elif questionable_peer_count > 0 and good_peer_count == 0:
            peer_health = "degraded"
            peer_warnings.append("only_questionable_peers_visible")
        else:
            peer_health = "ok"
        return {
            "error_class_counts": dict(sorted(by_error_class.items())),
            "penalty_reason_counts": dict(sorted(by_penalty_reason.items())),
            "peer_count_by_network": dict(sorted(by_network.items())),
            "peer_count_by_direction": dict(sorted(by_direction.items())),
            "peer_count_by_source": dict(sorted(by_source.items())),
            "peer_count_by_state": dict(sorted(by_state.items())),
            "peer_count_by_handshake_status": by_handshake_status,
            "good_peer_count": good_peer_count,
            "questionable_peer_count": questionable_peer_count,
            "manual_peer_count": manual_peer_count,
            "seed_peer_count": seed_peer_count,
            "discovered_peer_count": discovered_peer_count,
            "non_banned_peer_count": non_banned_peer_count,
            "backoff_peer_count": len(backoff_peers),
            "banned_peer_count": banned_peer_count,
            "backoff_peers": backoff_peers,
            "worst_score_peer": worst_peer,
            "highest_misbehavior_peer": highest_misbehavior_peer,
            "most_disconnected_peer": most_disconnected_peer,
            "most_recent_error_peer": None if not recent_errors else recent_errors[0],
            "peer_count": len(peers),
            "operator_summary": {
                "peer_health": peer_health,
                "non_banned_peer_count": non_banned_peer_count,
                "active_backoff_peer_count": len(backoff_peers),
                "active_ban_count": banned_peer_count,
                "warnings": tuple(peer_warnings),
            },
        }

    def record_peer_observation(
        self,
        *,
        host: str,
        port: int,
        direction: str | None = None,
        source: str | None = None,
        first_seen: int | None = None,
        last_success: int | None = None,
        last_failure: int | None = None,
        failure_count: int | None = None,
        success_count: int | None = None,
        handshake_complete: bool | None = None,
        last_known_height: int | None = None,
        node_id: str | None = None,
        score: int | None = None,
        reconnect_attempts: int | None = None,
        backoff_until: int | None = None,
        last_error: str | None = None,
        last_error_at: int | None = None,
        protocol_error_class: str | None = None,
        disconnect_count: int | None = None,
        session_started_at: int | None = None,
        misbehavior_score: int | None = None,
        misbehavior_last_updated_at: int | None = None,
        ban_until: int | None = None,
        last_penalty_reason: str | None = None,
        last_penalty_at: int | None = None,
    ) -> PeerInfo:
        """Persist the latest peer session metadata for diagnostics."""

        existing = next(
            (peer for peer in self.peerbook.list_all(network=self.network) if peer.host == host and peer.port == port),
            None,
        )
        now = self.time_provider()
        peer = PeerInfo(
            host=host,
            port=port,
            network=self.network,
            source=source if source is not None else (None if existing is None else existing.source),
            first_seen=(
                first_seen if first_seen is not None else (now if existing is None or existing.first_seen is None else existing.first_seen)
            ),
            direction=direction,
            last_seen=now,
            last_success=last_success,
            last_failure=last_failure,
            failure_count=failure_count,
            success_count=success_count,
            handshake_complete=handshake_complete,
            last_known_height=last_known_height,
            node_id=node_id,
            score=score,
            reconnect_attempts=reconnect_attempts,
            backoff_until=backoff_until,
            last_error=last_error,
            last_error_at=last_error_at,
            protocol_error_class=protocol_error_class,
            disconnect_count=disconnect_count,
            session_started_at=session_started_at,
            misbehavior_score=misbehavior_score,
            misbehavior_last_updated_at=misbehavior_last_updated_at,
            ban_until=ban_until,
            last_penalty_reason=last_penalty_reason,
            last_penalty_at=last_penalty_at,
        )
        self.peerbook.add(peer)
        if self.peer_repository is not None:
            self.peer_repository.observe(peer)
        return peer

    def peerbook_clean(self, *, reset_penalties: bool = False, dry_run: bool = False) -> dict[str, object]:
        """Prune transient discovered peers and optionally clear saved penalty state."""

        peers = self.list_peers()
        default_port = get_network_config(self.network).default_p2p_port
        removed: list[dict[str, object]] = []
        reset: list[dict[str, object]] = []

        for peer in peers:
            if peer.source not in {"manual", "seed"} and peer.port != default_port:
                removed.append({"host": peer.host, "port": peer.port, "reason": "noncanonical_discovered_port"})
                if not dry_run:
                    self.remove_peer(peer.host, peer.port)

        if reset_penalties:
            for peer in self.list_peers():
                if (
                    (peer.score or 0) == 0
                    and (peer.reconnect_attempts or 0) == 0
                    and (peer.backoff_until or 0) == 0
                    and (peer.misbehavior_score or 0) == 0
                    and peer.ban_until is None
                    and peer.last_penalty_reason is None
                    and peer.last_penalty_at is None
                ):
                    continue
                reset.append({"host": peer.host, "port": peer.port})
                if dry_run:
                    continue
                cleaned = replace(
                    peer,
                    score=0,
                    reconnect_attempts=0,
                    backoff_until=0,
                    last_error=None,
                    last_error_at=None,
                    protocol_error_class=None,
                    misbehavior_score=0,
                    misbehavior_last_updated_at=self.time_provider(),
                    ban_until=None,
                    last_penalty_reason=None,
                    last_penalty_at=None,
                )
                self.remove_peer(peer.host, peer.port)
                self.peerbook.add(cleaned)
                if self.peer_repository is not None:
                    self.peer_repository.add(cleaned)

        return {
            "peer_count_before": len(peers),
            "removed_count": len(removed),
            "removed": removed,
            "penalties_reset_count": len(reset),
            "penalties_reset": reset,
            "dry_run": dry_run,
        }

    def _peer_diagnostics_payload(self, peer: PeerInfo) -> dict[str, object]:
        """Render one peer record into deterministic diagnostic JSON fields."""

        network_magic_hex = get_network_config(peer.network).magic.hex()
        now = self.time_provider()
        return {
            "host": peer.host,
            "port": peer.port,
            "network": peer.network,
            "network_magic_hex": network_magic_hex,
            "source": peer.source,
            "peer_state": _peer_state(peer, now=now),
            "first_seen": peer.first_seen,
            "direction": peer.direction,
            "node_id": peer.node_id,
            "handshake_complete": peer.handshake_complete,
            "last_success": peer.last_success,
            "last_failure": peer.last_failure,
            "failure_count": peer.failure_count,
            "success_count": peer.success_count,
            "score": peer.score,
            "reconnect_attempts": peer.reconnect_attempts,
            "backoff_until": peer.backoff_until,
            "backoff_remaining_seconds": _remaining_seconds(peer.backoff_until, now=now),
            "last_seen": peer.last_seen,
            "session_started_at": peer.session_started_at,
            "last_known_height": peer.last_known_height,
            "disconnect_count": peer.disconnect_count,
            "last_error": peer.last_error,
            "last_error_at": peer.last_error_at,
            "protocol_error_class": peer.protocol_error_class,
            "misbehavior_score": peer.misbehavior_score,
            "misbehavior_last_updated_at": peer.misbehavior_last_updated_at,
            "ban_until": peer.ban_until,
            "ban_remaining_seconds": _remaining_seconds(peer.ban_until, now=now),
            "banned": peer.ban_until is not None and peer.ban_until > self.time_provider(),
            "last_penalty_reason": peer.last_penalty_reason,
            "last_penalty_at": peer.last_penalty_at,
        }

    def status(self) -> dict[str, object]:
        """Return a richer status snapshot for CLI diagnostics."""

        tip = self.chain_tip()
        header = None if tip is None else self.headers.get(tip.block_hash)
        record = None if tip is None else self.headers.get_record(tip.block_hash)
        next_height = 0 if tip is None else tip.height + 1
        rewarded_nodes = select_rewarded_nodes(
            self.node_registry.snapshot(),
            height=next_height,
            previous_block_hash="00" * 32 if tip is None else tip.block_hash,
            node_reward_pool_chipbits=node_reward_pool_chipbits(next_height, self.params),
            params=self.params,
        )
        supply = self.supply_snapshot()
        peers = self.list_peers()
        reward_node_fees = self.reward_node_fee_schedule()
        handshaken_peer_count = sum(1 for peer in peers if peer.handshake_complete)
        banned_peer_count = sum(1 for peer in peers if peer.ban_until is not None and peer.ban_until > self.time_provider())
        sync_status = self.sync_status()
        snapshot_anchor = self.snapshot_anchor()
        accepted_signer_pubkeys_raw = self._get_chain_meta("snapshot_accepted_signer_pubkeys")
        accepted_signer_pubkeys = [] if accepted_signer_pubkeys_raw is None else json.loads(accepted_signer_pubkeys_raw)
        snapshot_trust_warnings_raw = self._get_chain_meta("snapshot_trust_warnings")
        snapshot_trust_warnings = [] if snapshot_trust_warnings_raw is None else json.loads(snapshot_trust_warnings_raw)
        return {
            "network": self.network,
            "network_magic_hex": get_network_config(self.network).magic.hex(),
            "height": None if tip is None else tip.height,
            "tip_hash": None if tip is None else tip.block_hash,
            "bootstrap_mode": "full" if snapshot_anchor is None else "snapshot",
            "snapshot_anchor_height": None if snapshot_anchor is None else snapshot_anchor.height,
            "snapshot_anchor_hash": None if snapshot_anchor is None else snapshot_anchor.block_hash,
            "snapshot_trust_mode": self._get_chain_meta("snapshot_trust_mode") or "off",
            "snapshot_signature_verified": self._get_chain_meta("snapshot_signature_verified") == "true",
            "accepted_snapshot_signer_pubkeys": accepted_signer_pubkeys,
            "snapshot_trust_warnings": snapshot_trust_warnings,
            "sync_phase": str(sync_status.get("phase", sync_status.get("mode", "idle"))),
            "current_bits": self.params.genesis_bits if header is None else header.bits,
            "current_target": self._format_target(self.params.genesis_bits if header is None else header.bits),
            "current_difficulty_ratio": self._difficulty_ratio(self.params.genesis_bits if header is None else header.bits),
            "expected_next_bits": self.expected_next_bits(),
            "expected_next_target": self._format_target(self.expected_next_bits()),
            "cumulative_work": None if record is None else record.cumulative_work,
            "mempool_size": len(self.mempool.list_transactions()),
            "peer_count": len(peers),
            "handshaken_peer_count": handshaken_peer_count,
            "banned_peer_count": banned_peer_count,
            "sync": sync_status,
            "operator_summary": self._operator_status_summary(
                peer_count=len(peers),
                handshaken_peer_count=handshaken_peer_count,
                banned_peer_count=banned_peer_count,
                sync_status=sync_status,
                snapshot_trust_warnings=tuple(str(item) for item in snapshot_trust_warnings),
            ),
            "next_block_node_reward_recipients": [
                {
                    "node_id": rewarded_node.node_id,
                    "payout_address": rewarded_node.payout_address,
                    "reward_chipbits": rewarded_node.reward_chipbits,
                }
                for rewarded_node in rewarded_nodes
            ],
            "reward_node_fees": reward_node_fees,
            "supply": {
                "network": supply["network"],
                "height": supply["height"],
                "tip_hash": supply["tip_hash"],
                "max_supply_chipbits": supply["max_supply_chipbits"],
                "scheduled_supply_chipbits": supply["scheduled_supply_chipbits"],
                "scheduled_miner_supply_chipbits": supply["scheduled_miner_supply_chipbits"],
                "scheduled_node_reward_supply_chipbits": supply["scheduled_node_reward_supply_chipbits"],
                "scheduled_remaining_supply_chipbits": supply["scheduled_remaining_supply_chipbits"],
                "materialized_supply_chipbits": supply["materialized_supply_chipbits"],
                "materialized_miner_supply_chipbits": supply["materialized_miner_supply_chipbits"],
                "materialized_node_reward_supply_chipbits": supply["materialized_node_reward_supply_chipbits"],
                "undistributed_node_reward_supply_chipbits": supply["undistributed_node_reward_supply_chipbits"],
                "minted_supply_chipbits": supply["minted_supply_chipbits"],
                "miner_minted_supply_chipbits": supply["miner_minted_supply_chipbits"],
                "node_minted_supply_chipbits": supply["node_minted_supply_chipbits"],
                "burned_supply_chipbits": supply["burned_supply_chipbits"],
                "immature_supply_chipbits": supply["immature_supply_chipbits"],
                "circulating_supply_chipbits": supply["circulating_supply_chipbits"],
                "remaining_supply_chipbits": supply["remaining_supply_chipbits"],
            },
        }

    def operator_check(self, *, reward_node_id: str | None = None) -> dict[str, object]:
        """Return a stable operator readiness report for public testnet use."""

        status_payload = self.status()
        peer_summary = self.peer_summary()
        supply = self.supply_diagnostics()
        tip_height = status_payload["height"]
        current_epoch = None if tip_height is None else int(tip_height) // self.params.epoch_length_blocks
        last_closed_epoch = None
        if isinstance(tip_height, int):
            last_closed_epoch = (int(tip_height) + 1) // self.params.epoch_length_blocks - 1
            if last_closed_epoch < 0:
                last_closed_epoch = None

        sections = {
            "chain": self._operator_check_chain_section(status_payload),
            "peers": self._operator_check_peers_section(status_payload, peer_summary),
            "sync": self._operator_check_sync_section(status_payload),
            "supply": self._operator_check_supply_section(supply),
            "rewards": self._operator_check_rewards_section(
                current_epoch=current_epoch,
                last_closed_epoch=last_closed_epoch,
            ),
            "reward_node": self._operator_check_reward_node_section(
                reward_node_id=reward_node_id,
                current_epoch=current_epoch,
            ),
            "mining": self._operator_check_mining_section(),
            "snapshot": self._operator_check_snapshot_section(status_payload),
        }
        overall_status = _worst_operator_status(section["status"] for section in sections.values())
        return {
            "status": overall_status,
            "message": _operator_status_message(overall_status),
            "network": self.network,
            "checked_at": self.time_provider(),
            "sections": sections,
        }

    def _operator_check_chain_section(self, status_payload: dict[str, object]) -> dict[str, object]:
        tip_height = status_payload["height"]
        tip_hash = status_payload["tip_hash"]
        if not isinstance(tip_height, int) or not isinstance(tip_hash, str):
            status = "fail"
            message = "No active chain tip is present."
        else:
            status = "ok"
            message = "Active chain tip is present."
        return {
            "status": status,
            "message": message,
            "fields": {
                "height": tip_height,
                "tip_hash": tip_hash,
                "current_bits": status_payload["current_bits"],
                "current_difficulty_ratio": status_payload["current_difficulty_ratio"],
                "cumulative_work": status_payload["cumulative_work"],
            },
        }

    def _operator_check_peers_section(self, status_payload: dict[str, object], peer_summary: dict[str, object]) -> dict[str, object]:
        peer_count = int(status_payload["peer_count"])
        handshaken_peer_count = int(status_payload["handshaken_peer_count"])
        banned_peer_count = int(status_payload["banned_peer_count"])
        if handshaken_peer_count > 0:
            status = "ok"
            message = "At least one handshaken peer is available."
        elif peer_count > 0:
            status = "warn"
            message = "Known peers exist, but none have completed handshake."
        else:
            status = "warn"
            message = "No known peers are recorded."
        return {
            "status": status,
            "message": message,
            "fields": {
                "peer_count": peer_count,
                "handshaken_peer_count": handshaken_peer_count,
                "banned_peer_count": banned_peer_count,
                "peer_health": peer_summary["operator_summary"]["peer_health"],
                "warnings": list(peer_summary["operator_summary"]["warnings"]),
            },
        }

    def _operator_check_sync_section(self, status_payload: dict[str, object]) -> dict[str, object]:
        sync = status_payload["sync"]
        assert isinstance(sync, dict)
        sync_phase = str(status_payload["sync_phase"])
        validated_height = sync.get("validated_tip_height")
        best_header_height = sync.get("best_header_height")
        missing_block_count = int(sync.get("missing_block_count", 0) or 0)
        queued_block_count = int(sync.get("queued_block_count", 0) or 0)
        inflight_block_count = int(sync.get("inflight_block_count", 0) or 0)
        if status_payload["height"] is None:
            status = "fail"
            message = "Sync cannot be healthy without an active tip."
        elif sync_phase != "synced":
            status = "warn"
            message = "Node sync phase is not synced."
        elif missing_block_count > 0:
            status = "warn"
            message = "Headers are ahead of validated blocks."
        elif queued_block_count > 0 or inflight_block_count > 0:
            status = "warn"
            message = "Block sync work is still pending."
        else:
            status = "ok"
            message = "Sync state is coherent with the validated tip."
        return {
            "status": status,
            "message": message,
            "fields": {
                "sync_phase": sync_phase,
                "mode": sync.get("mode"),
                "validated_tip_height": validated_height,
                "best_header_height": best_header_height,
                "missing_block_count": missing_block_count,
                "queued_block_count": queued_block_count,
                "inflight_block_count": inflight_block_count,
            },
        }

    def _operator_check_supply_section(self, supply: dict[str, object]) -> dict[str, object]:
        max_supply = int(supply["max_supply_chipbits"])
        scheduled_supply = int(supply["scheduled_supply_chipbits"])
        scheduled_miner = int(supply["scheduled_miner_supply_chipbits"])
        scheduled_node = int(supply["scheduled_node_reward_supply_chipbits"])
        materialized_supply = int(supply["materialized_supply_chipbits"])
        materialized_miner = int(supply["materialized_miner_supply_chipbits"])
        materialized_node = int(supply["materialized_node_reward_supply_chipbits"])
        undistributed_node = int(supply["undistributed_node_reward_supply_chipbits"])
        minted_supply = int(supply["minted_supply_chipbits"])
        miner_minted = int(supply["miner_minted_supply_chipbits"])
        node_minted = int(supply["node_minted_supply_chipbits"])
        burned = int(supply["burned_supply_chipbits"])
        immature = int(supply["immature_supply_chipbits"])
        circulating = int(supply["circulating_supply_chipbits"])
        remaining = int(supply["remaining_supply_chipbits"])
        failures = []
        if scheduled_supply != scheduled_miner + scheduled_node:
            failures.append("scheduled_supply_split_mismatch")
        if materialized_supply != materialized_miner + materialized_node:
            failures.append("materialized_supply_split_mismatch")
        if minted_supply != materialized_supply:
            failures.append("minted_supply_not_materialized_supply")
        if miner_minted != materialized_miner or node_minted != materialized_node:
            failures.append("legacy_minted_alias_mismatch")
        if undistributed_node != max(0, scheduled_node - materialized_node):
            failures.append("undistributed_node_reward_mismatch")
        if circulating != minted_supply - burned - immature:
            failures.append("circulating_supply_mismatch")
        if remaining != max(0, max_supply - minted_supply):
            failures.append("remaining_supply_mismatch")
        if failures:
            status = "fail"
            message = "Supply counters are internally inconsistent."
        else:
            status = "ok"
            message = "Supply counters are internally coherent."
        return {
            "status": status,
            "message": message,
            "fields": {
                "height": supply["height"],
                "max_supply_chipbits": max_supply,
                "scheduled_supply_chipbits": scheduled_supply,
                "materialized_supply_chipbits": materialized_supply,
                "circulating_supply_chipbits": circulating,
                "immature_supply_chipbits": immature,
                "undistributed_node_reward_supply_chipbits": undistributed_node,
                "remaining_supply_chipbits": remaining,
                "confirmed_unspent_supply_chipbits": supply["confirmed_unspent_supply_chipbits"],
                "failures": failures,
            },
        }

    def _operator_check_rewards_section(self, *, current_epoch: int | None, last_closed_epoch: int | None) -> dict[str, object]:
        checked_epochs = []
        failures = []
        if last_closed_epoch is not None:
            start_epoch = max(0, last_closed_epoch - 1)
            for epoch_index in range(start_epoch, last_closed_epoch + 1):
                try:
                    summary = self.reward_epoch_summary(epoch_index=epoch_index)
                    checked_epochs.append(
                        {
                            "epoch_index": epoch_index,
                            "settlement_status": summary["settlement_status"],
                            "settlement_exists": summary["settlement_exists"],
                            "rewarded_node_count": summary["rewarded_node_count"],
                            "distributed_node_reward_chipbits": summary["payout_totals"].get("distributed_node_reward_chipbits"),
                            "undistributed_node_reward_chipbits": summary["payout_totals"].get("undistributed_node_reward_chipbits"),
                        }
                    )
                    if summary["settlement_status"] == "closed" and not summary["settlement_exists"]:
                        failures.append(f"missing_settlement_epoch_{epoch_index}")
                except Exception as exc:  # noqa: BLE001
                    failures.append(f"epoch_{epoch_index}_unreadable:{exc}")
        if failures:
            status = "fail"
            message = "Recent reward epoch summaries are not coherent."
        else:
            status = "ok"
            message = "Recent reward epoch summaries are readable."
        return {
            "status": status,
            "message": message,
            "fields": {
                "current_epoch": current_epoch,
                "last_closed_epoch": last_closed_epoch,
                "checked_epochs": checked_epochs,
                "failures": failures,
            },
        }

    def _operator_check_reward_node_section(self, *, reward_node_id: str | None, current_epoch: int | None) -> dict[str, object]:
        registry = self.node_registry_diagnostics()
        reward_registry = [row for row in registry if row["reward_registration"]]
        if reward_node_id is None:
            return {
                "status": "ok",
                "message": "No reward node id was requested.",
                "fields": {
                    "requested_node_id": None,
                    "registered_reward_node_count": len(reward_registry),
                    "active_reward_node_count": sum(1 for row in reward_registry if row["active"]),
                    "current_epoch": current_epoch,
                },
            }
        try:
            node_status = self.reward_node_status(node_id=reward_node_id)
        except ValueError as exc:
            return {
                "status": "fail",
                "message": str(exc),
                "fields": {
                    "requested_node_id": reward_node_id,
                    "registered_reward_node_count": len(reward_registry),
                    "current_epoch": current_epoch,
                },
            }
        eligibility_status = str(node_status["eligibility_status"])
        if node_status["active"] is True and eligibility_status == "active":
            status = "ok"
            message = "Requested reward node is active and eligible."
        elif eligibility_status in {"warming_up", "pending_activation"}:
            status = "warn"
            message = "Requested reward node is registered but not yet eligible."
        else:
            status = "fail"
            message = "Requested reward node is not eligible for rewards."
        return {
            "status": status,
            "message": message,
            "fields": {
                "requested_node_id": reward_node_id,
                "active": node_status["active"],
                "eligibility_status": eligibility_status,
                "eligibility_reason": node_status["eligibility_reason"],
                "last_renewal_epoch": node_status["last_renewal_epoch"],
                "last_renewal_height": node_status["last_renewal_height"],
                "selected_epoch_active": node_status["selected_epoch_active"],
                "selected_epoch_assigned": node_status["selected_epoch_assigned"],
                "selected_epoch_exclusion_reason": node_status["selected_epoch_exclusion_reason"],
            },
        }

    def _operator_check_mining_section(self) -> dict[str, object]:
        try:
            template = self.get_block_template(
                payout_address="operator-check",
                miner_id="operator-check",
                template_mode="header_and_coinbase_data",
            )
            self.discard_mining_template(str(template["template_id"]))
        except Exception as exc:  # noqa: BLE001
            return {
                "status": "fail",
                "message": f"Local mining template build failed: {exc}",
                "fields": {
                    "template_available": False,
                    "error": str(exc),
                },
            }
        return {
            "status": "ok",
            "message": "Local mining template is available.",
            "fields": {
                "template_available": True,
                "template_height": template["height"],
                "previous_block_hash": template["previous_block_hash"],
                "bits": template["bits"],
                "transaction_count": len(template["transactions"]),
                "node_reward_total_chipbits": template["node_reward_total_chipbits"],
            },
        }

    def _operator_check_snapshot_section(self, status_payload: dict[str, object]) -> dict[str, object]:
        warnings = list(status_payload["snapshot_trust_warnings"])
        if warnings:
            status = "warn"
            message = "Snapshot bootstrap has trust warnings."
        else:
            status = "ok"
            message = "Snapshot bootstrap state is coherent."
        return {
            "status": status,
            "message": message,
            "fields": {
                "bootstrap_mode": status_payload["bootstrap_mode"],
                "snapshot_anchor_height": status_payload["snapshot_anchor_height"],
                "snapshot_anchor_hash": status_payload["snapshot_anchor_hash"],
                "snapshot_trust_mode": status_payload["snapshot_trust_mode"],
                "snapshot_signature_verified": status_payload["snapshot_signature_verified"],
                "accepted_snapshot_signer_count": len(status_payload["accepted_snapshot_signer_pubkeys"]),
                "warnings": warnings,
            },
        }

    def reward_node_fee_schedule(self) -> dict[str, object]:
        """Return the current adaptive reward-node fee schedule snapshot."""

        registry_snapshot = self.node_registry.snapshot()
        registered_reward_node_count = reward_registered_node_count(registry_snapshot)
        tip = self.chain_tip()
        next_height = 0 if tip is None else tip.height + 1
        active_reward_node_count = len(
            active_node_records(
                registry_snapshot,
                height=next_height,
                params=self.params,
            )
        )
        register_fee = register_reward_node_fee_chipbits(
            registered_reward_node_count=registered_reward_node_count,
            params=self.params,
        )
        renew_fee = renew_reward_node_fee_chipbits(
            registered_reward_node_count=registered_reward_node_count,
            params=self.params,
        )
        return {
            "policy_version": "registry_log_v1",
            "driver": "registered_reward_node_count",
            "registered_reward_node_count": registered_reward_node_count,
            "active_reward_node_count": active_reward_node_count,
            "target_registered_reward_node_count": REWARD_NODE_FEE_TARGET_COUNT,
            "register_fee_chipbits": register_fee,
            "register_fee_chc": _format_chipbits_as_chc(register_fee),
            "renew_fee_chipbits": renew_fee,
            "renew_fee_chc": _format_chipbits_as_chc(renew_fee),
            "bounds": {
                "max_register_fee_chipbits": self.params.register_node_fee_chipbits,
                "max_register_fee_chc": _format_chipbits_as_chc(self.params.register_node_fee_chipbits),
                "min_register_fee_chipbits": REWARD_NODE_MIN_REGISTER_FEE_CHIPBITS,
                "min_register_fee_chc": _format_chipbits_as_chc(REWARD_NODE_MIN_REGISTER_FEE_CHIPBITS),
                "max_renew_fee_chipbits": self.params.renew_node_fee_chipbits,
                "max_renew_fee_chc": _format_chipbits_as_chc(self.params.renew_node_fee_chipbits),
                "min_renew_fee_chipbits": REWARD_NODE_MIN_RENEW_FEE_CHIPBITS,
                "min_renew_fee_chc": _format_chipbits_as_chc(REWARD_NODE_MIN_RENEW_FEE_CHIPBITS),
            },
        }

    def _operator_status_summary(
        self,
        *,
        peer_count: int,
        handshaken_peer_count: int,
        banned_peer_count: int,
        sync_status: dict[str, object],
        snapshot_trust_warnings: tuple[str, ...] = (),
    ) -> dict[str, object]:
        """Return a concise operator-oriented summary for status surfaces."""

        sync_mode = str(sync_status.get("mode", "idle"))
        if peer_count == 0:
            connectivity_state = "no_known_peers"
        elif handshaken_peer_count == 0:
            connectivity_state = "no_active_peers"
        else:
            connectivity_state = "connected"
        warnings: list[str] = []
        validated_tip_height = sync_status.get("validated_tip_height")
        best_header_height = sync_status.get("best_header_height")
        missing_block_count = sync_status.get("missing_block_count")
        stalled_peers = sync_status.get("stalled_peers")
        if connectivity_state == "no_known_peers":
            warnings.append("no_known_peers")
        elif connectivity_state == "no_active_peers":
            warnings.append("no_active_peers")
        if banned_peer_count > 0:
            warnings.append("banned_peers_present")
        if isinstance(best_header_height, int) and isinstance(validated_tip_height, int) and best_header_height > validated_tip_height:
            warnings.append("header_tip_ahead_of_validated_tip")
        if isinstance(missing_block_count, int) and missing_block_count > 0:
            warnings.append("missing_blocks_for_best_header")
        if isinstance(stalled_peers, tuple) and stalled_peers:
            warnings.append("stalled_peers_present")
        warnings.extend(snapshot_trust_warnings)
        return {
            "sync_state": sync_mode,
            "connectivity_state": connectivity_state,
            "peer_attention": bool(warnings),
            "warnings": tuple(warnings),
        }

    def set_runtime_sync_status(self, payload: dict[str, object] | None) -> None:
        """Persist one runtime-owned sync snapshot for diagnostics surfaces."""

        self._runtime_sync_status = None if payload is None else dict(payload)

    def sync_status(self) -> dict[str, object]:
        """Return the latest sync snapshot or a deterministic idle fallback."""

        if self._runtime_sync_status is not None:
            return dict(self._runtime_sync_status)
        tip = self.chain_tip()
        snapshot_anchor = self.snapshot_anchor()
        local_height = None if tip is None else tip.height
        if snapshot_anchor is not None and local_height == snapshot_anchor.height:
            phase = "snapshot_imported"
        else:
            phase = "idle"
        return {
            "mode": "idle",
            "phase": phase,
            "local_height": local_height,
            "remote_height": local_height,
            "validated_tip_height": None if tip is None else tip.height,
            "validated_tip_hash": None if tip is None else tip.block_hash,
            "best_header_height": None if tip is None else tip.height,
            "best_header_hash": None if tip is None else tip.block_hash,
            "missing_block_count": 0,
            "queued_block_count": 0,
            "inflight_block_count": 0,
            "inflight_block_hashes": (),
            "header_peer_count": 0,
            "header_peers": (),
            "block_peer_count": 0,
            "block_peers": (),
            "stalled_peers": (),
            "current_sync_peers": (),
            "download_window": {
                "start_height": None,
                "end_height": None,
                "size": 0,
            },
        }

    def tip_diagnostics(self) -> dict[str, object] | None:
        """Return detailed diagnostics for the current active tip."""

        tip = self.chain_tip()
        if tip is None:
            return None
        return self.chain_window(tip.height, tip.height)[0]

    def inspect_block(self, *, block_hash: str | None = None, height: int | None = None) -> dict[str, object] | None:
        """Return block contents plus derived diagnostics."""

        block = self.get_block_by_hash(block_hash) if block_hash is not None else self.get_block_by_height(int(height))
        if block is None:
            return None
        record = self.headers.get_record(block.block_hash())
        block_height = None if record is None else record.height
        total_fees_chipbits = None
        if block_height is not None:
            try:
                total_fees_chipbits = self._block_total_fees_chipbits(block_height, block)
            except ValueError:
                total_fees_chipbits = None
        miner_payout_chipbits = int(block.transactions[0].outputs[0].value) if block.transactions and block.transactions[0].outputs else 0
        return {
            "block_hash": block.block_hash(),
            "height": block_height,
            "header": {
                "version": block.header.version,
                "previous_block_hash": block.header.previous_block_hash,
                "merkle_root": block.header.merkle_root,
                "timestamp": block.header.timestamp,
                "bits": block.header.bits,
                "difficulty_target": self._format_target(block.header.bits),
                "difficulty_ratio": self._difficulty_ratio(block.header.bits),
                "nonce": block.header.nonce,
            },
            "cumulative_work": None if record is None else record.cumulative_work,
            "weight_units": block_weight_units(block),
            "fees_chipbits": total_fees_chipbits,
            "miner_payout_chipbits": miner_payout_chipbits,
            "node_reward_payouts": [
                {
                    "recipient": tx_output.recipient,
                    "amount_chipbits": int(tx_output.value),
                }
                for tx_output in block.transactions[0].outputs[1:]
            ],
            "transaction_count": len(block.transactions),
            "transactions": [
                {
                    "txid": transaction.txid(),
                    "weight_units": len(self._serialize_transaction(transaction)),
                }
                for transaction in block.transactions
            ],
        }

    def mempool_diagnostics(self) -> list[dict[str, object]]:
        """Return mempool entries with fee-rate and dependency diagnostics."""

        entry_by_txid = {entry.transaction.txid(): entry for entry in self.mempool.list_transactions()}
        diagnostics = []
        entries = list(entry_by_txid.values())
        entries.sort(
            key=lambda item: (
                -(item.fee * 1_000_000_000 // max(1, len(self._serialize_transaction(item.transaction)))),
                -item.fee,
                item.added_at,
                item.transaction.txid(),
            )
        )
        for entry in entries:
            weight_units = len(self._serialize_transaction(entry.transaction))
            depends_on = sorted(
                {
                    tx_input.previous_output.txid
                    for tx_input in entry.transaction.inputs
                    if tx_input.previous_output.txid in entry_by_txid
                }
            )
            diagnostics.append(
                {
                    "txid": entry.transaction.txid(),
                    "fee_chipbits": entry.fee,
                    "weight_units": weight_units,
                    "fee_rate": self._rate_string(entry.fee, weight_units),
                    "added_at": entry.added_at,
                    "depends_on": depends_on,
                }
            )
        return diagnostics

    def difficulty_diagnostics(self) -> dict[str, object]:
        """Return current and next difficulty information."""

        tip = self.chain_tip()
        current_bits = self.params.genesis_bits if tip is None else self.headers.get(tip.block_hash).bits
        next_height = 0 if tip is None else tip.height + 1
        next_bits = self.expected_next_bits()
        return {
            "current_height": None if tip is None else tip.height,
            "current_bits": current_bits,
            "current_target": self._format_target(current_bits),
            "current_difficulty_ratio": self._difficulty_ratio(current_bits),
            "next_block_height": next_height,
            "next_block_bits": next_bits,
            "next_block_target": self._format_target(next_bits),
            "next_block_difficulty_ratio": self._difficulty_ratio(next_bits),
            "next_retarget_height": self._next_retarget_height(next_height),
        }

    def retarget_diagnostics(self) -> dict[str, object]:
        """Return retarget-window diagnostics around the current chain tip."""

        tip = self.chain_tip()
        next_height = 0 if tip is None else tip.height + 1
        next_retarget_height = self._next_retarget_height(next_height)
        last_completed_boundary = None
        if tip is not None and tip.height >= self.params.difficulty_adjustment_window:
            last_completed_boundary = (tip.height // self.params.difficulty_adjustment_window) * self.params.difficulty_adjustment_window

        boundary_before_bits = None
        boundary_after_bits = None
        if last_completed_boundary is not None and last_completed_boundary > 0:
            before_hash = self.headers.get_hash_at_height(last_completed_boundary - 1)
            after_hash = self.headers.get_hash_at_height(last_completed_boundary)
            if before_hash is not None:
                boundary_before_bits = self.headers.get(before_hash).bits
            if after_hash is not None:
                boundary_after_bits = self.headers.get(after_hash).bits

        current_window = self._retarget_window_for_candidate(next_height)
        return {
            "difficulty_adjustment_window": self.params.difficulty_adjustment_window,
            "target_block_time_seconds": self.params.target_block_time_seconds,
            "current_tip_height": None if tip is None else tip.height,
            "current_bits": self.params.genesis_bits if tip is None else self.headers.get(tip.block_hash).bits,
            "expected_next_bits": self.expected_next_bits(),
            "next_retarget_height": next_retarget_height,
            "candidate_height": next_height,
            "current_window": current_window,
            "last_completed_boundary_height": last_completed_boundary,
            "bits_before_last_boundary": boundary_before_bits,
            "bits_after_last_boundary": boundary_after_bits,
        }

    def chain_window(self, start_height: int, end_height: int) -> list[dict[str, object]]:
        """Return a compact chain summary across a height window."""

        if start_height > end_height:
            raise ValueError("start height must be <= end height")
        rows: list[dict[str, object]] = []
        for height in range(start_height, end_height + 1):
            block = self.get_block_by_height(height)
            if block is None:
                continue
            record = self.headers.get_record(block.block_hash())
            rows.append(
                {
                    "height": height,
                    "block_hash": block.block_hash(),
                    "timestamp": block.header.timestamp,
                    "bits": block.header.bits,
                    "difficulty_target": self._format_target(block.header.bits),
                    "difficulty_ratio": self._difficulty_ratio(block.header.bits),
                    "cumulative_work": None if record is None else record.cumulative_work,
                    "weight_units": block_weight_units(block),
                    "transaction_count": len(block.transactions),
                }
            )
        return rows

    def list_spendable_outputs(self, recipient: str) -> list[SpendCandidate]:
        """Return active-chain UTXOs spendable by the supplied recipient."""

        spendable = []
        for outpoint, entry in self.chainstate.list_utxos():
            if entry.output.recipient != recipient:
                continue
            spendable.append(
                SpendCandidate(
                    txid=outpoint.txid,
                    index=outpoint.index,
                    amount_chipbits=int(entry.output.value),
                    recipient=entry.output.recipient,
                )
            )
        spendable.sort(key=lambda candidate: (candidate.amount_chipbits, candidate.txid, candidate.index))
        return spendable

    def utxo_diagnostics(self, recipient: str) -> list[dict[str, object]]:
        """Return active-chain UTXOs for one address with maturity diagnostics."""

        tip = self.chain_tip()
        spend_height = 0 if tip is None else tip.height + 1
        entries = []
        for outpoint, entry in self.chainstate.list_utxos():
            if entry.output.recipient != recipient:
                continue
            mature = True if not entry.is_coinbase else spend_height - int(entry.height) >= self.params.coinbase_maturity
            entries.append(
                {
                    "txid": outpoint.txid,
                    "vout": outpoint.index,
                    "amount_chipbits": int(entry.output.value),
                    "coinbase": bool(entry.is_coinbase),
                    "mature": mature,
                    "status": "unspent",
                    "origin_height": int(entry.height),
                }
            )
        return sorted(entries, key=lambda item: (item["origin_height"], item["txid"], item["vout"]))

    def balance_diagnostics(self, recipient: str) -> dict[str, object]:
        """Return confirmed, immature, and spendable balances for one address."""

        utxos = self.utxo_diagnostics(recipient)
        confirmed_balance_chipbits = sum(int(utxo["amount_chipbits"]) for utxo in utxos)
        immature_balance_chipbits = sum(
            int(utxo["amount_chipbits"]) for utxo in utxos if utxo["coinbase"] and not utxo["mature"]
        )
        spendable_balance_chipbits = sum(int(utxo["amount_chipbits"]) for utxo in utxos if utxo["mature"])
        return {
            "address": recipient,
            "confirmed_balance_chipbits": confirmed_balance_chipbits,
            "immature_balance_chipbits": immature_balance_chipbits,
            "spendable_balance_chipbits": spendable_balance_chipbits,
            "utxo_count": len(utxos),
        }

    def node_registry_diagnostics(self) -> list[dict[str, object]]:
        """Return registry entries with epoch and eligibility diagnostics."""

        tip = self.chain_tip()
        next_height = 0 if tip is None else tip.height + 1
        current_epoch = next_height // self.params.epoch_length_blocks
        rows = []
        for record in self.node_registry.list_records():
            renewal_epoch = record.last_renewed_height // self.params.epoch_length_blocks
            warmup_complete_epoch = reward_node_warmup_complete_epoch(record, self.params)
            warmup_complete_height = reward_node_warmup_complete_height(record, self.params)
            warmup_complete = reward_node_warmup_satisfied(record, height=next_height, params=self.params)
            active = reward_node_is_active(record, height=next_height, params=self.params)
            if active:
                eligibility_status = "active"
            elif renewal_epoch != current_epoch:
                eligibility_status = "stale"
            elif record.last_renewed_height >= next_height:
                eligibility_status = "pending_activation"
            elif not warmup_complete:
                eligibility_status = "warming_up"
            else:
                eligibility_status = "inactive"
            eligibility_reason = self._reward_node_eligibility_reason(
                record,
                evaluation_height=next_height,
                selected_epoch=current_epoch,
            )
            rows.append(
                {
                    "node_id": record.node_id,
                    "payout_address": record.payout_address,
                    "owner_pubkey": record.owner_pubkey.hex(),
                    "node_pubkey": None if record.node_pubkey is None else record.node_pubkey.hex(),
                    "declared_host": record.declared_host,
                    "declared_port": record.declared_port,
                    "reward_registration": record.reward_registration,
                    "registered_at_height": record.registered_height,
                    "last_renewal_height": record.last_renewed_height,
                    "last_renewal_epoch": renewal_epoch,
                    "active": active,
                    "eligible_from_height": reward_node_eligible_from_height(record, self.params),
                    "warmup_complete": warmup_complete,
                    "warmup_complete_epoch": warmup_complete_epoch,
                    "warmup_complete_height": warmup_complete_height,
                    "eligibility_status": eligibility_status,
                    "eligibility_reason": eligibility_reason,
                    "epoch_status": "current" if renewal_epoch == current_epoch else "stale",
                    "current_epoch": current_epoch,
                }
            )
        return rows

    def native_reward_epoch_seed_diagnostics(self, epoch_index: int | None = None) -> dict[str, object]:
        """Return deterministic epoch seed diagnostics for one epoch."""

        tip = self.chain_tip()
        current_height = -1 if tip is None else tip.height
        next_height = current_height + 1
        resolved_epoch = (next_height // self.params.epoch_length_blocks) if epoch_index is None else epoch_index
        seed_map = self._epoch_seed_map(next_height)
        seed = seed_map.get(resolved_epoch)
        previous_close_height = None if resolved_epoch == 0 else epoch_close_height(resolved_epoch - 1, self.params)
        previous_close_hash = "00" * 32 if resolved_epoch == 0 else self.headers.get_hash_at_height(previous_close_height)
        return {
            "epoch_index": resolved_epoch,
            "epoch_start_height": resolved_epoch * self.params.epoch_length_blocks,
            "epoch_end_height": epoch_close_height(resolved_epoch, self.params),
            "evaluation_height": next_height if resolved_epoch == (next_height // self.params.epoch_length_blocks) else min(
                epoch_close_height(resolved_epoch, self.params),
                current_height,
            ),
            "previous_epoch_close_height": previous_close_height,
            "previous_epoch_close_hash": previous_close_hash,
            "epoch_seed_hex": None if seed is None else seed.hex(),
        }

    def native_reward_assignments(self, *, epoch_index: int | None = None, node_id: str | None = None) -> list[dict[str, object]]:
        """Return deterministic candidate-window and verifier assignments."""

        seed_payload = self.native_reward_epoch_seed_diagnostics(epoch_index)
        seed_hex = seed_payload["epoch_seed_hex"]
        if not isinstance(seed_hex, str):
            return []
        resolved_epoch = int(seed_payload["epoch_index"])
        evaluation_height = int(seed_payload["evaluation_height"])
        all_active_records = [record for record in self.list_active_reward_nodes(evaluation_height) if record.reward_registration]
        active_ids = sorted(record.node_id for record in all_active_records)
        active_records = all_active_records
        if node_id is not None:
            active_records = [record for record in active_records if record.node_id == node_id]
        seed = bytes.fromhex(seed_hex)
        rows: list[dict[str, object]] = []
        for record in sorted(active_records, key=lambda item: item.node_id):
            windows = candidate_check_windows(node_id=record.node_id, seed=seed, params=self.params)
            committees = {
                str(window_index): list(
                    verifier_committee(
                        candidate_node_id=record.node_id,
                        active_verifier_node_ids=active_ids,
                        check_window_index=window_index,
                        seed=seed,
                        params=self.params,
                    )
                )
                for window_index in windows
            }
            rows.append(
                {
                    "epoch_index": resolved_epoch,
                    "evaluation_height": evaluation_height,
                    "node_id": record.node_id,
                    "declared_host": record.declared_host,
                    "declared_port": record.declared_port,
                    "candidate_check_windows": list(windows),
                    "verifier_committees": committees,
                }
            )
        return rows

    def native_reward_attestation_diagnostics(self, *, epoch_index: int | None = None) -> list[dict[str, object]]:
        """Return stored native attestation bundles."""

        rows = []
        for stored in self.reward_attestations.list_bundles(epoch_index=epoch_index):
            block_hash = self.headers.get_hash_at_height(stored.block_height)
            rows.append(
                {
                    "txid": stored.txid,
                    "block_height": stored.block_height,
                    "bundle_block_hash": block_hash,
                    "epoch_index": stored.bundle.epoch_index,
                    "bundle_window_index": stored.bundle.bundle_window_index,
                    "bundle_submitter_node_id": stored.bundle.bundle_submitter_node_id,
                    "reward_state_anchor": self._native_reward_state_anchor(epoch_index=stored.bundle.epoch_index),
                    "attestation_count": len(stored.bundle.attestations),
                    "attestations": [
                        {
                            "epoch_index": attestation.epoch_index,
                            "check_window_index": attestation.check_window_index,
                            "candidate_node_id": attestation.candidate_node_id,
                            "verifier_node_id": attestation.verifier_node_id,
                            "result_code": attestation.result_code,
                            "observed_sync_gap": attestation.observed_sync_gap,
                            "endpoint_commitment": attestation.endpoint_commitment,
                            "concentration_key": attestation.concentration_key,
                            "signature_hex": attestation.signature_hex,
                        }
                        for attestation in stored.bundle.attestations
                    ],
                }
            )
        return rows

    def native_reward_settlement_diagnostics(self, *, epoch_index: int | None = None) -> list[dict[str, object]]:
        """Return stored native settlement payloads."""

        rows = []
        for stored in self.reward_settlements.list_settlements(epoch_index=epoch_index):
            settlement = stored.settlement
            block_hash = self.headers.get_hash_at_height(stored.block_height)
            rows.append(
                {
                    "txid": stored.txid,
                    "block_height": stored.block_height,
                    "settlement_block_hash": block_hash,
                    "epoch_index": settlement.epoch_index,
                    "epoch_start_height": settlement.epoch_start_height,
                    "epoch_end_height": settlement.epoch_end_height,
                    "epoch_seed": settlement.epoch_seed_hex,
                    "policy_version": settlement.policy_version,
                    "submission_mode": settlement.submission_mode,
                    "candidate_summary_root": settlement.candidate_summary_root,
                    "verified_nodes_root": settlement.verified_nodes_root,
                    "rewarded_nodes_root": settlement.rewarded_nodes_root,
                    "rewarded_node_count": settlement.rewarded_node_count,
                    "distributed_node_reward_chipbits": settlement.distributed_node_reward_chipbits,
                    "undistributed_node_reward_chipbits": settlement.undistributed_node_reward_chipbits,
                    "reward_state_anchor": self._native_reward_state_anchor(
                        epoch_index=settlement.epoch_index,
                        settlement_block_height=stored.block_height,
                    ),
                    "reward_entries": [
                        {
                            "node_id": entry.node_id,
                            "payout_address": entry.payout_address,
                            "reward_chipbits": entry.reward_chipbits,
                            "selection_rank": entry.selection_rank,
                            "concentration_key": entry.concentration_key,
                            "final_confirmation_passed": entry.final_confirmation_passed,
                        }
                        for entry in settlement.reward_entries
                    ],
                }
            )
        return rows

    def native_reward_settlement_preview(self, *, epoch_index: int | None = None) -> dict[str, object]:
        """Return one deterministic prototype settlement preview for an epoch."""

        settlement = self.build_native_reward_settlement(epoch_index=epoch_index, submission_mode="preview")
        reward_count = settlement.rewarded_node_count
        distributed_reward = settlement.distributed_node_reward_chipbits
        split_summary = {
            "rewarded_node_count": reward_count,
            "scheduled_node_reward_chipbits": distributed_reward + settlement.undistributed_node_reward_chipbits,
            "distributed_node_reward_chipbits": distributed_reward,
            "undistributed_node_reward_chipbits": settlement.undistributed_node_reward_chipbits,
            "base_reward_chipbits": 0 if reward_count == 0 else distributed_reward // reward_count,
            "remainder_chipbits": 0 if reward_count == 0 else distributed_reward % reward_count,
            "ordered_rewarded_node_ids": [entry.node_id for entry in settlement.reward_entries],
            "ordered_payouts": [
                {
                    "selection_rank": entry.selection_rank,
                    "node_id": entry.node_id,
                    "payout_address": entry.payout_address,
                    "reward_chipbits": entry.reward_chipbits,
                }
                for entry in settlement.reward_entries
            ],
        }
        return {
            "epoch_index": settlement.epoch_index,
            "epoch_start_height": settlement.epoch_start_height,
            "epoch_end_height": settlement.epoch_end_height,
            "epoch_seed": settlement.epoch_seed_hex,
            "policy_version": settlement.policy_version,
            "submission_mode": settlement.submission_mode,
            "candidate_summary_root": settlement.candidate_summary_root,
            "verified_nodes_root": settlement.verified_nodes_root,
            "rewarded_nodes_root": settlement.rewarded_nodes_root,
            "rewarded_node_count": settlement.rewarded_node_count,
            "distributed_node_reward_chipbits": settlement.distributed_node_reward_chipbits,
            "undistributed_node_reward_chipbits": settlement.undistributed_node_reward_chipbits,
            "reward_entries": [
                {
                    "node_id": entry.node_id,
                    "payout_address": entry.payout_address,
                    "reward_chipbits": entry.reward_chipbits,
                    "selection_rank": entry.selection_rank,
                    "concentration_key": entry.concentration_key,
                    "final_confirmation_passed": entry.final_confirmation_passed,
                }
                for entry in settlement.reward_entries
            ],
            "reward_split_summary": split_summary,
            "eligible_reason": "deterministic_attestation_quorum",
        }

    def _native_reward_state_anchor(
        self,
        *,
        epoch_index: int,
        settlement_block_height: int | None = None,
    ) -> dict[str, object]:
        """Return active-chain anchor metadata for reward-state diagnostics."""

        tip = self.chain_tip()
        previous_close_height = None if epoch_index == 0 else epoch_close_height(epoch_index - 1, self.params)
        previous_close_hash = "00" * 32 if epoch_index == 0 else self.headers.get_hash_at_height(previous_close_height)
        settlement_block_hash = None
        if settlement_block_height is not None:
            settlement_block_hash = self.headers.get_hash_at_height(settlement_block_height)
        return {
            "tip_height": -1 if tip is None else tip.height,
            "tip_hash": None if tip is None else tip.block_hash,
            "previous_epoch_close_height": previous_close_height,
            "previous_epoch_close_hash": previous_close_hash,
            "settlement_block_height": settlement_block_height,
            "settlement_block_hash": settlement_block_hash,
        }

    def native_reward_settlement_report(self, *, epoch_index: int | None = None) -> dict[str, object]:
        """Return a detailed deterministic report explaining one epoch settlement outcome."""

        seed_payload = self.native_reward_epoch_seed_diagnostics(epoch_index)
        seed_hex = seed_payload["epoch_seed_hex"]
        if not isinstance(seed_hex, str):
            raise ValueError("Epoch seed is unavailable for the requested epoch.")
        resolved_epoch = int(seed_payload["epoch_index"])
        settlement_height = int(seed_payload["epoch_end_height"])
        active_by_id = {
            record.node_id: record
            for record in self.list_active_reward_nodes(settlement_height)
            if record.reward_registration
        }
        bundle_attestations = [
            attestation
            for stored in self.reward_attestations.list_bundles(epoch_index=resolved_epoch)
            for attestation in stored.bundle.attestations
        ]
        scheduled_pool = node_reward_pool_chipbits(settlement_height, self.params)
        analysis = analyze_reward_settlement(
            active_records_by_id=active_by_id,
            seed=bytes.fromhex(seed_hex),
            attestations=bundle_attestations,
            distributed_reward_chipbits=scheduled_pool,
            params=self.params,
        )
        settlement = build_reward_settlement(
            epoch_index=resolved_epoch,
            epoch_seed_hex=seed_hex,
            epoch_start_height=int(seed_payload["epoch_start_height"]),
            epoch_end_height=settlement_height,
            policy_version="native-v1-prototype",
            submission_mode="report",
            active_records_by_id=active_by_id,
            attestations=bundle_attestations,
            distributed_reward_chipbits=scheduled_pool,
            params=self.params,
        )
        stored_settlements = self.reward_settlements.list_settlements(epoch_index=resolved_epoch)
        latest_stored_settlement = None if not stored_settlements else stored_settlements[-1]
        reward_count = settlement.rewarded_node_count
        split_summary = {
            "rewarded_node_count": reward_count,
            "scheduled_node_reward_chipbits": scheduled_pool,
            "distributed_node_reward_chipbits": settlement.distributed_node_reward_chipbits,
            "undistributed_node_reward_chipbits": settlement.undistributed_node_reward_chipbits,
            "base_reward_chipbits": 0
            if reward_count == 0
            else settlement.distributed_node_reward_chipbits // reward_count,
            "remainder_chipbits": 0
            if reward_count == 0
            else settlement.distributed_node_reward_chipbits % reward_count,
            "ordered_rewarded_node_ids": [entry.node_id for entry in settlement.reward_entries],
            "ordered_payouts": [
                {
                    "selection_rank": entry.selection_rank,
                    "node_id": entry.node_id,
                    "payout_address": entry.payout_address,
                    "reward_chipbits": entry.reward_chipbits,
                }
                for entry in settlement.reward_entries
            ],
        }
        return {
            "epoch_index": settlement.epoch_index,
            "epoch_start_height": settlement.epoch_start_height,
            "epoch_end_height": settlement.epoch_end_height,
            "epoch_seed": settlement.epoch_seed_hex,
            "policy_version": settlement.policy_version,
            "scheduled_node_reward_chipbits": scheduled_pool,
            "rewarded_node_count": settlement.rewarded_node_count,
            "distributed_node_reward_chipbits": settlement.distributed_node_reward_chipbits,
            "undistributed_node_reward_chipbits": settlement.undistributed_node_reward_chipbits,
            "reward_entries": [
                {
                    "node_id": entry.node_id,
                    "payout_address": entry.payout_address,
                    "reward_chipbits": entry.reward_chipbits,
                    "selection_rank": entry.selection_rank,
                    "concentration_key": entry.concentration_key,
                    "final_confirmation_passed": entry.final_confirmation_passed,
                }
                for entry in settlement.reward_entries
            ],
            "eligible_ranking": analysis["eligible_ranking"],
            "node_evaluations": analysis["node_evaluations"],
            "concentration_exclusions": analysis["concentration_exclusions"],
            "settlement_accounting_summary": {
                "scheduled_node_reward_chipbits": scheduled_pool,
                "distributed_node_reward_chipbits": settlement.distributed_node_reward_chipbits,
                "undistributed_node_reward_chipbits": settlement.undistributed_node_reward_chipbits,
            },
            "reward_state_anchor": self._native_reward_state_anchor(
                epoch_index=resolved_epoch,
                settlement_block_height=None if latest_stored_settlement is None else latest_stored_settlement.block_height,
            ),
            "reward_split_summary": split_summary,
        }

    def native_reward_epoch_state(self, *, epoch_index: int | None = None, node_id: str | None = None) -> dict[str, object]:
        """Return one compact reward-state snapshot for cross-node determinism checks."""

        tip = self.chain_tip()
        current_height = -1 if tip is None else tip.height
        next_height = current_height + 1
        seed_payload = self.native_reward_epoch_seed_diagnostics(epoch_index)
        resolved_epoch = int(seed_payload["epoch_index"])
        evaluation_height = int(seed_payload["evaluation_height"])
        active_records = [
            record
            for record in self.list_active_reward_nodes(evaluation_height)
            if record.reward_registration and (node_id is None or record.node_id == node_id)
        ]
        active_rows = [
            {
                "node_id": record.node_id,
                "payout_address": record.payout_address,
                "declared_host": record.declared_host,
                "declared_port": record.declared_port,
                "registered_at_height": record.registered_height,
                "last_renewed_height": record.last_renewed_height,
                "eligible_from_height": reward_node_eligible_from_height(record, self.params),
            }
            for record in sorted(active_records, key=lambda item: item.node_id)
        ]
        assignments = self.native_reward_assignments(epoch_index=resolved_epoch, node_id=node_id)
        attestations = self.native_reward_attestation_diagnostics(epoch_index=resolved_epoch)
        settlements = self.native_reward_settlement_diagnostics(epoch_index=resolved_epoch)
        settlement_preview = self.native_reward_settlement_preview(epoch_index=resolved_epoch)
        settlement_report = self.native_reward_settlement_report(epoch_index=resolved_epoch)
        latest_settlement = None if not settlements else settlements[-1]
        comparison_keys = {
            "active_reward_nodes_digest": _stable_digest(active_rows),
            "assignments_digest": _stable_digest(assignments),
            "attestations_digest": _stable_digest(attestations),
            "settlement_preview_digest": _stable_digest(
                {
                    "rewarded_node_count": settlement_preview["rewarded_node_count"],
                    "distributed_node_reward_chipbits": settlement_preview["distributed_node_reward_chipbits"],
                    "undistributed_node_reward_chipbits": settlement_preview["undistributed_node_reward_chipbits"],
                    "reward_entries": settlement_preview["reward_entries"],
                    "rewarded_nodes_root": settlement_preview["rewarded_nodes_root"],
                }
            ),
            "stored_settlements_digest": _stable_digest(settlements),
        }
        return {
            "epoch_index": resolved_epoch,
            "tip_height": current_height,
            "tip_hash": None if tip is None else tip.block_hash,
            "next_block_height": next_height,
            "node_filter": node_id,
            "seed": seed_payload,
            "active_reward_node_count": len(active_rows),
            "active_reward_nodes": active_rows,
            "assignments_count": len(assignments),
            "assignments": assignments,
            "attestation_bundle_count": len(attestations),
            "attestations": attestations,
            "settlement_preview": {
                "epoch_index": settlement_preview["epoch_index"],
                "submission_mode": settlement_preview["submission_mode"],
                "rewarded_node_count": settlement_preview["rewarded_node_count"],
                "distributed_node_reward_chipbits": settlement_preview["distributed_node_reward_chipbits"],
                "undistributed_node_reward_chipbits": settlement_preview["undistributed_node_reward_chipbits"],
                "reward_entries": settlement_preview["reward_entries"],
                "rewarded_nodes_root": settlement_preview["rewarded_nodes_root"],
                "reward_split_summary": settlement_preview["reward_split_summary"],
            },
            "stored_settlement_count": len(settlements),
            "latest_stored_settlement": latest_settlement,
            "reward_state_anchor": self._native_reward_state_anchor(
                epoch_index=resolved_epoch,
                settlement_block_height=None if latest_settlement is None else int(latest_settlement["block_height"]),
            ),
            "rejection_summary": {
                "concentration_exclusions": settlement_report["concentration_exclusions"],
                "node_evaluations": settlement_report["node_evaluations"],
            },
            "comparison_keys": comparison_keys,
            "comparison_notes": [
                "Compare comparison_keys across honest nodes at the same tip height.",
                "If assignments_digest differs, registry state or epoch seed diverged.",
                "If attestations_digest differs, bundle relay or block inclusion diverged.",
                "If settlement_preview_digest differs, settlement inputs or accounting diverged.",
            ],
        }

    def reward_node_status(self, *, node_id: str, epoch_index: int | None = None) -> dict[str, object]:
        """Return one consolidated operator-facing reward status payload for one node."""

        registry_rows = {str(row["node_id"]): row for row in self.node_registry_diagnostics()}
        row = registry_rows.get(node_id)
        if row is None:
            raise ValueError("Node id is not registered.")
        record = self.get_registered_node(node_id)
        if record is None:
            raise ValueError("Node id is not registered.")

        tip = self.chain_tip()
        current_height = -1 if tip is None else tip.height
        next_height = current_height + 1
        selected_epoch = (next_height // self.params.epoch_length_blocks) if epoch_index is None else int(epoch_index)
        seed = self.native_reward_epoch_seed_diagnostics(selected_epoch)
        evaluation_height = int(seed["evaluation_height"])
        assignments = self.native_reward_assignments(epoch_index=selected_epoch, node_id=node_id)
        epoch_state = self.native_reward_epoch_state(epoch_index=selected_epoch, node_id=node_id)
        settlement_report = self.native_reward_settlement_report(epoch_index=selected_epoch)
        settlements = self.native_reward_settlement_diagnostics(epoch_index=selected_epoch)
        node_evaluation = next(
            (entry for entry in settlement_report["node_evaluations"] if entry["node_id"] == node_id),
            None,
        )
        selected_epoch_active = any(entry["node_id"] == node_id for entry in epoch_state["active_reward_nodes"])
        selected_epoch_assigned = any(entry["node_id"] == node_id for entry in epoch_state["assignments"])
        exclusion_reason = self._reward_node_epoch_exclusion_reason(
            node_id=node_id,
            registry_row=row,
            record=record,
            selected_epoch=selected_epoch,
            evaluation_height=evaluation_height,
            node_evaluation=node_evaluation,
            selected_epoch_active=selected_epoch_active,
            selected_epoch_assigned=selected_epoch_assigned,
        )
        latest_settlement = settlements[-1] if settlements else None
        return {
            "node_id": node_id,
            "epoch_index": selected_epoch,
            "tip_height": current_height,
            "tip_hash": None if tip is None else tip.block_hash,
            "current_epoch": int(row["current_epoch"]),
            "payout_address": row["payout_address"],
            "declared_host": row["declared_host"],
            "declared_port": row["declared_port"],
            "active": bool(row["active"]),
            "eligibility_status": row["eligibility_status"],
            "eligibility_reason": row["eligibility_reason"],
            "registered_at_height": int(row["registered_at_height"]),
            "last_renewal_height": int(row["last_renewal_height"]),
            "last_renewal_epoch": int(row["last_renewal_epoch"]),
            "warmup_complete": bool(row["warmup_complete"]),
            "warmup_complete_epoch": int(row["warmup_complete_epoch"]),
            "warmup_complete_height": int(row["warmup_complete_height"]),
            "eligible_from_height": int(row["eligible_from_height"]),
            "selected_epoch_active": selected_epoch_active,
            "selected_epoch_assigned": selected_epoch_assigned,
            "selected_epoch_assignment": None if not assignments else assignments[0],
            "selected_epoch_exclusion_reason": exclusion_reason,
            "selected_epoch_evaluation_height": evaluation_height,
            "selected_epoch_settlement_status": self._reward_epoch_settlement_status(
                epoch_index=selected_epoch,
                settlement_count=len(settlements),
            ),
            "selected_epoch_settlement_reason": self._reward_epoch_settlement_reason(
                epoch_index=selected_epoch,
                settlement_count=len(settlements),
                settlement_report=settlement_report,
            ),
            "selected_epoch_latest_settlement": latest_settlement,
            "reward_state_anchor": epoch_state["reward_state_anchor"],
        }

    def reward_epoch_summary(self, *, epoch_index: int) -> dict[str, object]:
        """Return one concise operator-facing epoch summary payload."""

        epoch_state = self.native_reward_epoch_state(epoch_index=epoch_index)
        settlement_report = self.native_reward_settlement_report(epoch_index=epoch_index)
        settlements = self.native_reward_settlement_diagnostics(epoch_index=epoch_index)
        latest_settlement = settlements[-1] if settlements else None
        return {
            "epoch_index": int(epoch_state["epoch_index"]),
            "tip_height": int(epoch_state["tip_height"]),
            "tip_hash": epoch_state["tip_hash"],
            "reward_state_anchor": epoch_state["reward_state_anchor"],
            "active_reward_node_count": int(epoch_state["active_reward_node_count"]),
            "active_reward_node_ids": [entry["node_id"] for entry in epoch_state["active_reward_nodes"]],
            "active_reward_nodes": epoch_state["active_reward_nodes"],
            "assignments_count": int(epoch_state["assignments_count"]),
            "assignments_by_node": {
                entry["node_id"]: {
                    "candidate_check_windows": entry["candidate_check_windows"],
                    "verifier_committees": entry["verifier_committees"],
                }
                for entry in epoch_state["assignments"]
            },
            "comparison_keys": epoch_state["comparison_keys"],
            "settlement_status": self._reward_epoch_settlement_status(
                epoch_index=int(epoch_state["epoch_index"]),
                settlement_count=len(settlements),
            ),
            "settlement_reason": self._reward_epoch_settlement_reason(
                epoch_index=int(epoch_state["epoch_index"]),
                settlement_count=len(settlements),
                settlement_report=settlement_report,
            ),
            "settlement_exists": bool(settlements),
            "stored_settlement_count": len(settlements),
            "latest_settlement": latest_settlement,
            "rewarded_node_count": (
                int(latest_settlement["rewarded_node_count"])
                if latest_settlement is not None
                else int(settlement_report["rewarded_node_count"])
            ),
            "rewarded_node_ids": (
                [entry["node_id"] for entry in latest_settlement["reward_entries"]]
                if latest_settlement is not None
                else [entry["node_id"] for entry in settlement_report["reward_entries"]]
            ),
            "reward_entries": latest_settlement["reward_entries"] if latest_settlement is not None else settlement_report["reward_entries"],
            "payout_totals": (
                {
                    "distributed_node_reward_chipbits": int(latest_settlement["distributed_node_reward_chipbits"]),
                    "undistributed_node_reward_chipbits": int(latest_settlement["undistributed_node_reward_chipbits"]),
                }
                if latest_settlement is not None
                else settlement_report["settlement_accounting_summary"]
            ),
        }

    def build_native_reward_settlement(
        self,
        *,
        epoch_index: int | None = None,
        submission_mode: str = "auto",
    ):
        """Build one canonical native reward settlement from persisted epoch state."""

        seed_payload = self.native_reward_epoch_seed_diagnostics(epoch_index)
        seed_hex = seed_payload["epoch_seed_hex"]
        if not isinstance(seed_hex, str):
            raise ValueError("Epoch seed is unavailable for the requested epoch.")
        resolved_epoch = int(seed_payload["epoch_index"])
        settlement_height = int(seed_payload["epoch_end_height"])
        active_by_id = {
            record.node_id: record
            for record in self.list_active_reward_nodes(settlement_height)
            if record.reward_registration
        }
        bundle_attestations = [
            attestation
            for stored in self.reward_attestations.list_bundles(epoch_index=resolved_epoch)
            for attestation in stored.bundle.attestations
        ]
        return build_reward_settlement(
            epoch_index=resolved_epoch,
            epoch_seed_hex=seed_hex,
            epoch_start_height=int(seed_payload["epoch_start_height"]),
            epoch_end_height=settlement_height,
            policy_version="native-v1-prototype",
            submission_mode=submission_mode,
            active_records_by_id=active_by_id,
            attestations=bundle_attestations,
            distributed_reward_chipbits=node_reward_pool_chipbits(settlement_height, self.params),
            params=self.params,
        )

    def build_native_reward_settlement_transaction(
        self,
        *,
        epoch_index: int | None = None,
        submission_mode: str = "auto",
    ) -> Transaction:
        """Build one canonical settlement transaction from persisted epoch state."""

        return build_reward_settlement_transaction(
            self.build_native_reward_settlement(epoch_index=epoch_index, submission_mode=submission_mode)
        )

    def _preferred_native_reward_settlement_transaction(self, *, height: int, mempool_entries) -> Transaction | None:
        """Return the manual settlement override or one auto-generated settlement for `height`."""

        if height < self.params.node_reward_activation_height:
            return None
        if not is_epoch_reward_height(height, self.params):
            return None
        epoch_index = height // self.params.epoch_length_blocks
        if epoch_index in self.reward_settlements.settled_epoch_indexes():
            return None
        manual_candidates = []
        for entry in mempool_entries:
            if entry.transaction.metadata.get("kind") != REWARD_SETTLE_EPOCH_KIND:
                continue
            settlement = parse_reward_settlement_metadata(entry.transaction.metadata)
            if settlement.epoch_end_height == height:
                manual_candidates.append(entry.transaction)
        if manual_candidates:
            manual_candidates.sort(key=lambda transaction: transaction.txid())
            return manual_candidates[0]
        return self.build_native_reward_settlement_transaction(epoch_index=epoch_index, submission_mode="auto")

    def next_winners_diagnostics(self) -> dict[str, object]:
        """Return deterministic next-block node reward diagnostics."""

        tip = self.chain_tip()
        next_height = 0 if tip is None else tip.height + 1
        node_reward_pool = node_reward_pool_chipbits(next_height, self.params)
        rewarded_nodes = select_rewarded_nodes(
            self.node_registry.snapshot(),
            height=next_height,
            previous_block_hash="00" * 32 if tip is None else tip.block_hash,
            node_reward_pool_chipbits=node_reward_pool,
            params=self.params,
        )
        active_nodes = active_node_records(self.node_registry.snapshot(), height=next_height, params=self.params)
        return {
            "next_block_height": next_height,
            "next_block_epoch": next_height // self.params.epoch_length_blocks,
            "epoch_closing_block": is_epoch_reward_height(next_height, self.params),
            "active_nodes_count": len(active_nodes),
            "rewarded_recipients": [
                {
                    "node_id": rewarded_node.node_id,
                    "payout_address": rewarded_node.payout_address,
                    "reward_chipbits": rewarded_node.reward_chipbits,
                }
                for rewarded_node in rewarded_nodes
            ],
            "rewarded_recipients_count": len(rewarded_nodes),
            "miner_subsidy_chipbits": miner_subsidy_chipbits(next_height, self.params),
            "node_reward_chipbits": node_reward_pool,
            "selection_basis": "pre_block_registry",
        }

    def reward_history(self, recipient: str, *, limit: int = 50, descending: bool = True) -> list[dict[str, object]]:
        """Return confirmed reward payouts paid to one address from active-chain coinbases."""

        tip = self.chain_tip()
        if tip is None:
            return []

        rows: list[dict[str, object]] = []
        heights = range(tip.height, -1, -1) if descending else range(tip.height + 1)
        for height in heights:
            block = self.get_block_by_height(height)
            if block is None or not block.transactions:
                continue
            coinbase = block.transactions[0]
            coinbase_txid = coinbase.txid()
            try:
                fees_chipbits = self._block_total_fees_chipbits(height, block)
            except ValueError:
                fees_chipbits = 0
            miner_subsidy = miner_subsidy_chipbits(height, self.params)
            mature = height + self.params.coinbase_maturity < (tip.height + 1)

            if coinbase.outputs and coinbase.outputs[0].recipient == recipient:
                rows.append(
                    {
                        "block_height": height,
                        "block_hash": block.block_hash(),
                        "txid": coinbase_txid,
                        "reward_type": "miner_subsidy",
                        "amount_chipbits": miner_subsidy,
                        "mature": mature,
                        "timestamp": block.header.timestamp,
                    }
                )
                if fees_chipbits > 0:
                    rows.append(
                        {
                            "block_height": height,
                            "block_hash": block.block_hash(),
                            "txid": coinbase_txid,
                            "reward_type": "fees",
                            "amount_chipbits": fees_chipbits,
                            "mature": mature,
                            "timestamp": block.header.timestamp,
                        }
                    )

            for output in coinbase.outputs[1:]:
                if output.recipient != recipient:
                    continue
                rows.append(
                    {
                        "block_height": height,
                        "block_hash": block.block_hash(),
                        "txid": coinbase_txid,
                        "reward_type": "node_reward",
                        "amount_chipbits": int(output.value),
                        "mature": mature,
                        "timestamp": block.header.timestamp,
                    }
                )

            if descending and len(rows) >= limit:
                return rows[:limit]

        rows.sort(key=lambda row: (row["block_height"], row["txid"], row["reward_type"]))
        return rows[:limit]

    def _reward_node_eligibility_reason(
        self,
        record,
        *,
        evaluation_height: int,
        selected_epoch: int,
    ) -> str:
        """Explain one node registry eligibility status at one evaluation height."""

        renewal_epoch = current_epoch(record.last_renewed_height, self.params)
        warmup_complete_height = reward_node_warmup_complete_height(record, self.params)
        eligible_from_height = reward_node_eligible_from_height(record, self.params)
        if reward_node_is_active(record, height=evaluation_height, params=self.params):
            return f"active_from_height_{eligible_from_height}"
        if renewal_epoch != selected_epoch:
            return f"missed_renewal_for_epoch_{selected_epoch}"
        if record.last_renewed_height >= evaluation_height:
            return f"pending_activation_until_height_{eligible_from_height}"
        if not reward_node_warmup_satisfied(record, height=evaluation_height, params=self.params):
            return f"warming_up_until_height_{warmup_complete_height}"
        return "inactive_for_epoch"

    def _reward_epoch_settlement_status(self, *, epoch_index: int, settlement_count: int) -> str:
        """Return one short machine-friendly settlement lifecycle status."""

        tip = self.chain_tip()
        current_height = -1 if tip is None else tip.height
        if settlement_count > 0:
            return "closed"
        if current_height < epoch_close_height(epoch_index, self.params):
            return "open"
        return "unsettled"

    def _reward_epoch_settlement_reason(
        self,
        *,
        epoch_index: int,
        settlement_count: int,
        settlement_report: dict[str, object],
    ) -> str:
        """Explain settlement presence or absence for one epoch."""

        status = self._reward_epoch_settlement_status(epoch_index=epoch_index, settlement_count=settlement_count)
        if status == "closed":
            return "settlement_stored"
        if status == "open":
            return "no_settlement_because_epoch_open"
        if int(settlement_report["rewarded_node_count"]) == 0:
            return "no_reward_because_no_valid_attestations"
        return "no_settlement_stored_for_closed_epoch"

    def _reward_node_epoch_exclusion_reason(
        self,
        *,
        node_id: str,
        registry_row: dict[str, object],
        record,
        selected_epoch: int,
        evaluation_height: int,
        node_evaluation: dict[str, object] | None,
        selected_epoch_active: bool,
        selected_epoch_assigned: bool,
    ) -> str | None:
        """Explain why one node is not active or assigned in one epoch."""

        if selected_epoch_assigned:
            return None
        if not selected_epoch_active:
            renewal_epoch = current_epoch(record.last_renewed_height, self.params)
            if renewal_epoch != selected_epoch:
                return f"no_assignment_because_stale_missed_renewal_for_epoch_{selected_epoch}"
            warmup_complete_height = reward_node_warmup_complete_height(record, self.params)
            if not reward_node_warmup_satisfied(record, height=evaluation_height, params=self.params):
                return f"no_assignment_because_warming_up_until_height_{warmup_complete_height}"
            eligible_from_height = reward_node_eligible_from_height(record, self.params)
            if eligible_from_height > evaluation_height:
                return f"no_assignment_because_active_from_height_{eligible_from_height}"
            return "no_assignment_because_inactive"
        if node_evaluation is not None and node_evaluation.get("not_rewarded_reason"):
            return str(node_evaluation["not_rewarded_reason"])
        return "no_assignment_because_not_selected"

    def reward_summary(
        self,
        recipient: str,
        *,
        start_height: int | None = None,
        end_height: int | None = None,
    ) -> dict[str, object]:
        """Return aggregated reward totals for one address across active-chain history."""

        rewards = [
            entry
            for entry in self.reward_history(recipient, limit=10_000_000, descending=False)
            if (start_height is None or int(entry["block_height"]) >= start_height)
            and (end_height is None or int(entry["block_height"]) <= end_height)
        ]
        total_rewards_chipbits = sum(int(entry["amount_chipbits"]) for entry in rewards)
        total_miner_subsidy_chipbits = sum(
            int(entry["amount_chipbits"]) for entry in rewards if entry["reward_type"] == "miner_subsidy"
        )
        total_node_rewards_chipbits = sum(
            int(entry["amount_chipbits"]) for entry in rewards if entry["reward_type"] == "node_reward"
        )
        total_fees_chipbits = sum(int(entry["amount_chipbits"]) for entry in rewards if entry["reward_type"] == "fees")
        mature_rewards_chipbits = sum(int(entry["amount_chipbits"]) for entry in rewards if entry["mature"])
        immature_rewards_chipbits = sum(int(entry["amount_chipbits"]) for entry in rewards if not entry["mature"])
        heights = [int(entry["block_height"]) for entry in rewards]
        return {
            "address": recipient,
            "total_rewards_chipbits": total_rewards_chipbits,
            "total_miner_subsidy_chipbits": total_miner_subsidy_chipbits,
            "total_node_rewards_chipbits": total_node_rewards_chipbits,
            "total_fees_chipbits": total_fees_chipbits,
            "mature_rewards_chipbits": mature_rewards_chipbits,
            "immature_rewards_chipbits": immature_rewards_chipbits,
            "payout_count": len(rewards),
            "first_reward_height": None if not heights else min(heights),
            "last_reward_height": None if not heights else max(heights),
        }

    def next_block_epoch(self) -> int:
        """Return the epoch number that applies to the next candidate block."""

        tip = self.chain_tip()
        next_height = 0 if tip is None else tip.height + 1
        return next_height // self.params.epoch_length_blocks

    def get_registered_node(self, node_id: str):
        """Return one registered node record by node id when present."""

        return self.node_registry.get_by_node_id(node_id)

    def get_registered_node_by_owner(self, owner_pubkey: bytes):
        """Return one registered node record by owner pubkey when present."""

        return self.node_registry.get_by_owner_pubkey(owner_pubkey)

    def node_income_summary(self, *, node_id: str | None = None, address: str | None = None) -> list[dict[str, object]]:
        """Return aggregated node reward income for registry records."""

        registry_rows = self.node_registry_diagnostics()
        if node_id is not None:
            registry_rows = [row for row in registry_rows if row["node_id"] == node_id]
        if address is not None:
            registry_rows = [row for row in registry_rows if row["payout_address"] == address]

        results = []
        for row in registry_rows:
            reward_entries = [
                entry
                for entry in self.reward_history(str(row["payout_address"]), limit=10_000_000, descending=False)
                if entry["reward_type"] == "node_reward"
            ]
            heights = [int(entry["block_height"]) for entry in reward_entries]
            results.append(
                {
                    "node_id": row["node_id"],
                    "payout_address": row["payout_address"],
                    "active": row["active"],
                    "total_node_rewards_chipbits": sum(int(entry["amount_chipbits"]) for entry in reward_entries),
                    "reward_count": len(reward_entries),
                    "last_reward_height": None if not heights else max(heights),
                    "registered_at_height": row["registered_at_height"],
                    "last_renewal_height": row["last_renewal_height"],
                    "current_epoch_status": row["epoch_status"],
                }
            )
        results.sort(key=lambda row: (row["node_id"], row["payout_address"]))
        return results

    def mining_history(self, recipient: str, *, limit: int = 50, descending: bool = True) -> list[dict[str, object]]:
        """Return per-block mining income details for a miner payout address."""

        tip = self.chain_tip()
        if tip is None:
            return []

        rows: list[dict[str, object]] = []
        for height in range(tip.height + 1):
            block = self.get_block_by_height(height)
            if block is None or not block.transactions or not block.transactions[0].outputs:
                continue
            coinbase = block.transactions[0]
            if coinbase.outputs[0].recipient != recipient:
                continue
            try:
                fees_chipbits = self._block_total_fees_chipbits(height, block)
            except ValueError:
                fees_chipbits = 0
            node_pool_chipbits = node_reward_pool_chipbits(height, self.params)
            rewarded_nodes = select_rewarded_nodes(
                self._replay_chain_state_before_height(height)[1],
                height=height,
                previous_block_hash=block.header.previous_block_hash,
                node_reward_pool_chipbits=node_pool_chipbits,
                params=self.params,
            )
            rows.append(
                {
                    "height": height,
                    "block_hash": block.block_hash(),
                    "miner_subsidy_chipbits": miner_subsidy_chipbits(height, self.params),
                    "fees_chipbits": fees_chipbits,
                    "node_reward_chipbits": node_pool_chipbits,
                    "node_reward_recipient_count": len(rewarded_nodes),
                    "timestamp": block.header.timestamp,
                }
            )
        rows.sort(key=lambda row: (row["height"], row["block_hash"]))
        if descending:
            rows.reverse()
        return rows[:limit]

    def economy_summary(self) -> dict[str, object]:
        """Return a macro-level summary of the active-chain economy."""

        tip = self.chain_tip()
        current_height = None if tip is None else tip.height
        next_height = 0 if tip is None else tip.height + 1
        current_bits = self.params.genesis_bits if tip is None else self.headers.get(tip.block_hash).bits
        registered_nodes = self.node_registry.list_records()
        active_nodes = active_node_records(self.node_registry.snapshot(), height=next_height, params=self.params)
        next_block_miner_subsidy = miner_subsidy_chipbits(next_height, self.params)
        next_block_node_reward = node_reward_pool_chipbits(next_height, self.params)
        supply = self.supply_snapshot()
        return {
            "current_height": current_height,
            "current_epoch": 0 if current_height is None else current_height // self.params.epoch_length_blocks,
            "current_bits": current_bits,
            "current_difficulty_ratio": self._difficulty_ratio(current_bits),
            "next_retarget_height": self._next_retarget_height(next_height),
            "registered_nodes_count": len(registered_nodes),
            "active_nodes_count": len(active_nodes),
            "next_block_miner_subsidy_chipbits": next_block_miner_subsidy,
            "next_block_node_reward_chipbits": next_block_node_reward,
            "next_block_epoch_closing": is_epoch_reward_height(next_height, self.params),
            "scheduled_supply_chipbits": supply["scheduled_supply_chipbits"],
            "scheduled_miner_supply_chipbits": supply["scheduled_miner_supply_chipbits"],
            "scheduled_node_reward_supply_chipbits": supply["scheduled_node_reward_supply_chipbits"],
            "scheduled_remaining_supply_chipbits": supply["scheduled_remaining_supply_chipbits"],
            "materialized_supply_chipbits": supply["materialized_supply_chipbits"],
            "materialized_miner_supply_chipbits": supply["materialized_miner_supply_chipbits"],
            "materialized_node_reward_supply_chipbits": supply["materialized_node_reward_supply_chipbits"],
            "undistributed_node_reward_supply_chipbits": supply["undistributed_node_reward_supply_chipbits"],
            "minted_supply_chipbits": supply["minted_supply_chipbits"],
            "miner_minted_supply_chipbits": supply["miner_minted_supply_chipbits"],
            "node_minted_supply_chipbits": supply["node_minted_supply_chipbits"],
            "circulating_supply_chipbits": supply["circulating_supply_chipbits"],
            "immature_supply_chipbits": supply["immature_supply_chipbits"],
            "max_supply_chipbits": self.params.max_money_chipbits,
            "remaining_supply_chipbits": supply["remaining_supply_chipbits"],
        }

    def supply_snapshot(self) -> dict[str, int | str | None]:
        """Return a supply snapshot for the active chain.

        Scheduled supply is the protocol budget through the tip height. Materialized
        supply is what actually appeared in coinbase outputs; undistributed node
        reward pools are intentionally excluded from public circulating supply.
        """

        tip = self.chain_tip()
        height = None if tip is None else tip.height
        tip_hash = None if tip is None else tip.block_hash
        scheduled_supply_chipbits = total_subsidy_through_height(-1 if tip is None else tip.height, self.params)
        scheduled_miner_supply_chipbits = 0
        scheduled_node_reward_supply_chipbits = 0
        if tip is not None:
            for block_height in range(tip.height + 1):
                miner_subsidy_chipbits, node_reward_chipbits = subsidy_split_chipbits(block_height, self.params)
                scheduled_miner_supply_chipbits += miner_subsidy_chipbits
                scheduled_node_reward_supply_chipbits += node_reward_chipbits
        materialized_supply = self._materialized_supply_snapshot()
        maturity_supply = self._supply_snapshot()
        burned_supply_chipbits = 0
        circulating_supply_chipbits = (
            materialized_supply["materialized_supply_chipbits"]
            - burned_supply_chipbits
            - maturity_supply["immature_supply_chipbits"]
        )
        undistributed_node_reward_supply_chipbits = max(
            0,
            scheduled_node_reward_supply_chipbits - materialized_supply["materialized_node_reward_supply_chipbits"],
        )
        return {
            "network": self.network,
            "height": height,
            "tip_hash": tip_hash,
            "max_supply_chipbits": self.params.max_money_chipbits,
            "scheduled_supply_chipbits": scheduled_supply_chipbits,
            "scheduled_miner_supply_chipbits": scheduled_miner_supply_chipbits,
            "scheduled_node_reward_supply_chipbits": scheduled_node_reward_supply_chipbits,
            "scheduled_remaining_supply_chipbits": max(0, self.params.max_money_chipbits - scheduled_supply_chipbits),
            "materialized_supply_chipbits": materialized_supply["materialized_supply_chipbits"],
            "materialized_miner_supply_chipbits": materialized_supply["materialized_miner_supply_chipbits"],
            "materialized_node_reward_supply_chipbits": materialized_supply["materialized_node_reward_supply_chipbits"],
            "undistributed_node_reward_supply_chipbits": undistributed_node_reward_supply_chipbits,
            "minted_supply_chipbits": materialized_supply["materialized_supply_chipbits"],
            "miner_minted_supply_chipbits": materialized_supply["materialized_miner_supply_chipbits"],
            "node_minted_supply_chipbits": materialized_supply["materialized_node_reward_supply_chipbits"],
            "burned_supply_chipbits": burned_supply_chipbits,
            "immature_supply_chipbits": maturity_supply["immature_supply_chipbits"],
            "circulating_supply_chipbits": circulating_supply_chipbits,
            "remaining_supply_chipbits": max(
                0,
                self.params.max_money_chipbits - materialized_supply["materialized_supply_chipbits"],
            ),
        }

    def supply_diagnostics(self) -> dict[str, object]:
        """Return a detailed supply and maturity snapshot for the active chain."""

        summary = self.economy_summary()
        protocol_supply = self.supply_snapshot()
        supply = self._supply_snapshot()
        return {
            **summary,
            "network": protocol_supply["network"],
            "height": protocol_supply["height"],
            "burned_supply_chipbits": protocol_supply["burned_supply_chipbits"],
            "confirmed_unspent_supply_chipbits": supply["confirmed_unspent_supply_chipbits"],
            "spendable_utxo_count": supply["spendable_utxo_count"],
            "immature_utxo_count": supply["immature_utxo_count"],
            "total_utxo_count": supply["total_utxo_count"],
        }

    def top_miners(self, *, limit: int = 10) -> list[dict[str, object]]:
        """Return miner payout addresses ranked by aggregate mining income."""

        tip = self.chain_tip()
        if tip is None:
            return []
        aggregated: dict[str, dict[str, object]] = {}
        for height in range(tip.height + 1):
            block = self.get_block_by_height(height)
            if block is None or not block.transactions or not block.transactions[0].outputs:
                continue
            miner_address = block.transactions[0].outputs[0].recipient
            try:
                fees_chipbits = self._block_total_fees_chipbits(height, block)
            except ValueError:
                fees_chipbits = 0
            node_pool_chipbits = node_reward_pool_chipbits(height, self.params)
            entry = aggregated.setdefault(
                miner_address,
                {
                    "miner_address": miner_address,
                    "blocks_mined": 0,
                    "total_miner_subsidy_chipbits": 0,
                    "total_fees_chipbits": 0,
                    "total_node_reward_chipbits": 0,
                    "last_mined_height": None,
                },
            )
            entry["blocks_mined"] = int(entry["blocks_mined"]) + 1
            entry["total_miner_subsidy_chipbits"] = int(entry["total_miner_subsidy_chipbits"]) + int(
                miner_subsidy_chipbits(height, self.params)
            )
            entry["total_fees_chipbits"] = int(entry["total_fees_chipbits"]) + int(fees_chipbits)
            entry["total_node_reward_chipbits"] = int(entry["total_node_reward_chipbits"]) + int(node_pool_chipbits)
            entry["last_mined_height"] = height
        rows = list(aggregated.values())
        rows.sort(
            key=lambda row: (
                -int(row["total_miner_subsidy_chipbits"]),
                -int(row["blocks_mined"]),
                str(row["miner_address"]),
            )
        )
        return rows[:limit]

    def top_nodes(self, *, limit: int = 10) -> list[dict[str, object]]:
        """Return nodes ranked by aggregate node reward income."""

        rows = self.node_income_summary()
        rows.sort(
            key=lambda row: (
                -int(row["total_node_rewards_chipbits"]),
                -int(row["reward_count"]),
                str(row["node_id"]),
            )
        )
        return rows[:limit]

    def top_recipients(self, *, limit: int = 10) -> list[dict[str, object]]:
        """Return reward recipients ranked by aggregate rewards."""

        recipients = self._reward_recipient_addresses()
        rows = [self.reward_summary(address) for address in recipients]
        rows.sort(
            key=lambda row: (
                -int(row["total_rewards_chipbits"]),
                -int(row["payout_count"]),
                str(row["address"]),
            )
        )
        return rows[:limit]

    def address_history(self, recipient: str, *, limit: int = 50, descending: bool = True) -> list[dict[str, object]]:
        """Return a minimal confirmed transaction history for one address."""

        tip = self.chain_tip()
        if tip is None:
            return []

        rows: list[dict[str, object]] = []
        utxo_view = InMemoryUtxoView()
        for height in range(tip.height + 1):
            block = self.get_block_by_height(height)
            if block is None:
                continue
            for tx_index, transaction in enumerate(block.transactions):
                incoming_chipbits = sum(
                    int(tx_output.value)
                    for tx_output in transaction.outputs
                    if tx_output.recipient == recipient
                )
                outgoing_chipbits = 0
                if tx_index > 0 and not is_special_node_transaction(transaction):
                    for tx_input in transaction.inputs:
                        spent_entry = utxo_view.get(tx_input.previous_output)
                        if spent_entry is not None and spent_entry.output.recipient == recipient:
                            outgoing_chipbits += int(spent_entry.output.value)
                if incoming_chipbits or outgoing_chipbits:
                    rows.append(
                        {
                            "block_height": height,
                            "block_hash": block.block_hash(),
                            "txid": transaction.txid(),
                            "incoming_chipbits": incoming_chipbits,
                            "outgoing_chipbits": outgoing_chipbits,
                            "net_chipbits": incoming_chipbits - outgoing_chipbits,
                            "timestamp": block.header.timestamp,
                        }
                    )
                if not is_special_node_transaction(transaction):
                    utxo_view.apply_transaction(transaction, height, is_coinbase=tx_index == 0)

        rows.sort(key=lambda row: (row["block_height"], row["txid"]))
        if descending:
            rows.reverse()
        return rows[:limit]

    def _validation_context_for_view(self, utxo_view) -> ValidationContext:
        """Build a validation context for mempool admission against a given UTXO view."""

        tip = self.headers.get_tip()
        return ValidationContext(
            height=0 if tip is None else tip.height + 1,
            median_time_past=0 if tip is None else self.headers.get(tip.block_hash).timestamp,
            params=self.params,
            utxo_view=utxo_view,
            node_registry_view=self.node_registry.snapshot(),
            reward_attestation_identities=frozenset(self.reward_attestations.attestation_identities()),
            reward_attestation_bundles=tuple(stored.bundle for stored in self.reward_attestations.list_bundles()),
            settled_epoch_indexes=frozenset(self.reward_settlements.settled_epoch_indexes()),
            epoch_seed_by_index=self._epoch_seed_map(0 if tip is None else tip.height + 1),
        )

    def _find_transaction_in_active_chain(self, txid: str) -> Transaction | None:
        """Return a confirmed active-chain transaction when present."""

        tip = self.chain_tip()
        if tip is None:
            return None
        for height in range(tip.height + 1):
            block = self.get_block_by_height(height)
            if block is None:
                continue
            for transaction in block.transactions:
                if transaction.txid() == txid:
                    return transaction
        return None

    def _known_confirmed_transaction_ids(self) -> set[str]:
        """Return transaction ids known to be available from chain history or current UTXOs."""

        txids: set[str] = set()
        tip = self.chain_tip()
        if tip is not None:
            for height in range(tip.height + 1):
                block = self.get_block_by_height(height)
                if block is None:
                    continue
                txids.update(transaction.txid() for transaction in block.transactions)
        txids.update(outpoint.txid for outpoint, _entry in self.chainstate.list_utxos())
        return txids

    def _expected_bits_for_height(self, height: int) -> int:
        """Return the required compact target for a candidate block height."""

        if height <= 0:
            return self.params.genesis_bits
        previous_hash = self.headers.get_hash_at_height(height - 1)
        if previous_hash is None:
            return self.params.genesis_bits
        previous_header = self.headers.get(previous_hash)
        if previous_header is None:
            return self.params.genesis_bits
        if height % self.params.difficulty_adjustment_window != 0:
            return previous_header.bits

        window_start_height = max(0, height - self.params.difficulty_adjustment_window)
        first_hash = self.headers.get_hash_at_height(window_start_height)
        if first_hash is None:
            return previous_header.bits
        first_header = self.headers.get(first_hash)
        if first_header is None:
            return previous_header.bits
        actual_timespan_seconds = max(1, previous_header.timestamp - first_header.timestamp)
        return calculate_next_work_required(
            previous_bits=previous_header.bits,
            actual_timespan_seconds=actual_timespan_seconds,
            params=self.params,
        )

    def _expected_bits_for_candidate_height(self, height: int, validated_headers: list) -> int:
        """Return required bits while validating a candidate branch path."""

        if height <= 0 or not validated_headers:
            return self.params.genesis_bits

        previous_header = validated_headers[-1]
        if height % self.params.difficulty_adjustment_window != 0:
            return previous_header.bits

        window_start_height = max(0, height - self.params.difficulty_adjustment_window)
        first_header = validated_headers[window_start_height]
        actual_timespan_seconds = max(1, previous_header.timestamp - first_header.timestamp)
        return calculate_next_work_required(
            previous_bits=previous_header.bits,
            actual_timespan_seconds=actual_timespan_seconds,
            params=self.params,
        )

    def list_active_reward_nodes(self, height: int):
        """Return reward-eligible nodes for the supplied height."""

        return active_node_records(self.node_registry.snapshot(), height=height, params=self.params)

    def _apply_node_registry_block(self, block: Block, height: int, *, registry_view=None) -> None:
        """Apply node special transactions from a block to registry state."""

        target_registry = self.node_registry if registry_view is None else registry_view
        for transaction in block.transactions[1:]:
            if is_special_node_transaction(transaction):
                apply_special_node_transaction(transaction, height=height, registry_view=target_registry)

    def _apply_native_reward_block(self, block: Block, height: int) -> None:
        """Persist native reward-node payloads from one connected block."""

        bundles: list[StoredRewardAttestationBundle] = []
        settlements: list[StoredEpochSettlement] = []
        attestation_identities: set[tuple[int, int, str, str]] = set()
        settled_epoch_indexes: set[int] = set()
        self._collect_native_reward_block(
            block,
            height,
            attestation_bundles=bundles,
            settled_epochs=settlements,
            attestation_identities=attestation_identities,
            settled_epoch_indexes=settled_epoch_indexes,
            persist=True,
        )

    def _collect_native_reward_block(
        self,
        block: Block,
        height: int,
        *,
        attestation_bundles: list[StoredRewardAttestationBundle],
        settled_epochs: list[StoredEpochSettlement],
        attestation_identities: set[tuple[int, int, str, str]],
        settled_epoch_indexes: set[int],
        persist: bool = False,
    ) -> None:
        """Collect or persist native reward payloads from one block."""

        for transaction in block.transactions[1:]:
            kind = transaction.metadata.get("kind")
            if kind == REWARD_ATTESTATION_BUNDLE_KIND:
                bundle = parse_reward_attestation_bundle_metadata(transaction.metadata)
                stored = StoredRewardAttestationBundle(txid=transaction.txid(), block_height=height, bundle=bundle)
                attestation_bundles.append(stored)
                attestation_identities.update(
                    (attestation.epoch_index, attestation.check_window_index, attestation.candidate_node_id, attestation.verifier_node_id)
                    for attestation in bundle.attestations
                )
                if persist:
                    self.reward_attestations.add_bundle(txid=stored.txid, block_height=height, bundle=bundle)
            elif kind == REWARD_SETTLE_EPOCH_KIND:
                settlement = parse_reward_settlement_metadata(transaction.metadata)
                stored = StoredEpochSettlement(txid=transaction.txid(), block_height=height, settlement=settlement)
                settled_epochs.append(stored)
                settled_epoch_indexes.add(settlement.epoch_index)
                if persist:
                    self.reward_settlements.add_settlement(txid=stored.txid, block_height=height, settlement=settlement)

    def _epoch_seed_map(self, next_height: int, *, path_hashes: list[str] | None = None) -> dict[int, bytes]:
        """Return known deterministic epoch seeds up to the current or candidate height."""

        last_epoch = max(0, next_height // self.params.epoch_length_blocks)
        mapping: dict[int, bytes] = {0: epoch_seed("00" * 32, 0)}
        for epoch_index in range(1, last_epoch + 1):
            previous_close_height = epoch_close_height(epoch_index - 1, self.params)
            previous_close_hash = None
            if path_hashes is not None and previous_close_height < len(path_hashes):
                previous_close_hash = path_hashes[previous_close_height]
            else:
                previous_close_hash = self.headers.get_hash_at_height(previous_close_height)
            if previous_close_hash is None:
                break
            mapping[epoch_index] = epoch_seed(previous_close_hash, epoch_index)
        return mapping

    def _disconnected_branch_transactions(self, previous_tip, new_tip_hash: str) -> list[Transaction]:
        """Return non-coinbase transactions from blocks disconnected by a reorg."""

        if previous_tip is None:
            return []
        old_path = self.headers.path_to_root(previous_tip.block_hash)
        new_path = self.headers.path_to_root(new_tip_hash)
        common_prefix = 0
        for old_hash, new_hash in zip(old_path, new_path):
            if old_hash != new_hash:
                break
            common_prefix += 1

        disconnected_hashes = old_path[common_prefix:]
        transactions: list[Transaction] = []
        for block_hash in disconnected_hashes:
            block = self.blocks.get(block_hash)
            if block is None:
                continue
            transactions.extend(block.transactions[1:])
        return transactions

    def _block_total_fees_chipbits(self, height: int, block: Block) -> int:
        """Replay active-chain state up to one block and compute contained fees."""

        utxo_view, node_registry_view = self._replay_chain_state_before_height(height)
        total_fees_chipbits = 0
        for index, transaction in enumerate(block.transactions):
            if index == 0 or is_coinbase_transaction(transaction):
                continue
            if is_special_node_transaction(transaction):
                if index > 0:
                    apply_special_node_transaction(transaction, height=height, registry_view=node_registry_view)
                continue
            input_total_chipbits = 0
            for tx_input in transaction.inputs:
                entry = utxo_view.get(tx_input.previous_output)
                if entry is None:
                    raise ValueError("Cannot derive fees for block with unresolved input.")
                input_total_chipbits += int(entry.output.value)
            output_total_chipbits = sum(int(tx_output.value) for tx_output in transaction.outputs)
            total_fees_chipbits += input_total_chipbits - output_total_chipbits
            utxo_view.apply_transaction(transaction, height)
        return total_fees_chipbits

    def _reward_recipient_addresses(self) -> list[str]:
        """Return all known reward-recipient addresses from active-chain coinbases."""

        tip = self.chain_tip()
        if tip is None:
            return []
        addresses: set[str] = set()
        for height in range(tip.height + 1):
            block = self.get_block_by_height(height)
            if block is None or not block.transactions:
                continue
            coinbase = block.transactions[0]
            for tx_output in coinbase.outputs:
                addresses.add(tx_output.recipient)
        return sorted(addresses)

    def _supply_snapshot(self) -> dict[str, int]:
        """Return current active-chain supply split by maturity."""

        tip = self.chain_tip()
        spend_height = 0 if tip is None else tip.height + 1
        circulating_spendable_supply_chipbits = 0
        immature_supply_chipbits = 0
        confirmed_unspent_supply_chipbits = 0
        spendable_utxo_count = 0
        immature_utxo_count = 0
        total_utxo_count = 0
        for _outpoint, entry in self.chainstate.list_utxos():
            total_utxo_count += 1
            amount_chipbits = int(entry.output.value)
            confirmed_unspent_supply_chipbits += amount_chipbits
            mature = True if not entry.is_coinbase else spend_height - int(entry.height) >= self.params.coinbase_maturity
            if mature:
                circulating_spendable_supply_chipbits += amount_chipbits
                spendable_utxo_count += 1
            else:
                immature_supply_chipbits += amount_chipbits
                immature_utxo_count += 1
        return {
            "circulating_spendable_supply_chipbits": circulating_spendable_supply_chipbits,
            "immature_supply_chipbits": immature_supply_chipbits,
            "confirmed_unspent_supply_chipbits": confirmed_unspent_supply_chipbits,
            "spendable_utxo_count": spendable_utxo_count,
            "immature_utxo_count": immature_utxo_count,
            "total_utxo_count": total_utxo_count,
        }

    def _materialized_supply_snapshot(self) -> dict[str, int]:
        """Return issued supply that actually exists in active-chain coinbases."""

        tip = self.chain_tip()
        materialized_miner_supply_chipbits = 0
        materialized_node_reward_supply_chipbits = 0
        if tip is not None:
            for block_height in range(tip.height + 1):
                block = self.get_block_by_height(block_height)
                if block is None or not block.transactions or not block.transactions[0].outputs:
                    continue
                miner_subsidy, _node_pool = subsidy_split_chipbits(block_height, self.params)
                materialized_miner_supply_chipbits += miner_subsidy
                materialized_node_reward_supply_chipbits += sum(
                    int(tx_output.value)
                    for tx_output in block.transactions[0].outputs[1:]
                )
        return {
            "materialized_supply_chipbits": materialized_miner_supply_chipbits + materialized_node_reward_supply_chipbits,
            "materialized_miner_supply_chipbits": materialized_miner_supply_chipbits,
            "materialized_node_reward_supply_chipbits": materialized_node_reward_supply_chipbits,
        }

    def _replay_chain_state_before_height(self, height: int) -> tuple[InMemoryUtxoView, InMemoryNodeRegistryView]:
        """Rebuild active-chain views immediately before a given block height."""

        snapshot_anchor = self.snapshot_anchor()
        if snapshot_anchor is None or height <= snapshot_anchor.height:
            utxo_view = InMemoryUtxoView()
            node_registry_view = InMemoryNodeRegistryView()
            start_height = 0
        else:
            utxo_view = InMemoryUtxoView.from_entries(self.chainstate.list_utxos())
            node_registry_view = self.node_registry.snapshot()
            start_height = snapshot_anchor.height + 1
        for current_height in range(start_height, height):
            block = self.get_block_by_height(current_height)
            if block is None:
                raise ValueError(f"Missing active-chain block at height {current_height}")
            utxo_view.apply_block(block, current_height)
            self._apply_node_registry_block(block, current_height, registry_view=node_registry_view)
        return utxo_view, node_registry_view

    def _get_chain_meta(self, key: str) -> str | None:
        """Return one stored chain metadata value when present."""

        if self.connection is None:
            return None
        row = self.connection.execute("SELECT value FROM chain_meta WHERE key = ?", (key,)).fetchone()
        if row is None:
            return None
        return str(row["value"])

    def _set_chain_meta(self, key: str, value: str) -> None:
        """Persist one chain metadata value."""

        if self.connection is None:
            raise ValueError("chain metadata persistence requires a writable SQLite-backed node service")
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO chain_meta(key, value) VALUES(?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, value),
            )

    def _retarget_window_for_candidate(self, candidate_height: int) -> dict[str, object]:
        """Return the window inputs that determine the candidate bits."""

        previous_height = candidate_height - 1
        if previous_height < 0:
            return {
                "window_start_height": None,
                "window_end_height": None,
                "first_timestamp": None,
                "last_timestamp": None,
                "actual_timespan_seconds": None,
                "target_timespan_seconds": self.params.target_block_time_seconds * self.params.difficulty_adjustment_window,
            }
        if candidate_height == 0:
            return {
                "window_start_height": None,
                "window_end_height": None,
                "first_timestamp": None,
                "last_timestamp": None,
                "actual_timespan_seconds": None,
                "target_timespan_seconds": self.params.target_block_time_seconds * self.params.difficulty_adjustment_window,
            }
        window_start_height = max(0, candidate_height - self.params.difficulty_adjustment_window)
        previous_hash = self.headers.get_hash_at_height(previous_height)
        first_hash = self.headers.get_hash_at_height(window_start_height)
        previous_header = None if previous_hash is None else self.headers.get(previous_hash)
        first_header = None if first_hash is None else self.headers.get(first_hash)
        actual_timespan_seconds = None
        if previous_header is not None and first_header is not None:
            actual_timespan_seconds = max(1, previous_header.timestamp - first_header.timestamp)
        return {
            "window_start_height": window_start_height,
            "window_end_height": previous_height,
            "first_timestamp": None if first_header is None else first_header.timestamp,
            "last_timestamp": None if previous_header is None else previous_header.timestamp,
            "actual_timespan_seconds": actual_timespan_seconds,
            "target_timespan_seconds": self.params.target_block_time_seconds * self.params.difficulty_adjustment_window,
        }

    def _next_retarget_height(self, next_height: int) -> int:
        """Return the next block height where a difficulty retarget occurs."""

        remainder = next_height % self.params.difficulty_adjustment_window
        if remainder == 0:
            return next_height
        return next_height + (self.params.difficulty_adjustment_window - remainder)

    def _difficulty_ratio(self, bits: int) -> str:
        """Return a readable relative difficulty ratio against the pow limit."""

        pow_limit = bits_to_target(self.params.genesis_bits)
        target = bits_to_target(bits)
        ratio = (Decimal(pow_limit) / Decimal(target)).quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)
        return format(ratio, "f")

    def _format_target(self, bits: int) -> str:
        """Return the expanded integer target as a decimal string."""

        return str(bits_to_target(bits))

    def _rate_string(self, numerator: int, denominator: int) -> str:
        """Return a fixed-scale decimal string for ratios used in diagnostics."""

        ratio = (Decimal(numerator) / Decimal(max(1, denominator))).quantize(
            Decimal("0.00000001"),
            rounding=ROUND_DOWN,
        )
        return format(ratio, "f")

    def _serialize_transaction(self, transaction: Transaction) -> bytes:
        """Local helper to avoid repeating import wiring in diagnostics code."""

        from ..consensus.serialization import serialize_transaction

        return serialize_transaction(transaction)
