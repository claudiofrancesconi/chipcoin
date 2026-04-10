"""Header-first synchronization planning and orchestration."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass

from ..consensus.models import Block, BlockHeader
from ..consensus.pow import calculate_next_work_required, header_work, verify_proof_of_work
from ..consensus.validation import ContextualValidationError, StatelessValidationError
from .messages import GetHeadersMessage


@dataclass(frozen=True)
class HeaderIngestResult:
    """Result of ingesting one or more headers from a peer."""

    headers_received: int
    parent_unknown: str | None
    best_tip_hash: str | None
    best_tip_height: int | None
    missing_block_hashes: tuple[str, ...]
    needs_more_headers: bool = False


@dataclass(frozen=True)
class BlockIngestResult:
    """Result of receiving a block payload."""

    block_hash: str
    activated_tip: str | None
    reorged: bool
    parent_unknown: str | None = None
    accepted_blocks: int = 1
    reorg_depth: int = 0
    old_tip: str | None = None
    new_tip: str | None = None
    common_ancestor: str | None = None
    readded_transaction_count: int = 0


@dataclass(frozen=True)
class SyncResult:
    """Summary of a synchronization attempt."""

    headers_received: int
    blocks_fetched: int
    activated_tip: str | None
    parent_unknown: str | None = None
    reorged: bool = False
    reorg_depth: int = 0
    old_tip: str | None = None
    new_tip: str | None = None
    common_ancestor: str | None = None
    readded_transaction_count: int = 0


@dataclass(frozen=True)
class BlockDownloadAssignment:
    """One scheduled block download request."""

    block_hash: str
    peer_id: str
    deadline_at: float
    attempt: int


@dataclass(frozen=True)
class BlockRequestState:
    """Tracked in-flight block request state."""

    block_hash: str
    peer_id: str
    requested_at: float
    deadline_at: float
    attempt: int


class SyncManager:
    """Coordinate header download, block fetch, and chain advancement."""

    _MAX_ORPHAN_BLOCKS = 256

    def __init__(self, *, node, max_headers: int = 2000) -> None:
        self.node = node
        self.max_headers = max_headers
        self._orphan_blocks: OrderedDict[str, dict[str, Block]] = OrderedDict()
        self._inflight_blocks: dict[str, BlockRequestState] = {}
        self._header_peer_heights: dict[str, int] = {}
        self._header_peer_tips: dict[str, str] = {}
        self._block_peer_counts: dict[str, int] = {}
        self._header_peer_counts: dict[str, int] = {}
        self._stalled_peer_counts: dict[str, int] = {}

    def best_header_record(self):
        """Return the best-known header record."""

        return self.node.headers.find_best_tip()

    def best_header_height(self) -> int | None:
        """Return the best-known header height."""

        record = self.best_header_record()
        return None if record is None else record.height

    def best_header_hash(self) -> str | None:
        """Return the best-known header hash."""

        record = self.best_header_record()
        return None if record is None else record.block_hash

    def missing_blocks_for_best_tip(self) -> tuple[str, ...]:
        """Return missing blocks for the strongest known header tip."""

        best_tip = self.best_header_record()
        if best_tip is None:
            return ()
        return self.missing_blocks_for_tip(best_tip.block_hash)

    def sync_status(self) -> dict[str, object]:
        """Return a runtime-friendly sync snapshot."""

        validated_tip = self.node.chain_tip()
        best_header = self.best_header_record()
        missing = self.missing_blocks_for_best_tip()
        queued_missing = tuple(block_hash for block_hash in missing if block_hash not in self._inflight_blocks)
        snapshot_anchor = self.node.snapshot_anchor()
        local_height = None if validated_tip is None else validated_tip.height
        remote_height = None if best_header is None else best_header.height
        if best_header is None:
            mode = "idle"
        elif missing:
            mode = "blocks" if self._inflight_blocks else "headers"
        else:
            mode = "synced"
        if snapshot_anchor is not None:
            if mode in {"headers", "blocks"}:
                phase = "syncing_post_anchor_delta"
            elif local_height == snapshot_anchor.height:
                phase = "snapshot_imported"
            else:
                phase = "synced"
        else:
            phase = "synced" if mode == "synced" else ("syncing_from_genesis" if mode in {"headers", "blocks"} else "idle")
        window_start_height = None
        window_end_height = None
        if missing:
            first_record = self.node.headers.get_record(missing[0])
            last_record = self.node.headers.get_record(missing[-1])
            window_start_height = None if first_record is None else first_record.height
            window_end_height = None if last_record is None else last_record.height
        stalled_peers = tuple(
            {"peer_id": peer_id, "stall_count": count}
            for peer_id, count in sorted(self._stalled_peer_counts.items())
            if count > 0
        )
        return {
            "mode": mode,
            "phase": phase,
            "local_height": local_height,
            "remote_height": remote_height,
            "validated_tip_height": None if validated_tip is None else validated_tip.height,
            "validated_tip_hash": None if validated_tip is None else getattr(validated_tip, "block_hash", None),
            "best_header_height": None if best_header is None else best_header.height,
            "best_header_hash": None if best_header is None else best_header.block_hash,
            "missing_block_count": len(missing),
            "queued_block_count": len(queued_missing),
            "inflight_block_count": len(self._inflight_blocks),
            "inflight_block_hashes": tuple(sorted(self._inflight_blocks)),
            "header_peer_count": len(self._header_peer_heights),
            "header_peers": tuple(sorted(self._header_peer_heights)),
            "block_peer_count": len(self._block_peer_counts),
            "block_peers": tuple(sorted(peer_id for peer_id, count in self._block_peer_counts.items() if count > 0)),
            "stalled_peers": stalled_peers,
            "download_window": {
                "start_height": window_start_height,
                "end_height": window_end_height,
                "size": len(missing),
            },
        }

    def ingest_headers(
        self,
        headers: tuple[BlockHeader, ...] | list[BlockHeader],
        *,
        peer_id: str | None = None,
    ) -> HeaderIngestResult:
        """Store headers, compute cumulative work, and report missing blocks."""

        stored_headers = 0
        parent_unknown = None
        previous_header_in_batch = None
        for header in headers:
            block_hash = header.block_hash()
            if self.node.headers.get_record(block_hash) is not None:
                previous_header_in_batch = header
                continue

            parent_hash = header.previous_block_hash
            if previous_header_in_batch is not None and parent_hash != previous_header_in_batch.block_hash():
                raise ContextualValidationError("Header sequence does not connect to the previous header.")
            parent_record = None if parent_hash == "00" * 32 else self.node.headers.get_record(parent_hash)
            if parent_hash != "00" * 32 and parent_record is None:
                parent_unknown = parent_hash
                break

            height = 0 if parent_record is None else int(parent_record.height) + 1
            self._validate_header_candidate(header, parent_record=parent_record, height=height)
            cumulative_work = 0 if parent_record is None or parent_record.cumulative_work is None else parent_record.cumulative_work
            cumulative_work += header_work(header)
            self.node.headers.put(
                header,
                height=height,
                cumulative_work=cumulative_work,
                is_main_chain=False,
            )
            stored_headers += 1
            previous_header_in_batch = header

        best_tip = self.node.headers.find_best_tip()
        best_tip_hash = None if best_tip is None else best_tip.block_hash
        best_tip_height = None if best_tip is None else best_tip.height
        if peer_id is not None and best_tip_height is not None:
            self._header_peer_heights[peer_id] = best_tip_height
            if best_tip_hash is not None:
                self._header_peer_tips[peer_id] = best_tip_hash
            self._header_peer_counts[peer_id] = self._header_peer_counts.get(peer_id, 0) + stored_headers
        missing = () if best_tip_hash is None else self.missing_blocks_for_tip(best_tip_hash)
        return HeaderIngestResult(
            headers_received=stored_headers,
            parent_unknown=parent_unknown,
            best_tip_hash=best_tip_hash,
            best_tip_height=best_tip_height,
            missing_block_hashes=missing,
            needs_more_headers=len(headers) >= self.max_headers,
        )

    def missing_blocks_for_tip(self, tip_hash: str) -> tuple[str, ...]:
        """Return missing blocks needed to activate a candidate chain tip."""

        path_hashes = self.node.headers.path_to_root(tip_hash)
        snapshot_anchor = None
        if hasattr(self.node, "snapshot_anchor"):
            snapshot_anchor = self.node.snapshot_anchor()
        if snapshot_anchor is not None:
            if snapshot_anchor.height >= len(path_hashes) or path_hashes[snapshot_anchor.height] != snapshot_anchor.block_hash:
                raise ValueError("snapshot anchor mismatch")
            path_hashes = path_hashes[snapshot_anchor.height + 1 :]
        return tuple(block_hash for block_hash in path_hashes if self.node.get_block_by_hash(block_hash) is None)

    def reserve_block_downloads(
        self,
        *,
        peer_ids: tuple[str, ...],
        max_window_size: int,
        max_inflight_per_peer: int,
        timeout_seconds: float,
        now: float,
    ) -> tuple[BlockDownloadAssignment, ...]:
        """Reserve one bounded batch of block download requests across peers."""

        if not peer_ids:
            return ()
        missing_blocks = self.missing_blocks_for_best_tip()
        if not missing_blocks:
            return ()
        max_window_size = max(1, max_window_size)
        max_inflight_per_peer = max(1, max_inflight_per_peer)
        window_hashes = missing_blocks[:max_window_size]
        inflight_by_peer: dict[str, int] = {peer_id: 0 for peer_id in peer_ids}
        for request in self._inflight_blocks.values():
            if request.peer_id in inflight_by_peer:
                inflight_by_peer[request.peer_id] += 1
        assignments: list[BlockDownloadAssignment] = []
        peer_cycle = list(peer_ids)
        for block_hash in window_hashes:
            if self.node.get_block_by_hash(block_hash) is not None:
                self._inflight_blocks.pop(block_hash, None)
                continue
            if block_hash in self._inflight_blocks:
                continue
            candidate_peers = sorted(
                peer_cycle,
                key=lambda peer_id: (
                    inflight_by_peer.get(peer_id, 0),
                    self._stalled_peer_counts.get(peer_id, 0),
                    peer_id,
                ),
            )
            selected_peer = None
            for peer_id in candidate_peers:
                if inflight_by_peer.get(peer_id, 0) < max_inflight_per_peer:
                    selected_peer = peer_id
                    break
            if selected_peer is None:
                break
            attempt = 1
            request = BlockRequestState(
                block_hash=block_hash,
                peer_id=selected_peer,
                requested_at=now,
                deadline_at=now + timeout_seconds,
                attempt=attempt,
            )
            self._inflight_blocks[block_hash] = request
            inflight_by_peer[selected_peer] = inflight_by_peer.get(selected_peer, 0) + 1
            self._block_peer_counts[selected_peer] = self._block_peer_counts.get(selected_peer, 0) + 1
            assignments.append(
                BlockDownloadAssignment(
                    block_hash=block_hash,
                    peer_id=selected_peer,
                    deadline_at=request.deadline_at,
                    attempt=attempt,
                )
            )
        return tuple(assignments)

    def expire_block_requests(self, *, now: float) -> tuple[BlockRequestState, ...]:
        """Expire overdue block requests so they can be reassigned."""

        expired: list[BlockRequestState] = []
        for block_hash, request in list(self._inflight_blocks.items()):
            if request.deadline_at > now:
                continue
            expired.append(request)
            self._inflight_blocks.pop(block_hash, None)
            self._stalled_peer_counts[request.peer_id] = self._stalled_peer_counts.get(request.peer_id, 0) + 1
        return tuple(expired)

    def release_peer_requests(self, peer_id: str) -> tuple[BlockRequestState, ...]:
        """Release in-flight requests assigned to one peer."""

        released: list[BlockRequestState] = []
        for block_hash, request in list(self._inflight_blocks.items()):
            if request.peer_id != peer_id:
                continue
            released.append(request)
            self._inflight_blocks.pop(block_hash, None)
        return tuple(released)

    def clear_block_request(self, block_hash: str) -> None:
        """Clear one in-flight block request after the block arrives."""

        self._inflight_blocks.pop(block_hash, None)

    def activate_best_chain_if_ready(self) -> SyncResult:
        """Activate the best-known chain when all blocks for it are available."""

        best_tip = self.node.headers.find_best_tip()
        current_tip = self.node.chain_tip()
        current_work = 0
        if current_tip is not None:
            current_record = self.node.headers.get_record(current_tip.block_hash)
            if current_record is not None and current_record.cumulative_work is not None:
                current_work = current_record.cumulative_work

        if best_tip is None or best_tip.cumulative_work is None or best_tip.cumulative_work <= current_work:
            return SyncResult(
                headers_received=0,
                blocks_fetched=0,
                activated_tip=current_tip.block_hash if current_tip is not None else None,
                reorged=False,
            )

        missing_blocks = self.missing_blocks_for_tip(best_tip.block_hash)
        if missing_blocks:
            return SyncResult(
                headers_received=0,
                blocks_fetched=0,
                activated_tip=current_tip.block_hash if current_tip is not None else None,
                parent_unknown=None,
                reorged=False,
            )

        previous_tip_hash = current_tip.block_hash if current_tip is not None else None
        reorged = False
        if previous_tip_hash is not None and best_tip.block_hash != previous_tip_hash:
            reorged = previous_tip_hash not in self.node.headers.path_to_root(best_tip.block_hash)
        activation = self.node.activate_chain(best_tip.block_hash)
        return SyncResult(
            headers_received=0,
            blocks_fetched=0,
            activated_tip=best_tip.block_hash,
            reorged=reorged,
            reorg_depth=activation.reorg_depth,
            old_tip=activation.old_tip,
            new_tip=activation.new_tip,
            common_ancestor=activation.common_ancestor,
            readded_transaction_count=activation.readded_transaction_count,
        )

    def receive_block(self, block: Block) -> BlockIngestResult:
        """Store a received block and activate the best chain when possible."""

        block_hash = block.block_hash()
        self.clear_block_request(block_hash)
        if self.node.get_block_by_hash(block_hash) is not None:
            activation = self.activate_best_chain_if_ready()
            accepted_blocks = self._accept_pending_children(block_hash)
            return BlockIngestResult(
                block_hash=block_hash,
                activated_tip=activation.activated_tip,
                reorged=activation.reorged,
                accepted_blocks=1 + accepted_blocks,
                reorg_depth=activation.reorg_depth,
                old_tip=activation.old_tip,
                new_tip=activation.new_tip,
                common_ancestor=activation.common_ancestor,
                readded_transaction_count=activation.readded_transaction_count,
            )

        parent_hash = block.header.previous_block_hash
        parent_record = None if parent_hash == "00" * 32 else self.node.headers.get_record(parent_hash)
        if parent_hash != "00" * 32 and parent_record is None:
            self._remember_orphan(block)
            return BlockIngestResult(
                block_hash=block_hash,
                activated_tip=self.node.chain_tip().block_hash if self.node.chain_tip() else None,
                reorged=False,
                parent_unknown=parent_hash,
                accepted_blocks=0,
            )

        header_record = self.node.headers.get_record(block_hash)
        if header_record is None:
            height = 0 if parent_record is None else int(parent_record.height) + 1
            cumulative_work = 0 if parent_record is None or parent_record.cumulative_work is None else parent_record.cumulative_work
            cumulative_work += header_work(block.header)
            self.node.headers.put(
                block.header,
                height=height,
                cumulative_work=cumulative_work,
                is_main_chain=False,
            )

        self.node.blocks.put(block)
        accepted_blocks = 1 + self._accept_pending_children(block_hash)
        activation = self.activate_best_chain_if_ready()
        return BlockIngestResult(
            block_hash=block_hash,
            activated_tip=activation.activated_tip,
            reorged=activation.reorged,
            accepted_blocks=accepted_blocks,
            reorg_depth=activation.reorg_depth,
            old_tip=activation.old_tip,
            new_tip=activation.new_tip,
            common_ancestor=activation.common_ancestor,
            readded_transaction_count=activation.readded_transaction_count,
        )

    def synchronize(self, peer) -> SyncResult:
        """Attempt to synchronize the local node with a remote peer service."""

        total_headers_received = 0
        last_ingest = HeaderIngestResult(
            headers_received=0,
            parent_unknown=None,
            best_tip_hash=None,
            best_tip_height=None,
            missing_block_hashes=(),
        )
        locator_hashes = self.node.build_block_locator()
        snapshot_anchor = None
        if hasattr(self.node, "snapshot_anchor"):
            snapshot_anchor = self.node.snapshot_anchor()
        while True:
            request = GetHeadersMessage(
                protocol_version=1,
                locator_hashes=locator_hashes,
                stop_hash="00" * 32,
            )
            response = peer.handle_getheaders(request, limit=self.max_headers)
            if (
                snapshot_anchor is not None
                and response.headers
                and response.headers[0].previous_block_hash not in locator_hashes
            ):
                raise ValueError("snapshot anchor mismatch")
            ingest = self.ingest_headers(response.headers)
            total_headers_received += ingest.headers_received
            last_ingest = ingest
            if ingest.parent_unknown is not None:
                break
            if not ingest.needs_more_headers:
                break
            if response.headers:
                locator_hashes = (response.headers[-1].block_hash(),)
            else:
                break

        blocks_fetched = 0
        reorged = False
        activated_tip = None
        reorg_depth = 0
        old_tip = None
        new_tip = None
        common_ancestor = None
        readded_transaction_count = 0
        for block_hash in last_ingest.missing_block_hashes:
            block = peer.get_block_by_hash(block_hash)
            if block is None:
                raise ValueError(f"Peer did not provide required block: {block_hash}")
            block_result = self.receive_block(block)
            blocks_fetched += block_result.accepted_blocks
            reorged = reorged or block_result.reorged
            activated_tip = block_result.activated_tip
            if block_result.reorged:
                reorg_depth = block_result.reorg_depth
                old_tip = block_result.old_tip
                new_tip = block_result.new_tip
                common_ancestor = block_result.common_ancestor
                readded_transaction_count = block_result.readded_transaction_count

        activation = self.activate_best_chain_if_ready()
        return SyncResult(
            headers_received=total_headers_received,
            blocks_fetched=blocks_fetched,
            activated_tip=activation.activated_tip if activation.activated_tip is not None else activated_tip,
            parent_unknown=last_ingest.parent_unknown,
            reorged=reorged or activation.reorged,
            reorg_depth=activation.reorg_depth if activation.reorged else reorg_depth,
            old_tip=activation.old_tip if activation.reorged else old_tip,
            new_tip=activation.new_tip if activation.reorged else new_tip,
            common_ancestor=activation.common_ancestor if activation.reorged else common_ancestor,
            readded_transaction_count=(
                activation.readded_transaction_count if activation.reorged else readded_transaction_count
            ),
        )

    def _remember_orphan(self, block: Block) -> None:
        """Store an orphan block until its parent becomes known."""

        parent_hash = block.header.previous_block_hash
        bucket = self._orphan_blocks.setdefault(parent_hash, {})
        bucket[block.block_hash()] = block
        self._orphan_blocks.move_to_end(parent_hash)
        while len(self._orphan_blocks) > self._MAX_ORPHAN_BLOCKS:
            self._orphan_blocks.popitem(last=False)

    def _accept_pending_children(self, parent_hash: str) -> int:
        """Recursively accept orphan blocks whose parent just became known."""

        pending = self._orphan_blocks.pop(parent_hash, {})
        accepted = 0
        for child_hash in sorted(pending):
            child = pending[child_hash]
            result = self.receive_block(child)
            accepted += result.accepted_blocks
        return accepted

    def _validate_header_candidate(self, header: BlockHeader, *, parent_record, height: int) -> None:
        """Perform header-only checks before block download."""

        if header.timestamp < 0:
            raise StatelessValidationError("Header timestamp cannot be negative.")
        if not verify_proof_of_work(header):
            raise StatelessValidationError("Header proof of work is invalid.")
        if parent_record is not None:
            if header.previous_block_hash != parent_record.block_hash:
                raise ContextualValidationError("Header does not connect to the expected parent.")
            if header.timestamp < parent_record.header.timestamp:
                raise ContextualValidationError("Header timestamp is below the previous header timestamp.")
        expected_bits = self._expected_bits_for_candidate_header(parent_record=parent_record, height=height)
        if header.bits != expected_bits:
            raise ContextualValidationError("Header bits do not match expected difficulty target.")

    def _expected_bits_for_candidate_header(self, *, parent_record, height: int) -> int:
        """Return the expected bits for one candidate header."""

        if height <= 0 or parent_record is None:
            return self.node.params.genesis_bits
        previous_header = parent_record.header
        if height % self.node.params.difficulty_adjustment_window != 0:
            return previous_header.bits
        path_hashes = self.node.headers.path_to_root(parent_record.block_hash)
        window_start_height = max(0, height - self.node.params.difficulty_adjustment_window)
        first_header_hash = path_hashes[window_start_height]
        first_header = self.node.headers.get(first_header_hash)
        if first_header is None:
            return previous_header.bits
        actual_timespan_seconds = max(1, previous_header.timestamp - first_header.timestamp)
        return calculate_next_work_required(
            previous_bits=previous_header.bits,
            actual_timespan_seconds=actual_timespan_seconds,
            params=self.node.params,
        )
