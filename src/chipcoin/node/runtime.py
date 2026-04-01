"""Persistent P2P node runtime."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import secrets
import socket
from dataclasses import dataclass, field

from ..config import get_network_config
from ..consensus.validation import ValidationError
from ..utils.logging import configure_logging
from .p2p.errors import (
    DuplicateConnectionError,
    HandshakeFailedError,
    InvalidBlockError,
    InvalidTxError,
    ProtocolError,
    protocol_error_class,
)
from .peers import classify_peer_error
from .messages import (
    AddrMessage,
    EmptyPayload,
    GetBlocksMessage,
    GetDataMessage,
    GetHeadersMessage,
    InvMessage,
    InventoryVector,
    MessageEnvelope,
    PeerAddress,
    BlockMessage,
    TransactionMessage,
)
from .p2p.protocol import LocalPeerIdentity, PeerProtocol
from .p2p.transport import PeerEndpoint, TCPTransport
from .sync import SyncManager


@dataclass(frozen=True)
class OutboundPeer:
    """Configured outbound peer endpoint."""

    host: str
    port: int


@dataclass
class SessionHandle:
    """Tracked peer session metadata."""

    protocol: PeerProtocol
    outbound: bool
    endpoint: OutboundPeer | None = None
    announced_inventory_counts: dict[tuple[str, str], int] = field(default_factory=dict)
    consecutive_ping_failures: int = 0
    last_activity_at: float = 0.0
    sync_start_height: int | None = None
    sync_target_height: int | None = None
    sync_next_log_height: int | None = None


class NodeRuntime:
    """Long-running TCP runtime coordinating peer sessions and sync."""

    _SYNC_PROGRESS_LOG_INTERVAL = 100

    def __init__(
        self,
        *,
        service,
        listen_host: str = "127.0.0.1",
        listen_port: int = 8333,
        outbound_peers: list[OutboundPeer] | None = None,
        connect_interval: float = 1.0,
        ping_interval: float = 2.0,
        read_timeout: float = 15.0,
        write_timeout: float = 15.0,
        handshake_timeout: float = 5.0,
        miner_address: str | None = None,
        mining_nonce_batch_size: int = 50_000,
        mining_idle_interval: float = 0.05,
        mining_min_interval_seconds: float = 0.0,
        mempool_relay_interval: float = 0.25,
        max_connect_backoff_seconds: float = 30.0,
        max_consecutive_ping_failures: int = 3,
        max_inventory_items: int = 500,
        max_addr_records: int = 1000,
        max_headers_per_message: int = 2000,
        duplicate_inventory_limit: int = 20,
        logger: logging.Logger | None = None,
    ) -> None:
        self.service = service
        self.listen_host = listen_host
        self.listen_port = listen_port
        self.connect_interval = connect_interval
        self.read_timeout = read_timeout
        self.write_timeout = write_timeout
        self.handshake_timeout = handshake_timeout
        self.ping_interval = ping_interval if ping_interval < read_timeout else max(0.5, read_timeout / 2)
        self.miner_address = miner_address
        self.mining_nonce_batch_size = mining_nonce_batch_size
        self.mining_idle_interval = mining_idle_interval
        self.mining_min_interval_seconds = max(0.0, mining_min_interval_seconds)
        self.mempool_relay_interval = max(0.05, mempool_relay_interval)
        self.max_connect_backoff_seconds = max(1.0, max_connect_backoff_seconds)
        self.max_consecutive_ping_failures = max(1, max_consecutive_ping_failures)
        self.max_inventory_items = max_inventory_items
        self.max_addr_records = max_addr_records
        self.max_headers_per_message = max_headers_per_message
        self.duplicate_inventory_limit = duplicate_inventory_limit
        self.logger = logger or logging.getLogger("chipcoin.node.runtime")
        self.sync_manager = SyncManager(node=service)
        self.node_id = secrets.token_hex(16)

        self._server: asyncio.AbstractServer | None = None
        self._running = False
        self._stop_event = asyncio.Event()
        self._tasks: set[asyncio.Task] = set()
        self._sessions: dict[PeerProtocol, SessionHandle] = {}
        self._sessions_by_node_id: dict[str, PeerProtocol] = {}
        self._outbound_targets: dict[tuple[str, int], OutboundPeer] = {
            (peer.host, peer.port): peer for peer in (outbound_peers or [])
        }
        self._mining_template_key: tuple[str | None, tuple[str, ...]] | None = None
        self._mining_nonce_cursor = 0
        self._mining_template = None
        self._last_mined_monotonic: float | None = None
        self._mining_wait_logged = False
        self._initial_sync_required = False
        self._relayed_mempool_txids: set[str] = set()

    @property
    def bound_port(self) -> int:
        """Return the active bound port once the server is running."""

        if self._server is None or not self._server.sockets:
            return self.listen_port
        return int(self._server.sockets[0].getsockname()[1])

    async def start(self) -> None:
        """Start the runtime listener and background loops."""

        if self._running:
            return
        configure_logging()
        self._server = await asyncio.start_server(self._handle_inbound_connection, self.listen_host, self.listen_port)
        self._running = True
        self.logger.info(
            "runtime started network=%s listen=%s:%s miner=%s outbound_targets=%s ping_interval=%s read_timeout=%s",
            self.service.network,
            self.listen_host,
            self.bound_port,
            self.miner_address,
            len(self._outbound_targets),
            self.ping_interval,
            self.read_timeout,
        )
        self._purge_persisted_self_aliases()
        self._purge_undialable_persisted_peers()
        self._purge_persisted_startup_duplicate_aliases()
        self._initial_sync_required = self.miner_address is not None and bool(self._desired_outbound_peers())
        self._stop_event.clear()
        self._spawn_task(self._connect_loop(), "connect-loop")
        self._spawn_task(self._ping_loop(), "ping-loop")
        self._spawn_task(self._mempool_relay_loop(), "mempool-relay-loop")
        if self.miner_address is not None:
            self._spawn_task(self._mining_loop(), "mining-loop")

    async def stop(self) -> None:
        """Stop listener, background tasks, and active sessions."""

        if not self._running:
            return
        self.logger.info(
            "runtime stopping network=%s active_sessions=%s background_tasks=%s",
            self.service.network,
            len(self._sessions),
            len(self._tasks),
        )
        self._running = False
        if self._server is not None:
            self._server.close()
            try:
                await asyncio.wait_for(
                    self._server.wait_closed(),
                    timeout=max(1.0, self.read_timeout),
                )
            except TimeoutError:
                self.logger.warning("runtime stop timed out waiting for listener shutdown")
            self._server = None
        sessions = list(self._sessions.keys())
        self.logger.info("runtime stopping close_sessions count=%s", len(sessions))
        for session in sessions:
            await session.close(reason="Runtime stopping.")
        tasks = list(self._tasks)
        self.logger.info("runtime stopping cancel_tasks count=%s", len(tasks))
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._tasks.clear()
        self._sessions.clear()
        self._sessions_by_node_id.clear()
        self._relayed_mempool_txids.clear()
        self._stop_event.set()
        self.logger.info("runtime stopped network=%s", self.service.network)

    async def run_forever(self) -> None:
        """Run the runtime until cancelled."""

        await self.start()
        try:
            await self._stop_event.wait()
        except asyncio.CancelledError:
            await self.stop()
            raise

    async def submit_transaction(self, transaction) -> None:
        """Accept a local transaction and relay its inventory to peers."""

        accepted = self.service.receive_transaction(transaction)
        self.logger.info("local tx accepted txid=%s fee_chipbits=%s", accepted.transaction.txid(), accepted.fee)
        self._invalidate_mining_template()
        self._relayed_mempool_txids.add(accepted.transaction.txid())
        await self._broadcast_inventory(InventoryVector(object_type="tx", object_hash=accepted.transaction.txid()))

    async def announce_block(self, block) -> None:
        """Apply a local block and relay its inventory to peers."""

        self.service.apply_block(block)
        self.logger.info("local block applied height=%s hash=%s", self.service.chain_tip().height, block.block_hash())
        self._invalidate_mining_template()
        await self._broadcast_inventory(InventoryVector(object_type="block", object_hash=block.block_hash()))

    def connected_peer_count(self) -> int:
        """Return the number of active handshaken peer sessions."""

        return sum(1 for session in self._sessions if session.state.handshake_complete and not session.state.closed)

    async def _connect_loop(self) -> None:
        """Keep outbound peer connections alive."""

        while self._running:
            for peer in list(self._desired_outbound_peers()):
                if self._has_active_endpoint(peer):
                    continue
                if self._is_backoff_active(peer):
                    info = self._known_peer_info(peer.host, peer.port)
                    if info is not None and info.backoff_until is not None:
                        self.logger.debug(
                            "outbound connect deferred peer=%s:%s reconnect_attempts=%s backoff_until=%s",
                            peer.host,
                            peer.port,
                            info.reconnect_attempts,
                            info.backoff_until,
                        )
                    continue
                try:
                    await self._connect_outbound(peer)
                except Exception as exc:
                    self.logger.info("outbound connect failed peer=%s:%s error=%s", peer.host, peer.port, exc)
                    self._register_peer_failure(peer, error=exc, penalty=20)
            await asyncio.sleep(self.connect_interval)

    async def _ping_loop(self) -> None:
        """Periodically ping active peers and clean up dead sessions."""

        while self._running:
            sessions = [session for session in self._sessions if session.state.handshake_complete and not session.state.closed]
            for session in sessions:
                handle = self._sessions.get(session)
                if handle is None:
                    continue
                try:
                    await session.ping(secrets.randbits(64), timeout=self.read_timeout)
                    handle.consecutive_ping_failures = 0
                except Exception as exc:
                    if self._session_has_recent_activity(session):
                        handle.consecutive_ping_failures = 0
                        self.logger.debug(
                            "ping timeout ignored for recently active peer=%s error=%s",
                            self._format_peer_for_logs(session),
                            exc,
                        )
                        continue
                    handle.consecutive_ping_failures += 1
                    self.logger.info(
                        "ping failed peer=%s failure_count=%s/%s error=%s",
                        self._format_peer_for_logs(session),
                        handle.consecutive_ping_failures,
                        self.max_consecutive_ping_failures,
                        exc,
                    )
                    if handle.consecutive_ping_failures < self.max_consecutive_ping_failures:
                        continue
                    self._apply_session_penalty(session, error=exc, penalty=10)
                    await session.close(reason=str(exc), error=exc)
                    await self._drop_session(session)
            await asyncio.sleep(self.ping_interval)

    async def _handle_inbound_connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Accept and start an inbound peer session."""

        transport = TCPTransport(reader, writer, read_timeout=self.read_timeout, write_timeout=self.write_timeout)
        endpoint = transport.peer_endpoint()
        self.logger.info("inbound connection accepted peer=%s:%s", endpoint.host, endpoint.port)
        session = PeerProtocol(
            transport=transport,
            identity=self._local_identity(),
            inbound=True,
            handshake_timeout=self.handshake_timeout,
            on_message=self._on_peer_message,
            on_handshake_complete=self._on_handshake_complete,
        )
        self._sessions[session] = SessionHandle(protocol=session, outbound=False)
        self._spawn_task(self._run_session(session), "inbound-session")

    async def _connect_outbound(self, peer: OutboundPeer) -> None:
        """Establish an outbound connection to a configured peer."""

        self.logger.info("attempting outbound connect peer=%s:%s", peer.host, peer.port)
        transport = await TCPTransport.connect(
            peer.host,
            peer.port,
            connect_timeout=self.read_timeout,
            read_timeout=self.read_timeout,
            write_timeout=self.write_timeout,
        )
        session = PeerProtocol(
            transport=transport,
            identity=self._local_identity(),
            inbound=False,
            handshake_timeout=self.handshake_timeout,
            on_message=self._on_peer_message,
            on_handshake_complete=self._on_handshake_complete,
        )
        self._sessions[session] = SessionHandle(protocol=session, outbound=True, endpoint=peer)
        self.logger.info("outbound TCP connected peer=%s:%s", peer.host, peer.port)
        self._spawn_task(self._run_session(session), f"outbound-{peer.host}:{peer.port}")

    async def _run_session(self, session: PeerProtocol) -> None:
        """Run a peer session until it ends."""

        try:
            await session.start()
            await session.wait_closed()
        except Exception as exc:
            self.logger.info("session failed handshake_complete=%s error=%s", session.state.handshake_complete, exc)
            self._apply_session_penalty(session, error=exc, penalty=20 if not session.state.handshake_complete else 10)
            await session.close(reason=str(exc), error=exc)
        finally:
            await self._drop_session(session)

    async def _on_handshake_complete(self, session: PeerProtocol) -> None:
        """Register a peer after handshake and begin initial sync."""

        try:
            remote = session.state.remote_version
            if remote is None:
                return
            if remote.node_id == self.node_id:
                self._forget_self_alias(session)
                error = DuplicateConnectionError("Refusing self-connection.")
                await session.close(reason=str(error), error=error)
                return

            existing = self._sessions_by_node_id.get(remote.node_id)
            if existing is not None and existing is not session:
                self.logger.info("duplicate peer connection rejected node_id=%s", remote.node_id)
                error = DuplicateConnectionError("Duplicate peer connection.")
                await session.close(reason=str(error), error=error)
                return

            self._sessions_by_node_id[remote.node_id] = session
            handle = self._sessions.get(session)
            if handle is not None and handle.endpoint is not None:
                self.service.add_peer(handle.endpoint.host, handle.endpoint.port)
            endpoint = self._session_endpoint(session, handle)
            if endpoint is not None:
                self.service.record_peer_observation(
                    host=endpoint.host,
                    port=endpoint.port,
                    direction="inbound" if session.inbound else "outbound",
                    handshake_complete=True,
                    last_known_height=remote.start_height,
                    node_id=remote.node_id,
                    score=self._updated_peer_score(endpoint.host, endpoint.port, delta=1),
                    reconnect_attempts=0,
                    backoff_until=0,
                    last_error=None,
                    protocol_error_class=None,
                    session_started_at=self.service.time_provider(),
                )
                self._canonicalize_peer_aliases(
                    remote.node_id,
                    canonical_host=endpoint.host,
                    canonical_port=endpoint.port,
                    prefer_configured=handle.endpoint if handle is not None else None,
                )
            self.logger.info(
                "peer handshake complete node_id=%s direction=%s height=%s",
                remote.node_id,
                "inbound" if session.inbound else "outbound",
                remote.start_height,
            )
            self.logger.info(
                "sync start peer=%s node_id=%s remote_height=%s local_height=%s",
                self._format_peer_for_logs(session),
                remote.node_id,
                remote.start_height,
                0 if self.service.chain_tip() is None else self.service.chain_tip().height,
            )
            self._begin_sync_tracking(session, remote.start_height)
            await self._request_headers(session)
            await self._send_known_peers(session)
            await self._announce_current_mempool(session)
        except Exception as exc:
            self.logger.info("handshake follow-up failed error=%s", exc)
            await session.close(reason=str(exc))
            await self._drop_session(session)

    async def _on_peer_message(self, session: PeerProtocol, message: MessageEnvelope) -> None:
        """Handle application-level peer protocol messages."""

        self._mark_session_activity(session)

        if message.command == "getheaders":
            await session.send_message(MessageEnvelope(command="headers", payload=self.service.handle_getheaders(message.payload)))
            return

        if message.command == "headers":
            if len(message.payload.headers) > self.max_headers_per_message:
                error = ProtocolError("headers message exceeded limit")
                self._apply_session_penalty(session, error=error, penalty=15)
                await session.close(reason=str(error), error=error)
                await self._drop_session(session)
                return
            ingest = self.sync_manager.ingest_headers(message.payload.headers)
            if ingest.headers_received > 0 or ingest.parent_unknown is not None or ingest.missing_block_hashes:
                self.logger.info(
                    "headers received peer=%s count=%s stored=%s missing_blocks=%s best_tip=%s continue=%s",
                    self._format_peer_for_logs(session),
                    len(message.payload.headers),
                    ingest.headers_received,
                    len(ingest.missing_block_hashes),
                    ingest.best_tip_hash,
                    ingest.needs_more_headers,
                )
            else:
                self.logger.debug(
                    "headers received peer=%s count=%s stored=%s missing_blocks=%s best_tip=%s continue=%s",
                    self._format_peer_for_logs(session),
                    len(message.payload.headers),
                    ingest.headers_received,
                    len(ingest.missing_block_hashes),
                    ingest.best_tip_hash,
                    ingest.needs_more_headers,
                )
            if ingest.parent_unknown is not None:
                self.logger.info(
                    "sync parent unknown peer=%s parent=%s",
                    self._format_peer_for_logs(session),
                    ingest.parent_unknown,
                )
                await self._request_headers(session)
                return
            if ingest.missing_block_hashes:
                self._begin_sync_tracking(session, self._sync_target_height(session))
                self._log_sync_progress(session, force=True)
                self.logger.info(
                    "sync requesting blocks peer=%s count=%s first=%s last=%s",
                    self._format_peer_for_logs(session),
                    len(ingest.missing_block_hashes),
                    ingest.missing_block_hashes[0],
                    ingest.missing_block_hashes[-1],
                )
                for start in range(0, len(ingest.missing_block_hashes), self.max_inventory_items):
                    batch = ingest.missing_block_hashes[start : start + self.max_inventory_items]
                    await session.send_message(
                        MessageEnvelope(
                            command="getdata",
                            payload=GetDataMessage(
                                items=tuple(
                                    InventoryVector(object_type="block", object_hash=block_hash)
                                    for block_hash in batch
                                )
                            ),
                        )
                    )
            else:
                self.sync_manager.activate_best_chain_if_ready()
                self._log_sync_progress(session, force=True)
            if ingest.needs_more_headers:
                next_locator = None if not message.payload.headers else (message.payload.headers[-1].block_hash(),)
                await self._request_headers(session, locator_hashes=next_locator)
            return

        if message.command == "getblocks":
            await session.send_message(MessageEnvelope(command="inv", payload=self.service.handle_getblocks(message.payload)))
            return

        if message.command == "inv":
            if len(message.payload.items) > self.max_inventory_items:
                error = ProtocolError("inventory message exceeded limit")
                self._apply_session_penalty(session, error=error, penalty=15)
                await session.close(reason=str(error), error=error)
                await self._drop_session(session)
                return
            needed: list[InventoryVector] = []
            for item in message.payload.items:
                if self._is_duplicate_inventory(session, item):
                    self._apply_session_penalty(session, error=ProtocolError("duplicate inventory announcements"), penalty=1)
                    continue
                if item.object_type == "block":
                    if self.service.headers.get_record(item.object_hash) is None and self.service.get_block_by_hash(item.object_hash) is None:
                        needed.append(item)
                elif item.object_type == "tx":
                    if self.service.get_transaction(item.object_hash) is None:
                        needed.append(item)
            if needed:
                await session.send_message(MessageEnvelope(command="getdata", payload=GetDataMessage(items=tuple(needed))))
            return

        if message.command == "getdata":
            if len(message.payload.items) > self.max_inventory_items:
                error = ProtocolError("getdata message exceeded limit")
                self._apply_session_penalty(session, error=error, penalty=10)
                await session.close(reason=str(error), error=error)
                await self._drop_session(session)
                return
            for item in message.payload.items:
                if item.object_type == "block":
                    block = self.service.get_block_by_hash(item.object_hash)
                    if block is not None:
                        await session.send_message(MessageEnvelope(command="block", payload=BlockMessage(block=block)))
                elif item.object_type == "tx":
                    transaction = self.service.get_transaction(item.object_hash)
                    if transaction is not None:
                        await session.send_message(MessageEnvelope(command="tx", payload=TransactionMessage(transaction=transaction)))
            return

        if message.command == "block":
            try:
                result = self.sync_manager.receive_block(message.payload.block)
            except ValidationError as exc:
                self.logger.debug("peer sent invalid block: %s", exc)
                typed_error = InvalidBlockError(f"invalid block: {exc}")
                self._apply_session_penalty(session, error=typed_error, penalty=25)
                await session.close(reason=str(typed_error), error=typed_error)
                await self._drop_session(session)
                return
            if result.parent_unknown is not None:
                self.logger.info(
                    "sync orphan block peer=%s block=%s parent=%s",
                    self._format_peer_for_logs(session),
                    result.block_hash,
                    result.parent_unknown,
                )
                await self._request_headers(session)
                return
            if result.reorged:
                self.logger.info(
                    "reorg start peer=%s old_tip=%s new_tip=%s common_ancestor=%s depth=%s",
                    self._format_peer_for_logs(session),
                    result.old_tip,
                    result.new_tip,
                    result.common_ancestor,
                    result.reorg_depth,
                )
                self._log_block_application(session, result, reorged=True)
                self.logger.info(
                    "mempool reconciled after reorg readded_transactions=%s",
                    result.readded_transaction_count,
                )
            else:
                self._log_block_application(session, result, reorged=False)
            self._invalidate_mining_template()
            await self._broadcast_inventory(
                InventoryVector(object_type="block", object_hash=result.block_hash),
                exclude=session,
            )
            return

        if message.command == "tx":
            try:
                accepted = self.service.receive_transaction(message.payload.transaction)
            except ValidationError as exc:
                self._apply_session_penalty(session, error=InvalidTxError(f"invalid tx: {exc}"), penalty=5)
                return
            self.logger.info(
                "tx accepted from peer txid=%s fee_chipbits=%s",
                accepted.transaction.txid(),
                accepted.fee,
            )
            self._invalidate_mining_template()
            self._relayed_mempool_txids.add(accepted.transaction.txid())
            await self._broadcast_inventory(
                InventoryVector(object_type="tx", object_hash=accepted.transaction.txid()),
                exclude=session,
            )
            return

        if message.command == "getaddr":
            await self._send_known_peers(session)
            return

        if message.command == "addr":
            if len(message.payload.addresses) > self.max_addr_records:
                error = ProtocolError("addr message exceeded limit")
                self._apply_session_penalty(session, error=error, penalty=10)
                await session.close(reason=str(error), error=error)
                await self._drop_session(session)
                return
            for address in message.payload.addresses:
                announced = OutboundPeer(address.host, address.port)
                if self._is_local_listener_alias(announced):
                    continue
                if not self._is_announced_peer_dialable(announced):
                    continue
                if self._is_known_peer_alias(announced):
                    continue
                self._outbound_targets[(address.host, address.port)] = announced
                self.service.add_peer(address.host, address.port)
            self.logger.debug("peer announced addresses count=%s", len(message.payload.addresses))
            return

    async def _broadcast_inventory(self, item: InventoryVector, *, exclude: PeerProtocol | None = None) -> None:
        """Broadcast a single inventory announcement to active peers."""

        await self._broadcast_inventory_items((item,), exclude=exclude)

    async def _broadcast_inventory_items(
        self,
        items: tuple[InventoryVector, ...],
        *,
        exclude: PeerProtocol | None = None,
    ) -> None:
        """Broadcast one inventory message to active peers."""

        if not items:
            return
        message = MessageEnvelope(command="inv", payload=InvMessage(items=items))
        for session in list(self._sessions):
            if session is exclude or session.state.closed or not session.state.handshake_complete:
                continue
            try:
                await session.send_message(message)
            except Exception:
                error = ProtocolError("broadcast failed")
                await session.close(reason=str(error), error=error)
                await self._drop_session(session)

    async def _announce_current_mempool(self, session: PeerProtocol) -> None:
        """Announce currently staged mempool transactions to one peer."""

        entries = self.service.mempool.list_transactions()
        if not entries:
            return
        items = tuple(
            InventoryVector(object_type="tx", object_hash=entry.transaction.txid())
            for entry in entries[: self.max_inventory_items]
        )
        await session.send_message(MessageEnvelope(command="inv", payload=InvMessage(items=items)))

    async def _mempool_relay_loop(self) -> None:
        """Relay transactions that appeared in the local shared mempool repository."""

        while self._running:
            try:
                current_txids = {transaction.txid() for transaction in self.service.list_mempool_transactions()}
                unseen_txids = sorted(current_txids - self._relayed_mempool_txids)
                if unseen_txids:
                    for txid in unseen_txids:
                        await self._broadcast_inventory(InventoryVector(object_type="tx", object_hash=txid))
                    self._relayed_mempool_txids.update(unseen_txids)
                self._relayed_mempool_txids.intersection_update(current_txids)
            except Exception as exc:
                self.logger.debug("mempool relay loop failed: %s", exc)
            await asyncio.sleep(self.mempool_relay_interval)

    async def _request_headers(self, session: PeerProtocol, *, locator_hashes: tuple[str, ...] | None = None) -> None:
        """Request headers from a peer using the local block locator."""

        await session.send_message(
            MessageEnvelope(
                command="getheaders",
                payload=GetHeadersMessage(
                    protocol_version=1,
                    locator_hashes=self.service.build_block_locator() if locator_hashes is None else locator_hashes,
                    stop_hash="00" * 32,
                ),
            )
        )
        self.logger.debug(
            "sync request headers peer=%s locator_count=%s",
            self._format_peer_for_logs(session),
            len(self.service.build_block_locator() if locator_hashes is None else locator_hashes),
        )

    async def _send_known_peers(self, session: PeerProtocol) -> None:
        """Send known peer addresses to a remote session."""

        remote = session.state.remote_version
        addresses = tuple(
            PeerAddress(host=peer.host, port=peer.port, services=0, timestamp=self.service.time_provider())
            for peer in self.service.list_peers()
            if self._is_advertisable_peer(peer)
            and peer.node_id != self.node_id
            and (remote is None or peer.node_id != remote.node_id)
        )
        await session.send_message(MessageEnvelope(command="addr", payload=AddrMessage(addresses=addresses)))
        self.logger.debug("sent addr records count=%s", len(addresses))

    async def _drop_session(self, session: PeerProtocol) -> None:
        """Remove a session from runtime tracking."""

        handle = self._sessions.pop(session, None)
        remote = session.state.remote_version
        endpoint = self._session_endpoint(session, handle)
        if endpoint is not None:
            existing = self._known_peer_info(endpoint.host, endpoint.port)
            current_error = None if not session.state.errors else session.state.errors[-1]
            current_error_obj = None if not session.state.error_causes else session.state.error_causes[-1]
            penalty = 0 if current_error is None else self._penalty_for_error(current_error_obj or current_error)
            outbound_pre_handshake = handle is not None and handle.outbound and not session.state.handshake_complete
            if outbound_pre_handshake:
                reconnect_attempts, backoff_until = self._next_backoff_state(existing)
            else:
                reconnect_attempts = None if existing is None else existing.reconnect_attempts
                backoff_until = None if existing is None else existing.backoff_until
            score = None
            if penalty > 0 and not (existing is not None and existing.last_error == current_error):
                score = self._updated_peer_score(endpoint.host, endpoint.port, delta=-penalty)
            self.service.record_peer_observation(
                host=endpoint.host,
                port=endpoint.port,
                direction=None if handle is None else ("outbound" if handle.outbound else "inbound"),
                handshake_complete=False,
                last_known_height=None if remote is None else remote.start_height,
                node_id=None if remote is None else remote.node_id,
                score=score,
                reconnect_attempts=reconnect_attempts,
                backoff_until=backoff_until,
                last_error=current_error,
                last_error_at=self.service.time_provider() if current_error is not None else None,
                protocol_error_class=classify_peer_error(current_error_obj or current_error),
                disconnect_count=0 if existing is None or existing.disconnect_count is None else existing.disconnect_count + 1,
            )
            self.logger.info(
                "session dropped peer=%s:%s handshake_complete=%s error=%s disconnects=%s",
                endpoint.host,
                endpoint.port,
                session.state.handshake_complete,
                current_error,
                0 if existing is None or existing.disconnect_count is None else existing.disconnect_count + 1,
            )
        if handle is None:
            return
        if remote is not None and self._sessions_by_node_id.get(remote.node_id) is session:
            del self._sessions_by_node_id[remote.node_id]

    def _has_active_endpoint(self, peer: OutboundPeer) -> bool:
        """Return whether an active outbound session already targets the endpoint."""

        known = self._known_peer_info(peer.host, peer.port)
        known_node_id = None if known is None else known.node_id
        for protocol, handle in self._sessions.items():
            if protocol.state.closed:
                continue
            endpoint = self._session_endpoint(protocol, handle)
            if endpoint is not None and endpoint.host == peer.host and endpoint.port == peer.port:
                return True
            if endpoint is not None and self._peers_equivalent(endpoint, peer):
                return True
            remote = protocol.state.remote_version
            if (
                known_node_id is not None
                and remote is not None
                and remote.node_id == known_node_id
                and protocol.state.handshake_complete
            ):
                return True
        return False

    def _desired_outbound_peers(self) -> list[OutboundPeer]:
        """Return configured and persisted peers excluding the local listener."""

        peers = set(self._outbound_targets.values())
        for peer in self.service.list_peers():
            if not self._is_dialable_peer(peer):
                continue
            peers.add(OutboundPeer(peer.host, peer.port))
        deduped: dict[str, OutboundPeer] = {}
        unnamed: list[OutboundPeer] = []
        for peer in sorted(peers, key=lambda peer: (peer.host, peer.port)):
            if self._is_local_listener_alias(peer):
                continue
            info = self._known_peer_info(peer.host, peer.port)
            if info is None or info.node_id is None:
                unnamed.append(peer)
                continue
            if info.node_id == self.node_id:
                continue
            current = deduped.get(info.node_id)
            if current is None or (peer.host, peer.port) in self._outbound_targets:
                deduped[info.node_id] = peer
        return sorted([*self._dedupe_unidentified_outbound_peers(unnamed), *deduped.values()], key=lambda peer: (peer.host, peer.port))

    def _is_dialable_peer(self, peer) -> bool:
        """Return whether a persisted peer is safe to use for outbound dialing."""

        if peer.direction == "inbound" or peer.port <= 0:
            return False
        return self._is_persisted_peer_host_dialable(peer.host)

    def _is_persisted_peer_host_dialable(self, host: str) -> bool:
        """Return whether one persisted peer host should be reused for outbound dialing."""

        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            # Hostnames remain eligible; explicit configured peers are handled separately.
            return True
        return address.is_global

    def _is_announced_peer_dialable(self, peer: OutboundPeer) -> bool:
        """Return whether one peer announced through addr should be accepted."""

        if peer.port <= 0:
            return False
        return self._is_persisted_peer_host_dialable(peer.host)

    def _resolved_peer_ips(self, peer: OutboundPeer) -> set[str]:
        """Resolve one peer endpoint into concrete IPs when possible."""

        try:
            address = ipaddress.ip_address(peer.host)
        except ValueError:
            try:
                return {
                    addrinfo[4][0]
                    for addrinfo in socket.getaddrinfo(peer.host, peer.port, type=socket.SOCK_STREAM)
                    if addrinfo[4]
                }
            except OSError:
                return set()
        return {str(address)}

    def _peers_equivalent(self, left: OutboundPeer, right: OutboundPeer) -> bool:
        """Return whether two peer targets resolve to the same remote endpoint."""

        if left.port != right.port:
            return False
        if left.host == right.host:
            return True
        left_ips = self._resolved_peer_ips(left)
        if not left_ips:
            return False
        right_ips = self._resolved_peer_ips(right)
        return bool(right_ips and left_ips & right_ips)

    def _is_known_peer_alias(self, peer: OutboundPeer) -> bool:
        """Return whether a newly learned peer is just an alias of a known endpoint."""

        peer_ips = self._resolved_peer_ips(peer)
        if not peer_ips:
            return False

        known_endpoints: set[OutboundPeer] = set(self._outbound_targets.values())
        for known_peer in self.service.list_peers():
            known_endpoints.add(OutboundPeer(known_peer.host, known_peer.port))
        for protocol, handle in self._sessions.items():
            endpoint = self._session_endpoint(protocol, handle)
            if endpoint is not None:
                known_endpoints.add(endpoint)

        for known in known_endpoints:
            if known.host == peer.host and known.port == peer.port:
                continue
            if self._peers_equivalent(known, peer):
                return True
        return False

    def _dedupe_unidentified_outbound_peers(self, peers: list[OutboundPeer]) -> list[OutboundPeer]:
        """Collapse hostname/IP aliases for peers that do not yet have a node id."""

        deduped: list[OutboundPeer] = []
        alias_keys: list[tuple[int, frozenset[str]]] = []
        for peer in peers:
            peer_ips = self._resolved_peer_ips(peer)
            if not peer_ips:
                deduped.append(peer)
                continue
            alias_key = (peer.port, frozenset(peer_ips))
            try:
                index = alias_keys.index(alias_key)
            except ValueError:
                alias_keys.append(alias_key)
                deduped.append(peer)
                continue
            deduped[index] = self._preferred_outbound_peer(deduped[index], peer)
        return deduped

    def _preferred_outbound_peer(self, current: OutboundPeer, candidate: OutboundPeer) -> OutboundPeer:
        """Choose the better endpoint when two outbound peers resolve to the same target."""

        current_configured = (current.host, current.port) in self._outbound_targets
        candidate_configured = (candidate.host, candidate.port) in self._outbound_targets
        if candidate_configured and not current_configured:
            return candidate
        if current_configured and not candidate_configured:
            return current

        current_is_ip = self._host_is_literal_ip(current.host)
        candidate_is_ip = self._host_is_literal_ip(candidate.host)
        if candidate_is_ip and not current_is_ip:
            return candidate
        if current_is_ip and not candidate_is_ip:
            return current

        return min(current, candidate, key=lambda peer: (peer.host, peer.port))

    def _host_is_literal_ip(self, host: str) -> bool:
        """Return whether one host string is already a literal IP address."""

        try:
            ipaddress.ip_address(host)
        except ValueError:
            return False
        return True

    def _is_advertisable_peer(self, peer) -> bool:
        """Return whether a persisted peer should be re-announced to other peers."""

        return peer.direction != "inbound" and peer.port > 0

    def _known_peer_info(self, host: str, port: int):
        """Return the current persisted peer info for one endpoint when known."""

        for peer in self.service.list_peers():
            if peer.host == host and peer.port == port and peer.network == self.service.network:
                return peer
        return None

    def _mark_session_activity(self, session: PeerProtocol) -> None:
        """Record recent peer activity for liveness decisions."""

        handle = self._sessions.get(session)
        if handle is None:
            return
        handle.last_activity_at = asyncio.get_running_loop().time()

    def _session_has_recent_activity(self, session: PeerProtocol) -> bool:
        """Return whether a peer recently exchanged useful sync traffic."""

        handle = self._sessions.get(session)
        if handle is None:
            return False
        if handle.last_activity_at <= 0:
            return False
        return (asyncio.get_running_loop().time() - handle.last_activity_at) < self.read_timeout

    def _begin_sync_tracking(self, session: PeerProtocol, target_height: int) -> None:
        """Track one peer catch-up session so progress logs stay aggregated."""

        handle = self._sessions.get(session)
        if handle is None:
            return
        local_tip = self.service.chain_tip()
        local_height = -1 if local_tip is None else local_tip.height
        target_height = max(target_height, local_height)
        if handle.sync_target_height is None or target_height > handle.sync_target_height:
            handle.sync_start_height = local_height
            handle.sync_target_height = target_height
            handle.sync_next_log_height = min(target_height, local_height + self._SYNC_PROGRESS_LOG_INTERVAL)

    def _sync_target_height(self, session: PeerProtocol) -> int:
        """Return the current sync target for one peer session."""

        handle = self._sessions.get(session)
        if handle is None or handle.sync_target_height is None:
            remote = session.state.remote_version
            return -1 if remote is None else remote.start_height
        return handle.sync_target_height

    def _sync_in_progress(self, session: PeerProtocol) -> bool:
        """Return whether the local chain is still catching up to this peer."""

        target_height = self._sync_target_height(session)
        local_tip = self.service.chain_tip()
        local_height = -1 if local_tip is None else local_tip.height
        return target_height > local_height

    def _log_sync_progress(self, session: PeerProtocol, *, force: bool = False) -> None:
        """Emit compact sync progress instead of one info line per block."""

        handle = self._sessions.get(session)
        if handle is None:
            return
        target_height = self._sync_target_height(session)
        local_tip = self.service.chain_tip()
        local_height = -1 if local_tip is None else local_tip.height
        if target_height <= local_height:
            if handle.sync_target_height is not None:
                self.logger.info(
                    "sync complete peer=%s local_height=%s target_height=%s",
                    self._format_peer_for_logs(session),
                    local_height,
                    target_height,
                )
            handle.sync_start_height = None
            handle.sync_target_height = None
            handle.sync_next_log_height = None
            return

        if not force and handle.sync_next_log_height is not None and local_height < handle.sync_next_log_height:
            return

        start_height = local_height if handle.sync_start_height is None else handle.sync_start_height
        total_blocks = max(0, target_height - start_height)
        synced_blocks = max(0, local_height - start_height)
        remaining_blocks = max(0, target_height - local_height)
        self.logger.info(
            "syncing blocks peer=%s synced=%s/%s local_height=%s target_height=%s remaining=%s",
            self._format_peer_for_logs(session),
            synced_blocks,
            total_blocks,
            local_height,
            target_height,
            remaining_blocks,
        )
        handle.sync_next_log_height = min(target_height, local_height + self._SYNC_PROGRESS_LOG_INTERVAL)

    def _log_block_application(self, session: PeerProtocol, result, *, reorged: bool) -> None:
        """Log applied blocks compactly during catch-up and verbosely once in sync."""

        if self._sync_in_progress(session):
            self.logger.debug(
                "%s peer=%s block=%s activated_tip=%s accepted_blocks=%s",
                "reorg applied" if reorged else "block applied",
                self._format_peer_for_logs(session),
                result.block_hash,
                result.activated_tip,
                result.accepted_blocks,
            )
            self._log_sync_progress(session)
            return

        self.logger.info(
            "%s peer=%s block=%s activated_tip=%s accepted_blocks=%s",
            "reorg applied" if reorged else "block applied",
            self._format_peer_for_logs(session),
            result.block_hash,
            result.activated_tip,
            result.accepted_blocks,
        )
        self._log_sync_progress(session, force=True)

    def _forget_self_alias(self, session: PeerProtocol) -> None:
        """Drop one endpoint that resolves back to this local node."""

        handle = self._sessions.get(session)
        endpoint = self._session_endpoint(session, handle)
        if endpoint is None:
            return
        aliases = {peer for peer in self._outbound_targets.values() if self._peers_equivalent(peer, endpoint)}
        aliases.update(
            OutboundPeer(peer.host, peer.port)
            for peer in self.service.list_peers()
            if self._peers_equivalent(OutboundPeer(peer.host, peer.port), endpoint)
        )
        for alias in aliases:
            self._outbound_targets.pop((alias.host, alias.port), None)
            self.service.remove_peer(alias.host, alias.port)
            self.logger.info("removed self-alias peer=%s:%s from outbound peer set", alias.host, alias.port)

    def _purge_persisted_self_aliases(self) -> None:
        """Drop persisted peer aliases that resolve back to this local listener."""

        aliases = [
            peer
            for peer in self.service.list_peers()
            if self._is_local_listener_alias(OutboundPeer(peer.host, peer.port))
        ]
        for peer in aliases:
            self._outbound_targets.pop((peer.host, peer.port), None)
            self.service.remove_peer(peer.host, peer.port)
            self.logger.info("removed startup self-alias peer=%s:%s", peer.host, peer.port)

    def _purge_undialable_persisted_peers(self) -> None:
        """Drop persisted peer endpoints that should never be redialed automatically."""

        peers = [
            peer
            for peer in self.service.list_peers()
            if not self._is_persisted_peer_host_dialable(peer.host)
        ]
        for peer in peers:
            self._outbound_targets.pop((peer.host, peer.port), None)
            self.service.remove_peer(peer.host, peer.port)
            self.logger.info("removed startup undialable peer=%s:%s", peer.host, peer.port)

    def _purge_persisted_startup_duplicate_aliases(self) -> None:
        """Drop persisted startup aliases previously classified as duplicate/self connections."""

        peers = [
            peer
            for peer in self.service.list_peers()
            if peer.direction == "outbound"
            and peer.port == self.bound_port
            and peer.protocol_error_class == "duplicate_connection"
        ]
        for peer in peers:
            self._outbound_targets.pop((peer.host, peer.port), None)
            self.service.remove_peer(peer.host, peer.port)
            self.logger.info("removed startup duplicate alias peer=%s:%s", peer.host, peer.port)

    def _is_local_listener_alias(self, peer: OutboundPeer) -> bool:
        """Return whether a peer endpoint resolves back to this local listener."""

        if peer.port != self.bound_port:
            return False
        if peer.host == self.listen_host:
            return True
        if peer.host == "localhost" and self.listen_host in {"127.0.0.1", "0.0.0.0", "::"}:
            return True
        if peer.host == "127.0.0.1" and self.listen_host in {"127.0.0.1", "0.0.0.0"}:
            return True
        try:
            peer_ips = {
                addrinfo[4][0]
                for addrinfo in socket.getaddrinfo(peer.host, peer.port, type=socket.SOCK_STREAM)
                if addrinfo[4]
            }
        except OSError:
            return False
        return bool(peer_ips & self._local_listener_ips())

    def _local_listener_ips(self) -> set[str]:
        """Return routable IPs that identify this process as a local listener."""

        ips: set[str] = set()
        if self.listen_host not in {"0.0.0.0", "::"}:
            ips.add(self.listen_host)
        try:
            hostname = socket.gethostname()
            ips.update(socket.gethostbyname_ex(hostname)[2])
        except OSError:
            pass
        return ips

    def _canonicalize_peer_aliases(
        self,
        node_id: str,
        *,
        canonical_host: str,
        canonical_port: int,
        prefer_configured: OutboundPeer | None,
    ) -> None:
        """Keep a single outbound endpoint per remote node id."""

        preferred_host = canonical_host
        preferred_port = canonical_port
        if prefer_configured is not None:
            preferred_host = prefer_configured.host
            preferred_port = prefer_configured.port

        aliases = [
            peer
            for peer in self.service.list_peers()
            if peer.node_id == node_id and (peer.host, peer.port) != (preferred_host, preferred_port)
        ]
        for peer in aliases:
            self._outbound_targets.pop((peer.host, peer.port), None)
            self.service.remove_peer(peer.host, peer.port)
            self.logger.info(
                "removed peer alias node_id=%s alias=%s:%s canonical=%s:%s",
                node_id,
                peer.host,
                peer.port,
                preferred_host,
                preferred_port,
            )

    def _updated_peer_score(self, host: str, port: int, *, delta: int) -> int:
        """Return a bounded peer score delta applied to the current value."""

        existing = self._known_peer_info(host, port)
        current = 0 if existing is None or existing.score is None else existing.score
        return max(-100, min(100, current + delta))

    def _is_backoff_active(self, peer: OutboundPeer) -> bool:
        """Return whether an outbound peer is still under reconnect backoff."""

        info = self._known_peer_info(peer.host, peer.port)
        if info is None or info.backoff_until is None:
            return False
        return info.backoff_until > self.service.time_provider()

    def _register_peer_failure(self, peer: OutboundPeer, *, error: Exception | str, penalty: int | None = None) -> None:
        """Persist a scored outbound failure with exponential reconnect backoff."""

        info = self._known_peer_info(peer.host, peer.port)
        attempts, backoff_until = self._next_backoff_state(info)
        now = self.service.time_provider()
        error_text = str(error)
        applied_penalty = self._penalty_for_error(error) if penalty is None else penalty
        self.service.record_peer_observation(
            host=peer.host,
            port=peer.port,
            direction="outbound",
            handshake_complete=False,
            score=self._updated_peer_score(peer.host, peer.port, delta=-applied_penalty),
            reconnect_attempts=attempts,
            backoff_until=backoff_until if backoff_until > now else now + 1,
            last_error=error_text,
            last_error_at=now,
            protocol_error_class=classify_peer_error(error),
            disconnect_count=0 if info is None or info.disconnect_count is None else info.disconnect_count + 1,
        )
        self.logger.info(
            "peer backoff applied peer=%s:%s reconnect_attempts=%s backoff_until=%s score=%s error=%s",
            peer.host,
            peer.port,
            attempts,
            backoff_until if backoff_until > now else now + 1,
            self._updated_peer_score(peer.host, peer.port, delta=-applied_penalty),
            error_text,
        )

    def _apply_session_penalty(self, session: PeerProtocol, *, error: Exception | str, penalty: int) -> None:
        """Penalize a peer session using the observed endpoint."""

        handle = self._sessions.get(session)
        endpoint = self._session_endpoint(session, handle)
        if endpoint is None:
            return
        info = self._known_peer_info(endpoint.host, endpoint.port)
        error_text = str(error)
        self.service.record_peer_observation(
            host=endpoint.host,
            port=endpoint.port,
            direction=None if handle is None else ("outbound" if handle.outbound else "inbound"),
            handshake_complete=False if not session.state.handshake_complete else True,
            last_known_height=None if session.state.remote_version is None else session.state.remote_version.start_height,
            node_id=None if session.state.remote_version is None else session.state.remote_version.node_id,
            score=self._updated_peer_score(endpoint.host, endpoint.port, delta=-penalty),
            reconnect_attempts=None if info is None else info.reconnect_attempts,
            backoff_until=None if info is None else info.backoff_until,
            last_error=error_text,
            last_error_at=self.service.time_provider(),
            protocol_error_class=classify_peer_error(error),
            disconnect_count=None if info is None else info.disconnect_count,
            session_started_at=None if info is None else info.session_started_at,
        )

    def _is_duplicate_inventory(self, session: PeerProtocol, item: InventoryVector) -> bool:
        """Track repeated inventory announcements from one session."""

        handle = self._sessions.get(session)
        if handle is None:
            return False
        key = (item.object_type, item.object_hash)
        count = handle.announced_inventory_counts.get(key, 0) + 1
        handle.announced_inventory_counts[key] = count
        return count > self.duplicate_inventory_limit

    def _next_backoff_state(self, info) -> tuple[int, int]:
        """Return reconnect attempts and absolute backoff deadline for one peer."""

        attempts = 1 if info is None or info.reconnect_attempts is None else info.reconnect_attempts + 1
        delay_seconds = min(self.max_connect_backoff_seconds, self.connect_interval * (2 ** min(attempts - 1, 5)))
        now = self.service.time_provider()
        return attempts, now + max(1, int(delay_seconds))

    def _penalty_for_error(self, error: Exception | str) -> int:
        """Map transport/protocol errors to a small peer score penalty."""

        classification = protocol_error_class(error)
        if classification in {"wrong_network_magic", "checksum_error", "malformed_message", "invalid_block"}:
            return 25
        if classification == "handshake_failed":
            return 20
        if classification == "timeout":
            return 10
        if classification == "duplicate_connection":
            return 5
        if classification in {"invalid_tx", "connection_closed"}:
            return 5
        return 5

    def _local_identity(self) -> LocalPeerIdentity:
        """Build the local identity used for the next session."""

        tip = self.service.chain_tip()
        return LocalPeerIdentity(
            node_id=self.node_id,
            network=self.service.network,
            start_height=0 if tip is None else tip.height,
            user_agent="/chipcoin-v2:0.1.0/",
            network_magic=get_network_config(self.service.network).magic,
        )

    def _spawn_task(self, coro, name: str) -> asyncio.Task:
        """Create and track a background task."""

        task = asyncio.create_task(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def _mining_loop(self) -> None:
        """Continuously mine blocks on the latest local template."""

        assert self.miner_address is not None
        while self._running:
            if not self._mining_ready_for_work():
                await asyncio.sleep(self.mining_idle_interval)
                continue
            await self._wait_for_next_mining_slot()
            template = self._refresh_mining_template()
            if template is None:
                await asyncio.sleep(self.mining_idle_interval)
                continue
            mined_block = self.service.mining.mine_block(
                template,
                start_nonce=self._mining_nonce_cursor,
                max_nonce_attempts=self.mining_nonce_batch_size,
            )
            self._mining_nonce_cursor += self.mining_nonce_batch_size
            if mined_block is not None:
                self.logger.info(
                    "mined block height=%s hash=%s txs=%s",
                    template.height,
                    mined_block.block_hash(),
                    len(mined_block.transactions),
                )
                await self.announce_block(mined_block)
                self._last_mined_monotonic = asyncio.get_running_loop().time()
                self._invalidate_mining_template()
            await asyncio.sleep(0)

    def _refresh_mining_template(self):
        """Rebuild the mining template when chain tip or mempool contents changed."""

        if self.miner_address is None:
            return None
        tip = self.service.chain_tip()
        mempool_txids = tuple(transaction.txid() for transaction in self.service.list_mempool_transactions())
        template_key = (None if tip is None else tip.block_hash, mempool_txids)
        if self._mining_template is None or template_key != self._mining_template_key:
            self._mining_template = self.service.build_candidate_block(self.miner_address)
            self._mining_template_key = template_key
            self._mining_nonce_cursor = 0
        return self._mining_template

    def _mining_ready_for_work(self) -> bool:
        """Return whether the miner should start or resume local mining."""

        if self.miner_address is None:
            return False

        local_tip = self.service.chain_tip()
        local_height = -1 if local_tip is None else local_tip.height
        active_remote_heights = [
            remote.start_height
            for protocol in self._sessions
            if protocol.state.handshake_complete and not protocol.state.closed
            for remote in [protocol.state.remote_version]
            if remote is not None
        ]

        if self._initial_sync_required and not active_remote_heights:
            if not self._mining_wait_logged:
                self.logger.info("mining paused reason=awaiting_initial_peer_sync local_height=%s", local_height)
                self._mining_wait_logged = True
            return False

        max_remote_height = max(active_remote_heights, default=local_height)
        if max_remote_height > local_height:
            if not self._mining_wait_logged:
                self.logger.info(
                    "mining paused reason=chain_not_synced local_height=%s remote_height=%s",
                    local_height,
                    max_remote_height,
                )
                self._mining_wait_logged = True
            return False

        if self._mining_wait_logged:
            self.logger.info("mining resumed local_height=%s remote_height=%s", local_height, max_remote_height)
            self._mining_wait_logged = False
        return True

    def _invalidate_mining_template(self) -> None:
        """Drop the current mining template so the next loop rebuilds it."""

        self._mining_template = None
        self._mining_template_key = None
        self._mining_nonce_cursor = 0

    async def _wait_for_next_mining_slot(self) -> None:
        """Optionally pace local mining for demo or test environments."""

        if self.mining_min_interval_seconds <= 0 or self._last_mined_monotonic is None:
            return
        elapsed = asyncio.get_running_loop().time() - self._last_mined_monotonic
        remaining = self.mining_min_interval_seconds - elapsed
        if remaining > 0:
            await asyncio.sleep(remaining)

    def _session_endpoint(self, session: PeerProtocol, handle: SessionHandle | None = None) -> PeerEndpoint | None:
        """Return the best-known remote endpoint for a session."""

        if handle is not None and handle.endpoint is not None:
            return PeerEndpoint(host=handle.endpoint.host, port=handle.endpoint.port)
        peer_endpoint = getattr(session.transport, "peer_endpoint", None)
        if callable(peer_endpoint):
            return peer_endpoint()
        return None

    def _format_peer_for_logs(self, session: PeerProtocol) -> str:
        """Return a compact peer identifier for runtime logs."""

        endpoint = self._session_endpoint(session, self._sessions.get(session))
        endpoint_text = "unknown:0" if endpoint is None else f"{endpoint.host}:{endpoint.port}"
        remote = session.state.remote_version
        if remote is None:
            return endpoint_text
        return f"{endpoint_text}/{remote.node_id}"
