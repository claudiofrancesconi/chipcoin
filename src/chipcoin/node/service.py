"""Node runtime facade coordinating local consensus, storage, and sync APIs."""

from __future__ import annotations

from decimal import Decimal, ROUND_DOWN
from dataclasses import dataclass
from pathlib import Path

from ..config import get_network_config
from ..consensus.models import Block, OutPoint, Transaction
from ..consensus.nodes import (
    InMemoryNodeRegistryView,
    active_node_records,
    apply_special_node_transaction,
    is_special_node_transaction,
    select_rewarded_nodes,
)
from ..consensus.params import ConsensusParams
from ..consensus.pow import bits_to_target, calculate_next_work_required, header_work
from ..consensus.serialization import deserialize_transaction
from ..consensus.economics import miner_subsidy_chipbits, node_reward_pool_chipbits, total_subsidy_through_height
from ..consensus.utxo import InMemoryUtxoView
from ..consensus.validation import ValidationContext, block_weight_units, is_coinbase_transaction, validate_block
from ..storage.blocks import SQLiteBlockRepository
from ..storage.chainstate import SQLiteChainStateRepository
from ..storage.db import initialize_database
from ..storage.headers import ChainTip, SQLiteHeaderRepository
from ..storage.mempool import SQLiteMempoolRepository
from ..storage.node_registry import SQLiteNodeRegistryRepository
from ..storage.peers import SQLitePeerRepository
from ..utils.time import unix_time
from .mempool import AcceptedTransaction, MempoolManager, MempoolPolicy
from .messages import GetBlocksMessage, GetHeadersMessage, HeadersMessage, InvMessage, InventoryVector
from .mining import BlockTemplate, MiningCoordinator
from .peers import PeerInfo, PeerManager
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
        mempool_repository,
        peer_repository=None,
        peerbook: PeerManager | None = None,
        time_provider=unix_time,
    ) -> None:
        self.network = network
        self.params = params
        self.headers = headers
        self.blocks = blocks
        self.chainstate = chainstate
        self.node_registry = node_registry
        self.peer_repository = peer_repository
        self.peerbook = peerbook or PeerManager()
        self.time_provider = time_provider
        self.mempool = MempoolManager(
            repository=mempool_repository,
            chainstate=chainstate,
            validation_context_factory=self._validation_context_for_view,
            time_provider=time_provider,
            known_chain_transaction_lookup=self._find_transaction_in_active_chain,
            policy=MempoolPolicy(),
        )
        self.mining = MiningCoordinator(params=params, time_provider=time_provider)

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
            mempool_repository=SQLiteMempoolRepository(connection),
            peer_repository=SQLitePeerRepository(connection),
            time_provider=time_provider,
        )

    def start(self) -> None:
        """Local-only startup placeholder."""

        return None

    def receive_transaction(self, transaction: Transaction) -> AcceptedTransaction:
        """Validate and stage a transaction into the local mempool."""

        return self.mempool.accept(transaction)

    def build_candidate_block(self, miner_address: str) -> BlockTemplate:
        """Construct a local candidate block from current chain state and mempool."""

        tip = self.headers.get_tip()
        height = 0 if tip is None else tip.height + 1
        previous_block_hash = "00" * 32 if tip is None else tip.block_hash
        expected_bits = self._expected_bits_for_height(height)
        return self.mining.build_block_template(
            previous_block_hash=previous_block_hash,
            height=height,
            miner_address=miner_address,
            bits=expected_bits,
            mempool_entries=self.mempool.list_transactions(),
            node_registry_view=self.node_registry.snapshot(),
            confirmed_transaction_ids=self._known_confirmed_transaction_ids(),
        )

    def expected_next_bits(self) -> int:
        """Return the compact target required for the next candidate block."""

        tip = self.headers.get_tip()
        next_height = 0 if tip is None else tip.height + 1
        return self._expected_bits_for_height(next_height)

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
        self.headers.set_tip(block.block_hash(), height)
        self.mempool.reconcile()
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
        utxo_view = InMemoryUtxoView()
        node_registry_view = InMemoryNodeRegistryView()
        previous_hash = "00" * 32
        median_time_past = 0
        applied_blocks = 0
        validated_headers = []

        for height, block_hash in enumerate(path_hashes):
            block = self.blocks.get(block_hash)
            if block is None:
                raise ValueError(f"Cannot activate chain without stored block: {block_hash}")
            context = ValidationContext(
                height=height,
                median_time_past=median_time_past,
                params=self.params,
                utxo_view=utxo_view,
                node_registry_view=node_registry_view,
                expected_previous_block_hash=previous_hash,
                expected_bits=self._expected_bits_for_candidate_height(height, validated_headers),
            )
            validate_block(block, context)
            utxo_view.apply_block(block, height)
            self._apply_node_registry_block(block, height, registry_view=node_registry_view)
            validated_headers.append(block.header)
            previous_hash = block_hash
            median_time_past = block.header.timestamp
            applied_blocks += 1

        self.chainstate.replace_all(utxo_view.list_entries())
        self.node_registry.replace_all(node_registry_view.list_records())
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

        transaction, offset = deserialize_transaction(bytes.fromhex(raw_hex))
        if offset != len(bytes.fromhex(raw_hex)):
            raise ValueError("Raw transaction contains trailing bytes.")
        return self.receive_transaction(transaction)

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

    def add_peer(self, host: str, port: int) -> PeerInfo:
        """Add a peer to the in-memory local peerbook."""

        peer = PeerInfo(host=host, port=port, network=self.network)
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
        by_network: dict[str, int] = {}
        by_direction: dict[str, int] = {}
        by_handshake_status = {"complete": 0, "incomplete": 0, "unknown": 0}
        backoff_peers = []
        recent_errors = []
        worst_peer = None
        most_disconnected_peer = None

        for peer in peers:
            error_class = peer["protocol_error_class"]
            if isinstance(error_class, str):
                by_error_class[error_class] = by_error_class.get(error_class, 0) + 1
            network = str(peer["network"])
            by_network[network] = by_network.get(network, 0) + 1
            direction = peer["direction"]
            if isinstance(direction, str):
                by_direction[direction] = by_direction.get(direction, 0) + 1
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
            if most_disconnected_peer is None or _disconnect_sort_key(peer) > _disconnect_sort_key(most_disconnected_peer):
                most_disconnected_peer = peer

        backoff_peers.sort(key=lambda peer: (peer["backoff_until"], peer["host"], peer["port"]))
        recent_errors.sort(
            key=lambda peer: (
                -(peer["last_error_at"] if isinstance(peer["last_error_at"], int) else -1),
                peer["host"],
                peer["port"],
            )
        )
        return {
            "error_class_counts": dict(sorted(by_error_class.items())),
            "peer_count_by_network": dict(sorted(by_network.items())),
            "peer_count_by_direction": dict(sorted(by_direction.items())),
            "peer_count_by_handshake_status": by_handshake_status,
            "backoff_peer_count": len(backoff_peers),
            "backoff_peers": backoff_peers,
            "worst_score_peer": worst_peer,
            "most_disconnected_peer": most_disconnected_peer,
            "most_recent_error_peer": None if not recent_errors else recent_errors[0],
            "peer_count": len(peers),
        }

    def record_peer_observation(
        self,
        *,
        host: str,
        port: int,
        direction: str | None = None,
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
    ) -> PeerInfo:
        """Persist the latest peer session metadata for diagnostics."""

        peer = PeerInfo(
            host=host,
            port=port,
            network=self.network,
            direction=direction,
            last_seen=self.time_provider(),
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
        )
        self.peerbook.add(peer)
        if self.peer_repository is not None:
            self.peer_repository.observe(peer)
        return peer

    def _peer_diagnostics_payload(self, peer: PeerInfo) -> dict[str, object]:
        """Render one peer record into deterministic diagnostic JSON fields."""

        network_magic_hex = get_network_config(peer.network).magic.hex()
        return {
            "host": peer.host,
            "port": peer.port,
            "network": peer.network,
            "network_magic_hex": network_magic_hex,
            "direction": peer.direction,
            "node_id": peer.node_id,
            "handshake_complete": peer.handshake_complete,
            "score": peer.score,
            "reconnect_attempts": peer.reconnect_attempts,
            "backoff_until": peer.backoff_until,
            "last_seen": peer.last_seen,
            "session_started_at": peer.session_started_at,
            "last_known_height": peer.last_known_height,
            "disconnect_count": peer.disconnect_count,
            "last_error": peer.last_error,
            "last_error_at": peer.last_error_at,
            "protocol_error_class": peer.protocol_error_class,
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
        peers = self.list_peers()
        return {
            "network": self.network,
            "network_magic_hex": get_network_config(self.network).magic.hex(),
            "height": None if tip is None else tip.height,
            "tip_hash": None if tip is None else tip.block_hash,
            "current_bits": self.params.genesis_bits if header is None else header.bits,
            "current_target": self._format_target(self.params.genesis_bits if header is None else header.bits),
            "current_difficulty_ratio": self._difficulty_ratio(self.params.genesis_bits if header is None else header.bits),
            "expected_next_bits": self.expected_next_bits(),
            "expected_next_target": self._format_target(self.expected_next_bits()),
            "cumulative_work": None if record is None else record.cumulative_work,
            "mempool_size": len(self.mempool.list_transactions()),
            "peer_count": len(peers),
            "handshaken_peer_count": sum(1 for peer in peers if peer.handshake_complete),
            "next_block_reward_winners": [
                {
                    "node_id": rewarded_node.node_id,
                    "payout_address": rewarded_node.payout_address,
                    "reward_chipbits": rewarded_node.reward_chipbits,
                    "score_hex": rewarded_node.score_hex,
                }
                for rewarded_node in rewarded_nodes
            ],
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
            rows.append(
                {
                    "node_id": record.node_id,
                    "payout_address": record.payout_address,
                    "owner_pubkey": record.owner_pubkey.hex(),
                    "registered_at_height": record.registered_height,
                    "last_renewal_height": record.last_renewed_height,
                    "last_renewal_epoch": renewal_epoch,
                    "active": record.last_renewed_height < next_height and renewal_epoch == current_epoch,
                    "eligible_from_height": record.last_renewed_height + 1,
                    "epoch_status": "current" if renewal_epoch == current_epoch else "stale",
                    "current_epoch": current_epoch,
                }
            )
        return rows

    def next_winners_diagnostics(self) -> dict[str, object]:
        """Return deterministic next-block node reward winner diagnostics."""

        tip = self.chain_tip()
        next_height = 0 if tip is None else tip.height + 1
        previous_block_hash = "00" * 32 if tip is None else tip.block_hash
        node_reward_pool = node_reward_pool_chipbits(next_height, self.params)
        rewarded_nodes = select_rewarded_nodes(
            self.node_registry.snapshot(),
            height=next_height,
            previous_block_hash=previous_block_hash,
            node_reward_pool_chipbits=node_reward_pool,
            params=self.params,
        )
        active_nodes = active_node_records(self.node_registry.snapshot(), height=next_height, params=self.params)
        distributed_node_reward_chipbits = sum(rewarded_node.reward_chipbits for rewarded_node in rewarded_nodes)
        return {
            "next_block_height": next_height,
            "active_nodes_count": len(active_nodes),
            "selected_winners": [
                {
                    "node_id": rewarded_node.node_id,
                    "payout_address": rewarded_node.payout_address,
                    "score_hex": rewarded_node.score_hex,
                    "reward_chipbits": rewarded_node.reward_chipbits,
                }
                for rewarded_node in rewarded_nodes
            ],
            "reward_per_winner_chipbits": 0 if not rewarded_nodes else rewarded_nodes[0].reward_chipbits,
            "miner_subsidy_chipbits": miner_subsidy_chipbits(next_height, self.params),
            "node_reward_pool_chipbits": node_reward_pool,
            "remainder_to_miner_chipbits": node_reward_pool - distributed_node_reward_chipbits,
            "selection_seed": previous_block_hash,
        }

    def reward_history(self, recipient: str, *, limit: int = 50, descending: bool = True) -> list[dict[str, object]]:
        """Return confirmed reward payouts paid to one address from active-chain coinbases."""

        tip = self.chain_tip()
        if tip is None:
            return []

        rows: list[dict[str, object]] = []
        for height in range(tip.height + 1):
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
            node_pool = node_reward_pool_chipbits(height, self.params)
            rewarded_nodes = select_rewarded_nodes(
                self._replay_chain_state_before_height(height)[1],
                height=height,
                previous_block_hash=block.header.previous_block_hash,
                node_reward_pool_chipbits=node_pool,
                params=self.params,
            )
            distributed_node_reward = sum(node.reward_chipbits for node in rewarded_nodes)
            miner_subsidy_effective = miner_subsidy + (node_pool - distributed_node_reward)
            mature = height + self.params.coinbase_maturity < (tip.height + 1)

            if coinbase.outputs and coinbase.outputs[0].recipient == recipient:
                rows.append(
                    {
                        "block_height": height,
                        "block_hash": block.block_hash(),
                        "txid": coinbase_txid,
                        "reward_type": "miner_subsidy",
                        "amount_chipbits": miner_subsidy_effective,
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

        rows.sort(key=lambda row: (row["block_height"], row["txid"], row["reward_type"]))
        if descending:
            rows.reverse()
        return rows[:limit]

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
            distributed_node_reward_chipbits = sum(node.reward_chipbits for node in rewarded_nodes)
            rows.append(
                {
                    "height": height,
                    "block_hash": block.block_hash(),
                    "miner_subsidy_chipbits": miner_subsidy_chipbits(height, self.params),
                    "fees_chipbits": fees_chipbits,
                    "remainder_from_node_pool_chipbits": node_pool_chipbits - distributed_node_reward_chipbits,
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
        current_miner_subsidy = miner_subsidy_chipbits(next_height, self.params)
        current_node_reward_pool = node_reward_pool_chipbits(next_height, self.params)
        total_emitted_supply_chipbits = total_subsidy_through_height(-1 if tip is None else tip.height, self.params)
        supply = self._supply_snapshot()
        return {
            "current_height": current_height,
            "current_epoch": 0 if current_height is None else current_height // self.params.epoch_length_blocks,
            "current_bits": current_bits,
            "current_difficulty_ratio": self._difficulty_ratio(current_bits),
            "next_retarget_height": self._next_retarget_height(next_height),
            "registered_nodes_count": len(registered_nodes),
            "active_nodes_count": len(active_nodes),
            "current_miner_subsidy_chipbits": current_miner_subsidy,
            "current_node_reward_pool_chipbits": current_node_reward_pool,
            "total_emitted_supply_chipbits": total_emitted_supply_chipbits,
            "circulating_spendable_supply_chipbits": supply["circulating_spendable_supply_chipbits"],
            "immature_supply_chipbits": supply["immature_supply_chipbits"],
            "max_supply_chipbits": self.params.max_money_chipbits,
            "remaining_supply_chipbits": max(0, self.params.max_money_chipbits - total_emitted_supply_chipbits),
        }

    def supply_diagnostics(self) -> dict[str, object]:
        """Return a detailed supply and maturity snapshot for the active chain."""

        summary = self.economy_summary()
        supply = self._supply_snapshot()
        return {
            **summary,
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
            rewarded_nodes = select_rewarded_nodes(
                self._replay_chain_state_before_height(height)[1],
                height=height,
                previous_block_hash=block.header.previous_block_hash,
                node_reward_pool_chipbits=node_pool_chipbits,
                params=self.params,
            )
            distributed_node_reward_chipbits = sum(node.reward_chipbits for node in rewarded_nodes)
            entry = aggregated.setdefault(
                miner_address,
                {
                    "miner_address": miner_address,
                    "blocks_mined": 0,
                    "total_miner_subsidy_chipbits": 0,
                    "total_fees_chipbits": 0,
                    "total_remainder_from_node_pool_chipbits": 0,
                    "last_mined_height": None,
                },
            )
            entry["blocks_mined"] = int(entry["blocks_mined"]) + 1
            entry["total_miner_subsidy_chipbits"] = int(entry["total_miner_subsidy_chipbits"]) + int(
                miner_subsidy_chipbits(height, self.params)
            )
            entry["total_fees_chipbits"] = int(entry["total_fees_chipbits"]) + int(fees_chipbits)
            entry["total_remainder_from_node_pool_chipbits"] = int(
                entry["total_remainder_from_node_pool_chipbits"]
            ) + int(node_pool_chipbits - distributed_node_reward_chipbits)
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

    def _replay_chain_state_before_height(self, height: int) -> tuple[InMemoryUtxoView, InMemoryNodeRegistryView]:
        """Rebuild active-chain views immediately before a given block height."""

        utxo_view = InMemoryUtxoView()
        node_registry_view = InMemoryNodeRegistryView()
        for current_height in range(height):
            block = self.get_block_by_height(current_height)
            if block is None:
                raise ValueError(f"Missing active-chain block at height {current_height}")
            utxo_view.apply_block(block, current_height)
            self._apply_node_registry_block(block, current_height, registry_view=node_registry_view)
        return utxo_view, node_registry_view

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
