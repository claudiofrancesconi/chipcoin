"""Header-first synchronization planning and orchestration."""

from __future__ import annotations

from dataclasses import dataclass

from ..consensus.models import Block, BlockHeader
from ..consensus.pow import header_work
from .messages import GetHeadersMessage


@dataclass(frozen=True)
class HeaderIngestResult:
    """Result of ingesting one or more headers from a peer."""

    headers_received: int
    parent_unknown: str | None
    best_tip_hash: str | None
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


class SyncManager:
    """Coordinate header download, block fetch, and chain advancement."""

    def __init__(self, *, node, max_headers: int = 2000) -> None:
        self.node = node
        self.max_headers = max_headers
        self._orphan_blocks: dict[str, dict[str, Block]] = {}

    def ingest_headers(self, headers: tuple[BlockHeader, ...] | list[BlockHeader]) -> HeaderIngestResult:
        """Store headers, compute cumulative work, and report missing blocks."""

        stored_headers = 0
        parent_unknown = None
        for header in headers:
            block_hash = header.block_hash()
            if self.node.headers.get_record(block_hash) is not None:
                continue

            parent_hash = header.previous_block_hash
            parent_record = None if parent_hash == "00" * 32 else self.node.headers.get_record(parent_hash)
            if parent_hash != "00" * 32 and parent_record is None:
                parent_unknown = parent_hash
                break

            height = 0 if parent_record is None else int(parent_record.height) + 1
            cumulative_work = 0 if parent_record is None or parent_record.cumulative_work is None else parent_record.cumulative_work
            cumulative_work += header_work(header)
            self.node.headers.put(
                header,
                height=height,
                cumulative_work=cumulative_work,
                is_main_chain=False,
            )
            stored_headers += 1

        best_tip = self.node.headers.find_best_tip()
        best_tip_hash = None if best_tip is None else best_tip.block_hash
        missing = () if best_tip_hash is None else self.missing_blocks_for_tip(best_tip_hash)
        return HeaderIngestResult(
            headers_received=stored_headers,
            parent_unknown=parent_unknown,
            best_tip_hash=best_tip_hash,
            missing_block_hashes=missing,
            needs_more_headers=len(headers) >= self.max_headers,
        )

    def missing_blocks_for_tip(self, tip_hash: str) -> tuple[str, ...]:
        """Return missing blocks needed to activate a candidate chain tip."""

        path_hashes = self.node.headers.path_to_root(tip_hash)
        return tuple(block_hash for block_hash in path_hashes if self.node.get_block_by_hash(block_hash) is None)

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
        last_ingest = HeaderIngestResult(headers_received=0, parent_unknown=None, best_tip_hash=None, missing_block_hashes=())
        locator_hashes = self.node.build_block_locator()
        while True:
            request = GetHeadersMessage(
                protocol_version=1,
                locator_hashes=locator_hashes,
                stop_hash="00" * 32,
            )
            response = peer.handle_getheaders(request, limit=self.max_headers)
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

    def _accept_pending_children(self, parent_hash: str) -> int:
        """Recursively accept orphan blocks whose parent just became known."""

        pending = self._orphan_blocks.pop(parent_hash, {})
        accepted = 0
        for child_hash in sorted(pending):
            child = pending[child_hash]
            result = self.receive_block(child)
            accepted += result.accepted_blocks
        return accepted
