"""Persistent P2P node runtime."""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import logging
import os
import secrets
import socket
import threading
from dataclasses import dataclass, field
from pathlib import Path
from wsgiref.simple_server import make_server

from .. import __version__
from ..config import get_network_config
from ..consensus.epoch_settlement import RewardAttestation
from ..consensus.models import Transaction
from ..consensus.nodes import current_epoch
from ..crypto.keys import parse_private_key_hex
from ..consensus.validation import ContextualValidationError, StatelessValidationError, ValidationError
from ..interfaces.http_api import HttpApiApp, ThreadingWSGIServer, load_allowed_origins_from_env
from ..utils.logging import configure_logging
from ..wallet.signer import TransactionSigner, wallet_key_from_private_key
from .p2p.errors import (
    BlockRequestStalledError,
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
    reusable_endpoint: bool = False
    announced_inventory_counts: dict[tuple[str, str], int] = field(default_factory=dict)
    consecutive_ping_failures: int = 0
    last_activity_at: float = 0.0
    sync_target_height: int | None = None
    sync_total_missing_blocks: int | None = None
    sync_next_log_height: int | None = None
    headers_sync_active: bool = False
    last_headers_requested_at: float = 0.0
    inflight_block_hashes: set[str] = field(default_factory=set)
    block_stall_count: int = 0
    headers_contributed: int = 0
    blocks_contributed: int = 0
    last_block_progress_at: float = 0.0
    addr_relay_window_started_at: float = 0.0
    addr_relay_entries_sent: int = 0


@dataclass(frozen=True)
class RewardNodeAutomationConfig:
    """Local operator configuration for one auto-managed reward node."""

    node_id: str
    owner_wallet_path: Path
    attest_wallet_path: Path
    declared_host: str | None = None
    declared_port: int | None = None
    auto_renew_enabled: bool = True
    auto_attest_enabled: bool = True
    poll_interval_seconds: float = 5.0


def _parse_bool_env(raw: str | None, *, default: bool) -> bool:
    """Parse one shell-style boolean env value."""

    if raw is None:
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    return default


def _load_wallet_key(path: Path):
    """Load one minimal wallet JSON file from disk."""

    payload = json.loads(path.read_text(encoding="utf-8"))
    return wallet_key_from_private_key(
        parse_private_key_hex(str(payload["private_key_hex"])),
        compressed=bool(payload.get("compressed", True)),
    )


def load_reward_node_automation_config_from_env() -> RewardNodeAutomationConfig | None:
    """Load reward-node runtime automation config from environment."""

    node_id = os.getenv("REWARD_NODE_AUTO_NODE_ID", "").strip()
    if not node_id:
        return None
    owner_wallet = os.getenv("REWARD_NODE_AUTO_OWNER_WALLET_FILE", "").strip()
    if not owner_wallet:
        raise ValueError("REWARD_NODE_AUTO_OWNER_WALLET_FILE is required when REWARD_NODE_AUTO_NODE_ID is set.")
    attest_wallet = os.getenv("REWARD_NODE_AUTO_ATTEST_WALLET_FILE", "").strip() or owner_wallet
    declared_host = os.getenv("REWARD_NODE_AUTO_DECLARED_HOST", "").strip() or None
    declared_port_raw = os.getenv("REWARD_NODE_AUTO_DECLARED_PORT", "").strip()
    declared_port = None if not declared_port_raw else int(declared_port_raw)
    return RewardNodeAutomationConfig(
        node_id=node_id,
        owner_wallet_path=Path(owner_wallet),
        attest_wallet_path=Path(attest_wallet),
        declared_host=declared_host,
        declared_port=declared_port,
        auto_renew_enabled=_parse_bool_env(os.getenv("REWARD_NODE_AUTO_RENEW_ENABLED"), default=True),
        auto_attest_enabled=_parse_bool_env(os.getenv("REWARD_NODE_AUTO_ATTEST_ENABLED"), default=True),
        poll_interval_seconds=max(1.0, float(os.getenv("REWARD_NODE_AUTO_POLL_INTERVAL_SECONDS", "5.0"))),
    )


class NodeRuntime:
    """Long-running TCP runtime coordinating peer sessions and sync."""

    _SYNC_PROGRESS_LOG_INTERVAL = 100
    _SYNC_SCHEDULER_INTERVAL = 0.25
    _INITIAL_SYNC_STALL_GRACE_MULTIPLIER = 2.0
    _SEVERE_MISBEHAVIOR_DELTA = 100
    _BLOCK_STALL_DISCONNECT_THRESHOLD = 2

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
        mempool_relay_interval: float = 0.25,
        max_connect_backoff_seconds: float = 30.0,
        max_consecutive_ping_failures: int = 3,
        max_inventory_items: int = 500,
        max_addr_records: int = 1000,
        max_headers_per_message: int = 2000,
        headers_sync_enabled: bool = True,
        block_download_window_size: int = 128,
        block_max_inflight_per_peer: int = 16,
        block_request_timeout_seconds: float = 10.0,
        headers_sync_parallel_peers: int = 2,
        headers_sync_start_height_gap_threshold: int = 1,
        duplicate_inventory_limit: int = 20,
        peer_discovery_enabled: bool = True,
        peerbook_max_size: int = 1024,
        peer_addr_max_per_message: int = 250,
        peer_addr_relay_limit_per_interval: int = 250,
        peer_addr_relay_interval_seconds: int = 30,
        peer_stale_after_seconds: int = 604800,
        peer_retry_backoff_base_seconds: float = 1.0,
        peer_retry_backoff_max_seconds: float = 30.0,
        peer_discovery_startup_prefer_persisted: bool = True,
        misbehavior_warning_threshold: int = 25,
        misbehavior_disconnect_threshold: int = 50,
        misbehavior_ban_threshold: int = 100,
        misbehavior_ban_duration_seconds: int = 1800,
        misbehavior_decay_interval_seconds: int = 300,
        misbehavior_decay_step: int = 5,
        http_host: str | None = None,
        http_port: int | None = None,
        reward_automation: RewardNodeAutomationConfig | None = None,
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
        self.mempool_relay_interval = max(0.05, mempool_relay_interval)
        self.max_connect_backoff_seconds = max(1.0, max_connect_backoff_seconds)
        self.max_consecutive_ping_failures = max(1, max_consecutive_ping_failures)
        self.max_inventory_items = max_inventory_items
        self.max_addr_records = min(max_addr_records, peer_addr_max_per_message)
        self.max_headers_per_message = max_headers_per_message
        self.headers_sync_enabled = headers_sync_enabled
        self.block_download_window_size = max(1, block_download_window_size)
        self.block_max_inflight_per_peer = max(1, block_max_inflight_per_peer)
        self.block_request_timeout_seconds = max(1.0, block_request_timeout_seconds)
        self.headers_sync_parallel_peers = max(1, headers_sync_parallel_peers)
        self.headers_sync_start_height_gap_threshold = max(0, headers_sync_start_height_gap_threshold)
        self.duplicate_inventory_limit = duplicate_inventory_limit
        self.peer_discovery_enabled = peer_discovery_enabled
        self.peerbook_max_size = max(16, peerbook_max_size)
        self.peer_addr_max_per_message = max(1, min(peer_addr_max_per_message, max_addr_records))
        self.peer_addr_relay_limit_per_interval = max(1, peer_addr_relay_limit_per_interval)
        self.peer_addr_relay_interval_seconds = max(1, peer_addr_relay_interval_seconds)
        self.peer_stale_after_seconds = max(60, peer_stale_after_seconds)
        self.peer_retry_backoff_base_seconds = max(0.5, peer_retry_backoff_base_seconds)
        self.peer_retry_backoff_max_seconds = max(1.0, peer_retry_backoff_max_seconds)
        self.peer_discovery_startup_prefer_persisted = peer_discovery_startup_prefer_persisted
        self.misbehavior_warning_threshold = max(1, misbehavior_warning_threshold)
        self.misbehavior_disconnect_threshold = max(
            self.misbehavior_warning_threshold,
            misbehavior_disconnect_threshold,
        )
        self.misbehavior_ban_threshold = max(
            self.misbehavior_disconnect_threshold,
            misbehavior_ban_threshold,
        )
        self.misbehavior_ban_duration_seconds = max(1, misbehavior_ban_duration_seconds)
        self.misbehavior_decay_interval_seconds = max(1, misbehavior_decay_interval_seconds)
        self.misbehavior_decay_step = max(1, misbehavior_decay_step)
        self.http_host = http_host
        self.http_port = http_port
        self.reward_automation = reward_automation
        self.logger = logger or logging.getLogger("chipcoin.node.runtime")
        self.sync_manager = SyncManager(node=service)
        self.sync_manager.max_headers = max_headers_per_message
        self.node_id = secrets.token_hex(16)

        self._server: asyncio.AbstractServer | None = None
        self._http_server = None
        self._http_thread: threading.Thread | None = None
        self._event_loop: asyncio.AbstractEventLoop | None = None
        self._running = False
        self._stop_event = asyncio.Event()
        self._tasks: set[asyncio.Task] = set()
        self._sessions: dict[PeerProtocol, SessionHandle] = {}
        self._sessions_by_node_id: dict[str, PeerProtocol] = {}
        self._pending_outbound_peers: set[OutboundPeer] = set()
        self._outbound_targets: dict[tuple[str, int], OutboundPeer] = {
            (peer.host, peer.port): peer for peer in (outbound_peers or [])
        }
        self._outbound_target_sources: dict[tuple[str, int], str] = {
            (peer.host, peer.port): "manual" for peer in (outbound_peers or [])
        }
        self._relayed_mempool_txids: set[str] = set()
        self._last_logged_sync_phase: str | None = None
        self._reward_owner_wallet = None if reward_automation is None else _load_wallet_key(reward_automation.owner_wallet_path)
        self._reward_attest_wallet = None if reward_automation is None else _load_wallet_key(reward_automation.attest_wallet_path)
        self._reward_submitted_renewal_epochs: set[int] = set()
        self._reward_submitted_attestation_identities: set[tuple[int, int, str, str]] = set()

    @property
    def bound_port(self) -> int:
        """Return the active bound port once the server is running."""

        if self._server is None or not self._server.sockets:
            return self.listen_port
        return int(self._server.sockets[0].getsockname()[1])

    @property
    def http_bound_port(self) -> int | None:
        """Return the active HTTP API port once started."""

        if self._http_server is None:
            return self.http_port
        return int(self._http_server.server_port)

    async def start(self) -> None:
        """Start the runtime listener and background loops."""

        if self._running:
            return
        configure_logging()
        self._event_loop = asyncio.get_running_loop()
        self._server = await asyncio.start_server(self._handle_inbound_connection, self.listen_host, self.listen_port)
        self._start_http_api_server()
        self._running = True
        self.logger.info(
            "runtime started network=%s listen=%s:%s outbound_targets=%s ping_interval=%s read_timeout=%s bootstrap_mode=%s snapshot_anchor_height=%s snapshot_anchor_hash=%s",
            self.service.network,
            self.listen_host,
            self.bound_port,
            len(self._outbound_targets),
            self.ping_interval,
            self.read_timeout,
            "full" if self.service.snapshot_anchor() is None else "snapshot",
            None if self.service.snapshot_anchor() is None else self.service.snapshot_anchor().height,
            None if self.service.snapshot_anchor() is None else self.service.snapshot_anchor().block_hash,
        )
        self._persist_configured_peer_targets()
        self._purge_persisted_self_aliases()
        self._purge_undialable_persisted_peers()
        self._purge_stale_persisted_peers()
        self._trim_peerbook_to_capacity()
        self._purge_persisted_startup_duplicate_aliases()
        self._update_sync_status()
        self._stop_event.clear()
        self._spawn_task(self._connect_loop(), "connect-loop")
        self._spawn_task(self._ping_loop(), "ping-loop")
        self._spawn_task(self._mempool_relay_loop(), "mempool-relay-loop")
        if self.headers_sync_enabled:
            self._spawn_task(self._sync_scheduler_loop(), "sync-scheduler-loop")
        if self.reward_automation is not None and (self.reward_automation.auto_renew_enabled or self.reward_automation.auto_attest_enabled):
            self._spawn_task(self._reward_automation_loop(), "reward-automation-loop")

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
        if self._http_server is not None:
            self._http_server.shutdown()
            self._http_server.server_close()
            if self._http_thread is not None:
                self._http_thread.join(timeout=max(1.0, self.read_timeout))
            self._http_server = None
            self._http_thread = None
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
        self._pending_outbound_peers.clear()
        self._relayed_mempool_txids.clear()
        self.service.set_runtime_sync_status(None)
        self._event_loop = None
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
        self._relayed_mempool_txids.add(accepted.transaction.txid())
        await self._broadcast_inventory(InventoryVector(object_type="tx", object_hash=accepted.transaction.txid()))

    async def announce_block(self, block) -> None:
        """Apply a local block and relay its inventory to peers."""

        self.service.apply_block(block)
        self.logger.info("local block applied height=%s hash=%s", self.service.chain_tip().height, block.block_hash())
        await self._broadcast_inventory(InventoryVector(object_type="block", object_hash=block.block_hash()))

    def _start_http_api_server(self) -> None:
        """Start the runtime-owned HTTP API so submit paths share one authority."""

        if self.http_host is None or self.http_port is None:
            return
        app = HttpApiApp(
            self.service,
            allowed_origins=load_allowed_origins_from_env(),
            mining_submit_handler=self.submit_mined_block_from_http,
            tx_submit_handler=self.submit_raw_transaction_from_http,
        )
        self._http_server = make_server(self.http_host, self.http_port, app, server_class=ThreadingWSGIServer)
        self._http_thread = threading.Thread(target=self._http_server.serve_forever, daemon=True)
        self._http_thread.start()
        self.logger.info("http api started host=%s port=%s", self.http_host, self.http_bound_port)

    def submit_mined_block_from_http(
        self,
        *,
        template_id: str,
        serialized_block_hex: str,
        miner_id: str,
    ) -> dict[str, object]:
        """Route one mined block submit through the runtime acceptance path."""

        if self._event_loop is None:
            return {"accepted": False, "reason": "runtime_not_running", "block_hash": None, "became_tip": False}
        future = asyncio.run_coroutine_threadsafe(
            self._submit_mined_block_from_http(
                template_id=template_id,
                serialized_block_hex=serialized_block_hex,
                miner_id=miner_id,
            ),
            self._event_loop,
        )
        return future.result(timeout=max(5.0, self.read_timeout))

    def submit_raw_transaction_from_http(self, *, raw_hex: str) -> dict[str, object]:
        """Route one raw transaction submit through the runtime acceptance path."""

        if self._event_loop is None:
            return {"accepted": False, "reason": "runtime_not_running", "txid": None, "fee": None}
        future = asyncio.run_coroutine_threadsafe(
            self._submit_raw_transaction_from_http(raw_hex=raw_hex),
            self._event_loop,
        )
        return future.result(timeout=max(5.0, self.read_timeout))

    async def _submit_mined_block_from_http(
        self,
        *,
        template_id: str,
        serialized_block_hex: str,
        miner_id: str,
    ) -> dict[str, object]:
        """Validate one template submission and accept it through announce_block."""

        prepared = self.service.prepare_mined_block_submission(
            template_id=template_id,
            serialized_block_hex=serialized_block_hex,
            miner_id=miner_id,
        )
        if prepared.get("accepted") is False:
            return prepared
        block = prepared["block"]
        try:
            await self.announce_block(block)
        except ValidationError as exc:
            self.service.discard_mining_template(template_id)
            return {"accepted": False, "reason": f"validation_error:{exc}", "block_hash": None, "became_tip": False}
        self.service.discard_mining_template(template_id)
        return {"accepted": True, "reason": "accepted", "block_hash": block.block_hash(), "became_tip": True}

    async def _submit_raw_transaction_from_http(self, *, raw_hex: str) -> dict[str, object]:
        """Decode and accept one raw transaction through the runtime relay path."""

        transaction = self.service.decode_raw_transaction(raw_hex)
        accepted = self.service.receive_transaction(transaction)
        self.logger.info("local tx accepted txid=%s fee_chipbits=%s", accepted.transaction.txid(), accepted.fee)
        self._relayed_mempool_txids.add(accepted.transaction.txid())
        await self._broadcast_inventory(InventoryVector(object_type="tx", object_hash=accepted.transaction.txid()))
        return {"accepted": True, "txid": accepted.transaction.txid(), "fee": accepted.fee}

    async def _reward_automation_loop(self) -> None:
        """Periodically auto-renew and auto-attest for one configured reward node."""

        assert self.reward_automation is not None
        while self._running:
            try:
                await self._run_reward_automation_once()
            except Exception as exc:
                self.logger.warning("reward automation loop failed node_id=%s error=%s", self.reward_automation.node_id, exc)
            await asyncio.sleep(self.reward_automation.poll_interval_seconds)

    async def _run_reward_automation_once(self) -> None:
        """Run one idempotent reward automation pass."""

        assert self.reward_automation is not None
        current_epoch_index = self.service.next_block_epoch()
        self._reward_submitted_renewal_epochs = {epoch for epoch in self._reward_submitted_renewal_epochs if epoch >= current_epoch_index}
        self._reward_submitted_attestation_identities = {
            identity for identity in self._reward_submitted_attestation_identities if identity[0] >= current_epoch_index
        }
        if self.reward_automation.auto_renew_enabled:
            await self._maybe_auto_renew(current_epoch_index)
        if self.reward_automation.auto_attest_enabled:
            await self._maybe_auto_attest(current_epoch_index)

    async def _maybe_auto_renew(self, current_epoch_index: int) -> None:
        """Submit one renewal transaction when the configured reward node is stale."""

        assert self.reward_automation is not None
        assert self._reward_owner_wallet is not None
        record = self.service.get_registered_node(self.reward_automation.node_id)
        if record is None or not record.reward_registration:
            return
        if record.owner_pubkey != self._reward_owner_wallet.public_key:
            raise ValueError(f"owner wallet does not match reward node owner for node_id={record.node_id}")
        if current_epoch(record.last_renewed_height, self.service.params) == current_epoch_index:
            return
        if current_epoch_index in self._reward_submitted_renewal_epochs:
            return
        declared_host = self.reward_automation.declared_host or record.declared_host
        declared_port = self.reward_automation.declared_port or record.declared_port
        if not declared_host or declared_port is None:
            raise ValueError(f"reward node declared endpoint is incomplete for node_id={record.node_id}")
        transaction = TransactionSigner(self._reward_owner_wallet).build_renew_reward_node_transaction(
            node_id=record.node_id,
            renewal_epoch=current_epoch_index,
            declared_host=declared_host,
            declared_port=int(declared_port),
            renewal_fee_chipbits=int(self.service.reward_node_fee_schedule()["renew_fee_chipbits"]),
        )
        await self.submit_transaction(transaction)
        self._reward_submitted_renewal_epochs.add(current_epoch_index)
        self.logger.info("auto reward renewal submitted node_id=%s epoch=%s txid=%s", record.node_id, current_epoch_index, transaction.txid())

    async def _maybe_auto_attest(self, current_epoch_index: int) -> None:
        """Submit deterministic pass attestations for one configured verifier node."""

        assert self.reward_automation is not None
        assert self._reward_attest_wallet is not None
        tip = self.service.chain_tip()
        next_height = 0 if tip is None else tip.height + 1
        if next_height < self.service.params.node_reward_activation_height:
            return
        status = self.service.reward_node_status(node_id=self.reward_automation.node_id, epoch_index=current_epoch_index)
        if not bool(status.get("selected_epoch_active")):
            return
        record = self.service.get_registered_node(self.reward_automation.node_id)
        if record is None or record.node_pubkey is None:
            return
        if record.node_pubkey != self._reward_attest_wallet.public_key:
            raise ValueError(f"attestation wallet does not match reward node node_pubkey for node_id={record.node_id}")
        recorded_identities = self.service.reward_attestations.attestation_identities()
        assignments = self.service.native_reward_assignments(epoch_index=current_epoch_index)
        selected_candidates_by_window: dict[int, str] = {}
        for assignment in assignments:
            candidate_node_id = str(assignment["node_id"])
            for window_index in assignment["candidate_check_windows"]:
                committee = assignment["verifier_committees"].get(str(window_index), [])
                identity = (current_epoch_index, int(window_index), candidate_node_id, self.reward_automation.node_id)
                if self.reward_automation.node_id not in committee:
                    continue
                if identity in recorded_identities or identity in self._reward_submitted_attestation_identities:
                    continue
                attestation_score = hashlib.sha256(
                    (
                        f"reward-auto-attest|{current_epoch_index}|{int(window_index)}|"
                        f"{self.reward_automation.node_id}|{candidate_node_id}"
                    ).encode("utf-8")
                ).hexdigest()
                selected_candidate = selected_candidates_by_window.get(int(window_index))
                if selected_candidate is None:
                    selected_candidates_by_window[int(window_index)] = candidate_node_id
                    continue
                selected_score = hashlib.sha256(
                    (
                        f"reward-auto-attest|{current_epoch_index}|{int(window_index)}|"
                        f"{self.reward_automation.node_id}|{selected_candidate}"
                    ).encode("utf-8")
                ).hexdigest()
                if (attestation_score, candidate_node_id) < (selected_score, selected_candidate):
                    selected_candidates_by_window[int(window_index)] = candidate_node_id
        bundles_by_window: dict[int, list[RewardAttestation]] = {}
        for assignment in assignments:
            candidate_node_id = str(assignment["node_id"])
            for window_index in assignment["candidate_check_windows"]:
                if selected_candidates_by_window.get(int(window_index)) != candidate_node_id:
                    continue
                identity = (current_epoch_index, int(window_index), candidate_node_id, self.reward_automation.node_id)
                if identity in recorded_identities or identity in self._reward_submitted_attestation_identities:
                    continue
                endpoint_commitment = f"{assignment['declared_host']}:{assignment['declared_port']}"
                attestation = TransactionSigner(self._reward_attest_wallet).sign_reward_attestation(
                    RewardAttestation(
                        epoch_index=current_epoch_index,
                        check_window_index=int(window_index),
                        candidate_node_id=candidate_node_id,
                        verifier_node_id=self.reward_automation.node_id,
                        result_code="pass",
                        observed_sync_gap=0,
                        endpoint_commitment=endpoint_commitment,
                        concentration_key=f"unscoped:{candidate_node_id}",
                        signature_hex="",
                    )
                )
                bundles_by_window.setdefault(int(window_index), []).append(attestation)
        for window_index, attestations in sorted(bundles_by_window.items()):
            transaction = self._build_reward_attestation_bundle_transaction(
                epoch_index=current_epoch_index,
                bundle_window_index=window_index,
                bundle_submitter_node_id=self.reward_automation.node_id,
                attestations=attestations,
            )
            await self.submit_transaction(transaction)
            for attestation in attestations:
                self._reward_submitted_attestation_identities.add(
                    (
                        attestation.epoch_index,
                        attestation.check_window_index,
                        attestation.candidate_node_id,
                        attestation.verifier_node_id,
                    )
                )
            self.logger.info(
                "auto reward attestation submitted node_id=%s epoch=%s window=%s attestation_count=%s txid=%s",
                self.reward_automation.node_id,
                current_epoch_index,
                window_index,
                len(attestations),
                transaction.txid(),
            )
            return

    def _build_reward_attestation_bundle_transaction(
        self,
        *,
        epoch_index: int,
        bundle_window_index: int,
        bundle_submitter_node_id: str,
        attestations: list[RewardAttestation],
    ) -> Transaction:
        """Build one native reward attestation bundle transaction."""

        return Transaction(
            version=1,
            inputs=(),
            outputs=(),
            metadata={
                "kind": "reward_attestation_bundle",
                "epoch_index": str(epoch_index),
                "bundle_window_index": str(bundle_window_index),
                "bundle_submitter_node_id": str(bundle_submitter_node_id),
                "attestation_count": str(len(attestations)),
                "attestations_json": json.dumps(
                    [
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
                        for attestation in attestations
                    ],
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            },
        )

    def connected_peer_count(self) -> int:
        """Return the number of active handshaken peer sessions."""

        return sum(1 for session in self._sessions if session.state.handshake_complete and not session.state.closed)

    async def _connect_loop(self) -> None:
        """Keep outbound peer connections alive."""

        while self._running:
            for peer in list(self._desired_outbound_peers()):
                if self._has_active_endpoint(peer):
                    continue
                if self._is_peer_currently_banned(peer.host, peer.port):
                    self.logger.debug("outbound connect skipped for banned peer=%s:%s", peer.host, peer.port)
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
                    self._pending_outbound_peers.add(peer)
                    await self._connect_outbound(peer)
                except Exception as exc:
                    self.logger.debug("outbound connect failed peer=%s:%s error=%s", peer.host, peer.port, exc)
                    self._register_peer_failure(peer, error=exc, penalty=20)
                finally:
                    self._pending_outbound_peers.discard(peer)
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
        if self._is_peer_currently_banned(endpoint.host, endpoint.port):
            self.logger.info("inbound connection rejected peer=%s:%s reason=temporary_ban", endpoint.host, endpoint.port)
            writer.close()
            await writer.wait_closed()
            return
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
                self.logger.debug("duplicate peer connection rejected node_id=%s", remote.node_id)
                error = DuplicateConnectionError("Duplicate peer connection.")
                await session.close(reason=str(error), error=error)
                return

            self._sessions_by_node_id[remote.node_id] = session
            handle = self._sessions.get(session)
            if handle is not None and handle.endpoint is not None:
                self.service.add_peer(
                    handle.endpoint.host,
                    handle.endpoint.port,
                    source=self._configured_peer_source(handle.endpoint),
                )
            endpoint = self._session_endpoint(session, handle)
            observation_direction = "inbound" if session.inbound else "outbound"
            observation_source = "discovered"
            endpoint_reusable = False
            if handle is not None and endpoint is not None:
                canonical_endpoint = self._canonicalize_reusable_inbound_endpoint(
                    endpoint,
                    inbound=session.inbound,
                    node_id=remote.node_id,
                )
                if canonical_endpoint is not None:
                    if canonical_endpoint != endpoint:
                        self.logger.info(
                            "canonicalized inbound peer endpoint source=%s:%s reusable=%s:%s",
                            endpoint.host,
                            endpoint.port,
                            canonical_endpoint.host,
                            canonical_endpoint.port,
                        )
                    handle.endpoint = canonical_endpoint
                    handle.reusable_endpoint = True
                    endpoint = canonical_endpoint
                    observation_direction = None
                    endpoint_reusable = True
                elif handle.outbound and endpoint is not None:
                    observation_source = self._configured_peer_source(endpoint)
                    endpoint_reusable = True
            if endpoint is not None:
                existing = self._known_peer_info(endpoint.host, endpoint.port)
                self.service.record_peer_observation(
                    host=endpoint.host,
                    port=endpoint.port,
                    source=observation_source if existing is None or existing.source is None else existing.source,
                    direction=observation_direction,
                    handshake_complete=True,
                    last_success=self.service.time_provider(),
                    success_count=1 if existing is None or existing.success_count is None else existing.success_count + 1,
                    last_known_height=remote.start_height,
                    node_id=remote.node_id,
                    score=self._updated_peer_score(endpoint.host, endpoint.port, delta=1),
                    reconnect_attempts=0,
                    backoff_until=0,
                    last_error=None,
                    protocol_error_class=None,
                    session_started_at=self.service.time_provider(),
                )
                if endpoint_reusable:
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
            if self.headers_sync_enabled:
                await self._drive_header_sync()
            else:
                await self._request_headers(session)
            await self._send_known_peers(session)
            await self._announce_current_mempool(session)
            self._update_sync_status()
        except Exception as exc:
            self.logger.info("post-handshake bootstrap failed peer=%s error=%s", self._format_peer_for_logs(session), exc)
            await session.close(reason="Post-handshake bootstrap failed.", error=exc)
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
                self._apply_session_penalty(session, error=error, penalty=25)
                await session.close(reason=str(error), error=error)
                await self._drop_session(session)
                return
            handle = self._sessions.get(session)
            if handle is not None:
                handle.headers_sync_active = False
            try:
                ingest = self.sync_manager.ingest_headers(
                    message.payload.headers,
                    peer_id=self._sync_peer_id(session),
                )
            except (StatelessValidationError, ContextualValidationError, ValueError) as exc:
                error = ProtocolError(f"invalid headers: {exc}")
                self._apply_session_penalty(session, error=error, penalty=self._SEVERE_MISBEHAVIOR_DELTA)
                await session.close(reason=str(error), error=error)
                await self._drop_session(session)
                return
            if handle is not None:
                handle.headers_contributed += ingest.headers_received
            if ingest.headers_received > 0 or ingest.parent_unknown is not None or ingest.missing_block_hashes:
                self.logger.info(
                    "headers received peer=%s count=%s stored=%s missing_blocks=%s best_tip=%s best_height=%s continue=%s",
                    self._format_peer_for_logs(session),
                    len(message.payload.headers),
                    ingest.headers_received,
                    len(ingest.missing_block_hashes),
                    ingest.best_tip_hash,
                    ingest.best_tip_height,
                    ingest.needs_more_headers,
                )
            else:
                self.logger.debug(
                    "headers received peer=%s count=%s stored=%s missing_blocks=%s best_tip=%s best_height=%s continue=%s",
                    self._format_peer_for_logs(session),
                    len(message.payload.headers),
                    ingest.headers_received,
                    len(ingest.missing_block_hashes),
                    ingest.best_tip_hash,
                    ingest.best_tip_height,
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
                self._begin_sync_tracking(
                    session,
                    ingest.best_tip_height if ingest.best_tip_height is not None else self._sync_target_height(session),
                    total_missing_blocks=len(ingest.missing_block_hashes),
                )
                self._log_sync_progress(session, force=True)
                if not self.headers_sync_enabled:
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
            self._update_sync_status()
            return

        if message.command == "getblocks":
            await session.send_message(MessageEnvelope(command="inv", payload=self.service.handle_getblocks(message.payload)))
            return

        if message.command == "inv":
            if len(message.payload.items) > self.max_inventory_items:
                error = ProtocolError("inventory message exceeded limit")
                self._apply_session_penalty(session, error=error, penalty=25)
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
                self._apply_session_penalty(session, error=error, penalty=25)
                await session.close(reason=str(error), error=error)
                await self._drop_session(session)
                return
            block_requests = [item.object_hash for item in message.payload.items if item.object_type == "block"]
            tx_requests = sum(1 for item in message.payload.items if item.object_type == "tx")
            served_blocks = 0
            served_txs = 0
            for item in message.payload.items:
                if item.object_type == "block":
                    block = self.service.get_block_by_hash(item.object_hash)
                    if block is not None:
                        served_blocks += 1
                        await session.send_message(MessageEnvelope(command="block", payload=BlockMessage(block=block)))
                elif item.object_type == "tx":
                    transaction = self.service.get_transaction(item.object_hash)
                    if transaction is not None:
                        served_txs += 1
                        await session.send_message(MessageEnvelope(command="tx", payload=TransactionMessage(transaction=transaction)))
            if block_requests or tx_requests:
                self.logger.info(
                    "served getdata peer=%s requested_blocks=%s served_blocks=%s requested_txs=%s served_txs=%s first_block=%s",
                    self._format_peer_for_logs(session),
                    len(block_requests),
                    served_blocks,
                    tx_requests,
                    served_txs,
                    block_requests[0] if block_requests else None,
                )
            return

        if message.command == "block":
            block_hash = message.payload.block.block_hash()
            self.logger.info(
                "block received peer=%s block=%s inflight_before=%s",
                self._format_peer_for_logs(session),
                block_hash,
                len(self._sessions.get(session).inflight_block_hashes) if self._sessions.get(session) is not None else 0,
            )
            try:
                result = self.sync_manager.receive_block(message.payload.block)
            except ValidationError as exc:
                self.logger.debug("peer sent invalid block: %s", exc)
                typed_error = InvalidBlockError(f"invalid block: {exc}")
                self._apply_session_penalty(session, error=typed_error, penalty=self._SEVERE_MISBEHAVIOR_DELTA)
                await session.close(reason=str(typed_error), error=typed_error)
                await self._drop_session(session)
                return
            self._clear_inflight_block_hash(result.block_hash)
            handle = self._sessions.get(session)
            if handle is not None:
                handle.blocks_contributed += result.accepted_blocks
                handle.block_stall_count = 0
                handle.last_block_progress_at = asyncio.get_running_loop().time()
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
            self.logger.info(
                "block processed peer=%s block=%s accepted_blocks=%s parent_unknown=%s reorged=%s inflight_after=%s",
                self._format_peer_for_logs(session),
                result.block_hash,
                result.accepted_blocks,
                result.parent_unknown,
                result.reorged,
                len(self._sessions.get(session).inflight_block_hashes) if self._sessions.get(session) is not None else 0,
            )
            await self._broadcast_inventory(
                InventoryVector(object_type="block", object_hash=result.block_hash),
                exclude=session,
            )
            self._update_sync_status()
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
            self._relayed_mempool_txids.add(accepted.transaction.txid())
            await self._broadcast_inventory(
                InventoryVector(object_type="tx", object_hash=accepted.transaction.txid()),
                exclude=session,
            )
            return

        if message.command == "getaddr":
            if not self.peer_discovery_enabled:
                return
            await self._send_known_peers(session)
            return

        if message.command == "addr":
            if not self.peer_discovery_enabled:
                return
            if len(message.payload.addresses) > self.max_addr_records:
                error = ProtocolError("addr message exceeded limit")
                self._apply_session_penalty(session, error=error, penalty=25)
                await session.close(reason=str(error), error=error)
                await self._drop_session(session)
                return
            accepted = 0
            for address in message.payload.addresses:
                announced = OutboundPeer(address.host, address.port)
                canonical_announced = self._canonicalize_announced_peer_endpoint(announced)
                if canonical_announced is None:
                    continue
                if self._is_local_listener_alias(canonical_announced):
                    continue
                if self._is_known_peer_alias(canonical_announced):
                    continue
                self._outbound_targets[(canonical_announced.host, canonical_announced.port)] = canonical_announced
                self._outbound_target_sources[(canonical_announced.host, canonical_announced.port)] = "discovered"
                self.service.add_peer(canonical_announced.host, canonical_announced.port, source="discovered")
                accepted += 1
            self._trim_peerbook_to_capacity()
            self.logger.debug("peer announced addresses count=%s accepted=%s", len(message.payload.addresses), accepted)
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

    async def _sync_scheduler_loop(self) -> None:
        """Drive headers-first sync, block scheduling, and stall reassignment."""

        while self._running:
            try:
                await self._expire_stalled_block_requests()
                await self._drive_header_sync()
                await self._dispatch_block_downloads()
            except Exception as exc:  # noqa: BLE001
                self.logger.debug("sync scheduler loop failed: %s", exc)
            self._update_sync_status()
            await asyncio.sleep(self._SYNC_SCHEDULER_INTERVAL)

    async def _drive_header_sync(self) -> None:
        """Request headers from a bounded set of suitable peers."""

        now = asyncio.get_running_loop().time()
        for session, handle in list(self._sessions.items()):
            if not handle.headers_sync_active:
                continue
            if handle.last_headers_requested_at <= 0:
                continue
            if (now - handle.last_headers_requested_at) < self.block_request_timeout_seconds:
                continue
            handle.headers_sync_active = False
            error = ProtocolError("headers request stalled")
            self._apply_session_penalty(session, error=error, penalty=10)
            self.logger.info("sync headers request stalled peer=%s action=deprioritize", self._format_peer_for_logs(session))
        active = sum(
            1
            for handle in self._sessions.values()
            if handle.headers_sync_active and not handle.protocol.state.closed and handle.protocol.state.handshake_complete
        )
        for session in self._eligible_header_sync_sessions():
            if active >= self.headers_sync_parallel_peers:
                break
            handle = self._sessions.get(session)
            if handle is None or handle.headers_sync_active:
                continue
            await self._request_headers(session)
            active += 1

    async def _dispatch_block_downloads(self) -> None:
        """Assign block downloads across multiple healthy peers."""

        sessions = self._eligible_block_download_sessions()
        if not sessions:
            return
        peer_ids = tuple(self._sync_peer_id(session) for session in sessions)
        now = asyncio.get_running_loop().time()
        assignments = self.sync_manager.reserve_block_downloads(
            peer_ids=peer_ids,
            max_window_size=self.block_download_window_size,
            max_inflight_per_peer=self.block_max_inflight_per_peer,
            timeout_seconds=self.block_request_timeout_seconds,
            now=now,
        )
        if not assignments:
            return
        grouped: dict[str, list[InventoryVector]] = {}
        for assignment in assignments:
            grouped.setdefault(assignment.peer_id, []).append(
                InventoryVector(object_type="block", object_hash=assignment.block_hash)
            )
        for session in sessions:
            peer_id = self._sync_peer_id(session)
            items = grouped.get(peer_id)
            if not items:
                continue
            handle = self._sessions.get(session)
            if handle is None:
                continue
            for item in items:
                handle.inflight_block_hashes.add(item.object_hash)
            for start in range(0, len(items), self.max_inventory_items):
                batch = tuple(items[start : start + self.max_inventory_items])
                self.logger.info(
                    "requesting blocks peer=%s batch_count=%s first_block=%s last_block=%s",
                    self._format_peer_for_logs(session),
                    len(batch),
                    batch[0].object_hash if batch else None,
                    batch[-1].object_hash if batch else None,
                )
                await session.send_message(MessageEnvelope(command="getdata", payload=GetDataMessage(items=batch)))
            self.logger.info(
                "sync scheduled block downloads peer=%s count=%s inflight=%s",
                self._format_peer_for_logs(session),
                len(items),
                len(handle.inflight_block_hashes),
            )

    async def _expire_stalled_block_requests(self) -> None:
        """Expire stalled block requests and reassign them on the next scheduler tick."""

        now = asyncio.get_running_loop().time()
        expired = self.sync_manager.expire_block_requests(now=now)
        for request in expired:
            session = self._session_for_sync_peer(request.peer_id)
            handle = None if session is None else self._sessions.get(session)
            if handle is not None:
                handle.inflight_block_hashes.discard(request.block_hash)
            self.logger.info(
                "sync block request stalled peer=%s block=%s attempt=%s action=reassign",
                request.peer_id,
                request.block_hash,
                request.attempt,
            )
            if session is None or handle is None:
                continue
            if self._should_tolerate_stalled_block_request(handle=handle, now=now):
                self.logger.info(
                    "sync stall tolerated peer=%s block=%s recent_progress_seconds=%s",
                    request.peer_id,
                    request.block_hash,
                    round(now - handle.last_block_progress_at, 3),
                )
                continue
            handle.block_stall_count += 1
            penalty = BlockRequestStalledError("block request stalled")
            self._apply_session_penalty(session, error=penalty, penalty=10)
            if handle.block_stall_count >= self._BLOCK_STALL_DISCONNECT_THRESHOLD:
                await session.close(reason=str(penalty), error=penalty)
                await self._drop_session(session)

    def _eligible_header_sync_sessions(self) -> list[PeerProtocol]:
        """Return peers eligible to contribute headers-first sync."""

        return sorted(
            [
                session
                for session in self._sessions
                if self._session_can_contribute_headers(session)
            ],
            key=self._sync_session_rank_key,
        )

    def _eligible_block_download_sessions(self) -> list[PeerProtocol]:
        """Return peers eligible to serve block downloads."""

        best_header_height = self.sync_manager.best_header_height()
        sessions = [
            session
            for session in self._sessions
            if self._session_can_download_blocks(session, best_header_height=best_header_height)
        ]
        return sorted(sessions, key=self._sync_session_rank_key)

    def _session_can_contribute_headers(self, session: PeerProtocol) -> bool:
        """Return whether one session should be queried for headers."""

        if session.state.closed or not session.state.handshake_complete:
            return False
        handle = self._sessions.get(session)
        if handle is None:
            return False
        remote = session.state.remote_version
        if remote is None:
            return False
        local_tip = self.service.chain_tip()
        local_height = -1 if local_tip is None else local_tip.height
        best_header_height = self.sync_manager.best_header_height()
        current_height = max(local_height, -1 if best_header_height is None else best_header_height)
        return remote.start_height >= current_height + self.headers_sync_start_height_gap_threshold

    def _session_can_download_blocks(self, session: PeerProtocol, *, best_header_height: int | None) -> bool:
        """Return whether one session can be used for block download work."""

        if session.state.closed or not session.state.handshake_complete:
            return False
        if best_header_height is None:
            return False
        handle = self._sessions.get(session)
        if handle is None:
            return False
        if len(handle.inflight_block_hashes) >= self.block_max_inflight_per_peer:
            return False
        remote = session.state.remote_version
        if remote is None:
            return False
        advertised_height = max(remote.start_height, self._sync_target_height(session))
        required_height = self.sync_manager.block_download_window_end_height(
            max_window_size=self.block_download_window_size
        )
        if required_height is None:
            required_height = best_header_height
        return advertised_height >= required_height or advertised_height >= max(0, required_height - 1)

    def _sync_session_rank_key(self, session: PeerProtocol) -> tuple[int, int, int, str]:
        """Prefer healthier peers for header and block sync work."""

        handle = self._sessions.get(session)
        endpoint = self._session_endpoint(session, handle)
        if endpoint is None:
            return (1, 0, 0, "unknown")
        info = self._known_peer_info(endpoint.host, endpoint.port)
        success_count = 0 if info is None or info.success_count is None else info.success_count
        score = 0 if info is None or info.score is None else info.score
        stall_count = 0 if handle is None else handle.block_stall_count
        return (stall_count, -success_count, -score, f"{endpoint.host}:{endpoint.port}")

    def _sync_peer_id(self, session: PeerProtocol) -> str:
        """Return a stable sync scheduler identifier for one session."""

        remote = session.state.remote_version
        if remote is not None:
            return remote.node_id
        handle = self._sessions.get(session)
        endpoint = self._session_endpoint(session, handle)
        if endpoint is not None:
            return f"{endpoint.host}:{endpoint.port}"
        return f"session:{id(session)}"

    def _session_for_sync_peer(self, peer_id: str) -> PeerProtocol | None:
        """Return the active session matching one scheduler peer identifier."""

        for session in self._sessions:
            if self._sync_peer_id(session) == peer_id:
                return session
        return None

    def _clear_inflight_block_hash(self, block_hash: str) -> None:
        """Remove one completed block request from all session handles."""

        for handle in self._sessions.values():
            handle.inflight_block_hashes.discard(block_hash)

    def _update_sync_status(self) -> None:
        """Publish the latest sync snapshot through the service diagnostics surface."""

        payload = self.sync_manager.sync_status()
        payload["current_sync_peers"] = tuple(self._current_sync_peers())
        self.service.set_runtime_sync_status(payload)
        phase = str(payload.get("phase", payload.get("mode", "idle")))
        if phase != self._last_logged_sync_phase:
            self._last_logged_sync_phase = phase
            self.logger.info(
                "sync phase changed phase=%s local_height=%s remote_height=%s current_sync_peers=%s",
                phase,
                payload.get("local_height"),
                payload.get("remote_height"),
                len(payload["current_sync_peers"]),
            )

    def _current_sync_peers(self) -> list[dict[str, object]]:
        """Return active sync peer diagnostics for status surfaces."""

        peers: list[dict[str, object]] = []
        for handle in self._sessions.values():
            protocol = handle.protocol
            if protocol.state.closed or not protocol.state.handshake_complete:
                continue
            if not handle.headers_sync_active and not handle.inflight_block_hashes and handle.sync_target_height is None:
                continue
            remote_version = protocol.state.remote_version
            peers.append(
                {
                    "node_id": None if remote_version is None else remote_version.node_id,
                    "direction": "outbound" if handle.outbound else "inbound",
                    "endpoint": None if handle.endpoint is None else f"{handle.endpoint.host}:{handle.endpoint.port}",
                    "sync_target_height": handle.sync_target_height,
                    "headers_sync_active": handle.headers_sync_active,
                    "inflight_block_count": len(handle.inflight_block_hashes),
                    "blocks_contributed": handle.blocks_contributed,
                    "headers_contributed": handle.headers_contributed,
                }
            )
        return peers

    def _is_catchup_sync_active(self) -> bool:
        """Return whether the node is still catching up to the best known header tip."""

        best_header_height = self.sync_manager.best_header_height()
        if best_header_height is None:
            return False
        local_tip = self.service.chain_tip()
        local_height = -1 if local_tip is None else local_tip.height
        return best_header_height > local_height

    def _should_tolerate_stalled_block_request(self, *, handle: SessionHandle, now: float) -> bool:
        """Return whether one stalled request should be tolerated during active catch-up."""

        if not self._is_catchup_sync_active():
            return False
        if handle.last_block_progress_at <= 0.0:
            return False
        grace_seconds = max(
            self.block_request_timeout_seconds,
            self.read_timeout,
        ) * self._INITIAL_SYNC_STALL_GRACE_MULTIPLIER
        return (now - handle.last_block_progress_at) <= grace_seconds

    async def _request_headers(self, session: PeerProtocol, *, locator_hashes: tuple[str, ...] | None = None) -> None:
        """Request headers from a peer using the local block locator."""

        handle = self._sessions.get(session)
        if handle is not None:
            handle.headers_sync_active = True
            handle.last_headers_requested_at = asyncio.get_running_loop().time()
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

        if not self.peer_discovery_enabled:
            return
        handle = self._sessions.get(session)
        if handle is not None:
            now = asyncio.get_running_loop().time()
            if (
                handle.addr_relay_window_started_at <= 0
                or (now - handle.addr_relay_window_started_at) >= self.peer_addr_relay_interval_seconds
            ):
                handle.addr_relay_window_started_at = now
                handle.addr_relay_entries_sent = 0
            remaining = self.peer_addr_relay_limit_per_interval - handle.addr_relay_entries_sent
            if remaining <= 0:
                peer_label = "unknown"
                try:
                    peer_label = self._format_peer_for_logs(session)
                except Exception:  # noqa: BLE001
                    peer_label = "unknown"
                self.logger.debug("addr relay skipped peer=%s reason=rate_limited", peer_label)
                return
        else:
            remaining = self.peer_addr_max_per_message
        remote = session.state.remote_version
        relay_cap = min(self.peer_addr_max_per_message, remaining)
        advertised_peers = sorted(
            (
                peer
                for peer in self.service.list_peers()
                if self._is_advertisable_peer(peer)
                and not self._is_stale_peer(peer)
                and peer.node_id != self.node_id
                and (remote is None or peer.node_id != remote.node_id)
            ),
            key=lambda peer: (
                0 if (peer.success_count or 0) > 0 else 1,
                {"manual": 0, "seed": 1, "discovered": 2}.get(peer.source, 3),
                -(0 if peer.success_count is None else peer.success_count),
                -(0 if peer.score is None else peer.score),
                peer.host,
                peer.port,
            ),
        )[:relay_cap]
        addresses = tuple(
            PeerAddress(host=peer.host, port=peer.port, services=0, timestamp=self.service.time_provider())
            for peer in advertised_peers
        )
        await session.send_message(MessageEnvelope(command="addr", payload=AddrMessage(addresses=addresses)))
        if handle is not None:
            handle.addr_relay_entries_sent += len(addresses)
        self.logger.debug("sent addr records count=%s", len(addresses))

    async def _drop_session(self, session: PeerProtocol) -> None:
        """Remove a session from runtime tracking."""

        handle = self._sessions.pop(session, None)
        peer_id = self._sync_peer_id(session)
        released_requests = self.sync_manager.release_peer_requests(peer_id)
        for request in released_requests:
            self.logger.info(
                "sync block request released peer=%s block=%s reason=session_dropped",
                peer_id,
                request.block_hash,
            )
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
                direction=(
                    None
                    if handle is None or handle.reusable_endpoint
                    else ("outbound" if handle.outbound else "inbound")
                ),
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
            log = self.logger.debug if self._is_low_value_session_drop(current_error_obj or current_error) else self.logger.info
            log(
                "session dropped peer=%s:%s handshake_complete=%s error=%s disconnects=%s",
                endpoint.host,
                endpoint.port,
                session.state.handshake_complete,
                current_error,
                0 if existing is None or existing.disconnect_count is None else existing.disconnect_count + 1,
            )
        if handle is None:
            self._update_sync_status()
            return
        if remote is not None and self._sessions_by_node_id.get(remote.node_id) is session:
            del self._sessions_by_node_id[remote.node_id]
        self._update_sync_status()

    def _has_active_endpoint(self, peer: OutboundPeer) -> bool:
        """Return whether an active outbound session already targets the endpoint."""

        for pending in self._pending_outbound_peers:
            if pending == peer or self._peers_equivalent(pending, peer):
                return True
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

    def _configured_peer_source(self, peer: OutboundPeer) -> str:
        """Return the configured source classification for one explicit target."""

        return self._outbound_target_sources.get((peer.host, peer.port), "manual")

    def _persist_configured_peer_targets(self) -> None:
        """Persist explicitly configured peer targets with their source classification."""

        for peer in self._outbound_targets.values():
            self.service.add_peer(peer.host, peer.port, source=self._configured_peer_source(peer))

    def _desired_outbound_peers(self) -> list[OutboundPeer]:
        """Return configured and persisted peers excluding the local listener."""

        persisted_peers = [peer for peer in self.service.list_peers() if self._is_dialable_peer(peer)]
        peers = set()
        use_configured_fallback = True
        if self.peer_discovery_startup_prefer_persisted:
            healthy_persisted = [peer for peer in persisted_peers if self._is_healthy_persisted_peer(peer)]
            if healthy_persisted:
                peers.update(OutboundPeer(peer.host, peer.port) for peer in healthy_persisted)
                use_configured_fallback = False
            else:
                peers.update(OutboundPeer(peer.host, peer.port) for peer in persisted_peers)
        else:
            peers.update(OutboundPeer(peer.host, peer.port) for peer in persisted_peers)
        if use_configured_fallback:
            peers.update(self._outbound_targets.values())
        deduped: dict[str, OutboundPeer] = {}
        unnamed: list[OutboundPeer] = []
        for peer in sorted(peers, key=self._outbound_peer_rank_key):
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
        return sorted([*self._dedupe_unidentified_outbound_peers(unnamed), *deduped.values()], key=self._outbound_peer_rank_key)

    def _is_healthy_persisted_peer(self, peer) -> bool:
        """Return whether one persisted peer is good enough to outrank manual seed fallback."""

        if peer.source not in {"manual", "seed", "discovered"}:
            return False
        if self._is_peer_currently_banned(peer.host, peer.port):
            return False
        if self._is_stale_peer(peer):
            return False
        if peer.backoff_until is not None and peer.backoff_until > self.service.time_provider():
            return False
        if (peer.success_count or 0) > 0:
            return True
        return (peer.handshake_complete is True) or ((peer.score or 0) > 0)

    def _outbound_peer_rank_key(self, peer: OutboundPeer) -> tuple[int, int, int, int, str, int]:
        """Prefer reliable persisted peers before fallback seed/manual endpoints."""

        info = self._known_peer_info(peer.host, peer.port)
        source = None if info is None else info.source
        source_rank = {"discovered": 0, "manual": 1, "seed": 2}.get(source, 3)
        success_count = 0 if info is None or info.success_count is None else info.success_count
        score = 0 if info is None or info.score is None else info.score
        last_success = 0 if info is None or info.last_success is None else info.last_success
        return (source_rank, -success_count, -score, -last_success, peer.host, peer.port)

    def _is_dialable_peer(self, peer) -> bool:
        """Return whether a persisted peer is safe to use for outbound dialing."""

        if peer.direction == "inbound" or peer.port <= 0:
            return False
        if self._is_peer_currently_banned(peer.host, peer.port):
            return False
        if peer.source in {"manual", "seed"}:
            return self._is_valid_peer_host(peer.host)
        if peer.source == "discovered":
            return self._is_reusable_discovered_peer(peer) and self._is_persisted_peer_host_dialable(peer.host)
        return self._is_persisted_peer_host_dialable(peer.host)

    def _is_reusable_discovered_peer(self, peer) -> bool:
        """Return whether one discovered endpoint is reusable as a persisted peer candidate."""

        default_port = get_network_config(peer.network).default_p2p_port
        if peer.port == default_port:
            return True
        return peer.handshake_complete is True or (peer.success_count or 0) > 0

    def _is_persisted_peer_host_dialable(self, host: str) -> bool:
        """Return whether one persisted peer host should be reused for outbound dialing."""

        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            # Hostnames remain eligible; explicit configured peers are handled separately.
            return True
        return address.is_global

    def _canonicalize_reusable_inbound_endpoint(
        self,
        endpoint: PeerEndpoint,
        *,
        inbound: bool,
        node_id: str | None = None,
    ) -> OutboundPeer | None:
        """Return a reusable public endpoint for an inbound peer when it is safe to do so."""

        if not inbound or endpoint.port <= 0:
            return None
        try:
            address = ipaddress.ip_address(endpoint.host)
        except ValueError:
            return None
        if not address.is_global:
            return None
        canonical = OutboundPeer(endpoint.host, get_network_config(self.service.network).default_p2p_port)
        existing = self._known_peer_info(canonical.host, canonical.port)
        if existing is not None and existing.node_id is not None and node_id is not None and existing.node_id != node_id:
            return None
        return canonical

    def _is_valid_peer_host(self, host: str) -> bool:
        """Return whether one advertised host string is syntactically reasonable."""

        if not host or len(host) > 253 or any(character.isspace() for character in host):
            return False
        try:
            ipaddress.ip_address(host)
        except ValueError:
            labels = host.split(".")
            if any(not label or len(label) > 63 for label in labels):
                return False
            for label in labels:
                if label.startswith("-") or label.endswith("-"):
                    return False
                if not all(character.isalnum() or character == "-" for character in label):
                    return False
            return True
        return True

    def _is_stale_peer(self, peer) -> bool:
        """Return whether one persisted peer should age out of the automatic peerbook."""

        if peer.source in {"manual", "seed"}:
            return False
        anchor = peer.last_success or peer.last_seen or peer.first_seen
        if anchor is None:
            return False
        return (self.service.time_provider() - anchor) >= self.peer_stale_after_seconds

    def _purge_stale_persisted_peers(self) -> None:
        """Drop stale discovered peers so startup candidate selection stays bounded."""

        stale_peers = [peer for peer in self.service.list_peers() if self._is_stale_peer(peer)]
        for peer in stale_peers:
            self._outbound_targets.pop((peer.host, peer.port), None)
            self.service.remove_peer(peer.host, peer.port)
            self.logger.info("removed stale peer=%s:%s source=%s", peer.host, peer.port, peer.source)

    def _peerbook_trim_sort_key(self, peer) -> tuple[int, int, int, int, int, str, int]:
        """Order peers from worst to best when trimming the persistent peerbook."""

        source_rank = {"discovered": 0, "seed": 1, "manual": 2}.get(peer.source, -1)
        banned_rank = 1 if self._is_peer_currently_banned(peer.host, peer.port) else 0
        success_count = 0 if peer.success_count is None else peer.success_count
        score = 0 if peer.score is None else peer.score
        last_seen = 0 if peer.last_seen is None else peer.last_seen
        return (source_rank, banned_rank, success_count, score, last_seen, peer.host, peer.port)

    def _trim_peerbook_to_capacity(self) -> None:
        """Keep the persistent peerbook within the configured capacity."""

        peers = self.service.list_peers()
        if len(peers) <= self.peerbook_max_size:
            return
        removable = sorted(
            [peer for peer in peers if peer.source != "manual"],
            key=self._peerbook_trim_sort_key,
        )
        overflow = len(peers) - self.peerbook_max_size
        for peer in removable[:overflow]:
            self._outbound_targets.pop((peer.host, peer.port), None)
            self.service.remove_peer(peer.host, peer.port)
            self.logger.info("trimmed peerbook peer=%s:%s source=%s", peer.host, peer.port, peer.source)

    def _is_announced_peer_dialable(self, peer: OutboundPeer) -> bool:
        """Return whether one peer announced through addr should be accepted."""

        if peer.port <= 0 or not self._is_valid_peer_host(peer.host):
            return False
        return self._is_persisted_peer_host_dialable(peer.host)

    def _canonicalize_announced_peer_endpoint(self, peer: OutboundPeer) -> OutboundPeer | None:
        """Normalize one announced peer so transient ports do not enter the persistent peerbook."""

        if not self._is_announced_peer_dialable(peer):
            return None
        default_port = get_network_config(self.service.network).default_p2p_port
        if peer.port == default_port:
            return peer
        if self._host_is_literal_ip(peer.host):
            return OutboundPeer(peer.host, default_port)
        return None

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

        if peer.direction == "inbound" or peer.port <= 0 or self._is_peer_currently_banned(peer.host, peer.port):
            return False
        if peer.source == "discovered" and not self._is_reusable_discovered_peer(peer):
            return False
        return True

    def _decayed_misbehavior_state(self, info, *, now: int) -> tuple[int, int]:
        """Return the peer misbehavior score after applying passive decay."""

        score = 0 if info is None or info.misbehavior_score is None else max(0, info.misbehavior_score)
        updated_at = now if info is None or info.misbehavior_last_updated_at is None else info.misbehavior_last_updated_at
        if score <= 0:
            return 0, updated_at
        elapsed = max(0, now - updated_at)
        if elapsed < self.misbehavior_decay_interval_seconds:
            return score, updated_at
        decay_steps = elapsed // self.misbehavior_decay_interval_seconds
        decayed_score = max(0, score - (decay_steps * self.misbehavior_decay_step))
        return decayed_score, updated_at + (decay_steps * self.misbehavior_decay_interval_seconds)

    def _ban_state_for_peer(self, host: str, port: int) -> tuple[bool, int | None]:
        """Return whether one peer or host-equivalent alias is still banned."""

        now = self.service.time_provider()
        exact = self._known_peer_info(host, port)
        for candidate in [exact, *self.service.list_peers()]:
            if candidate is None or candidate.host != host:
                continue
            ban_until = candidate.ban_until
            if ban_until is not None and ban_until > now:
                return True, ban_until
        return False, None

    def _is_peer_currently_banned(self, host: str, port: int) -> bool:
        """Return whether one peer endpoint is under an active temporary ban."""

        banned, _ban_until = self._ban_state_for_peer(host, port)
        return banned

    def _observe_peer_misbehavior(
        self,
        *,
        host: str,
        port: int,
        event: str,
        delta: int,
        direction: str | None,
        handshake_complete: bool | None,
        last_known_height: int | None = None,
        node_id: str | None = None,
        reconnect_attempts: int | None = None,
        backoff_until: int | None = None,
        disconnect_count: int | None = None,
        session_started_at: int | None = None,
        last_error: str | None = None,
        protocol_error_class_name: str | None = None,
        score: int | None = None,
        force_disconnect: bool = False,
    ) -> str:
        """Apply one misbehavior event, persist it, and return the action taken."""

        info = self._known_peer_info(host, port)
        now = self.service.time_provider()
        current_score, updated_at = self._decayed_misbehavior_state(info, now=now)
        next_score = min(10_000, current_score + max(0, delta))
        source = None if info is None else info.source
        action = "observe"
        ban_until = None if info is None else info.ban_until
        if ban_until is not None and ban_until <= now:
            ban_until = None
        if next_score >= self.misbehavior_ban_threshold:
            action = "ban"
            ban_until = now + self.misbehavior_ban_duration_seconds
        elif force_disconnect or next_score >= self.misbehavior_disconnect_threshold:
            action = "disconnect"
            ban_until = None if ban_until is None or ban_until <= now else ban_until
        elif next_score >= self.misbehavior_warning_threshold:
            action = "warn"
            ban_until = None if ban_until is None or ban_until <= now else ban_until
        self.service.record_peer_observation(
            host=host,
            port=port,
            source=source,
            direction=direction,
            handshake_complete=handshake_complete,
            last_failure=now if last_error is not None else None,
            failure_count=(
                None if last_error is None else (1 if info is None or info.failure_count is None else info.failure_count + 1)
            ),
            last_known_height=last_known_height,
            node_id=node_id,
            score=score,
            reconnect_attempts=reconnect_attempts,
            backoff_until=backoff_until,
            last_error=last_error,
            last_error_at=now if last_error is not None else None,
            protocol_error_class=protocol_error_class_name,
            disconnect_count=disconnect_count,
            session_started_at=session_started_at,
            misbehavior_score=next_score,
            misbehavior_last_updated_at=now if next_score != current_score else updated_at,
            ban_until=ban_until,
            last_penalty_reason=event,
            last_penalty_at=now,
        )
        self._trim_peerbook_to_capacity()
        self.logger.info(
            "peer misbehavior peer=%s:%s event=%s score_delta=%s score=%s action=%s ban_until=%s",
            host,
            port,
            event,
            delta,
            next_score,
            action,
            ban_until,
        )
        return action

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

    def _begin_sync_tracking(
        self,
        session: PeerProtocol,
        target_height: int,
        *,
        total_missing_blocks: int | None = None,
    ) -> None:
        """Track one peer catch-up session so progress logs stay aggregated."""

        handle = self._sessions.get(session)
        if handle is None:
            return
        local_tip = self.service.chain_tip()
        local_height = -1 if local_tip is None else local_tip.height
        target_height = max(target_height, local_height)
        if handle.sync_target_height is None or target_height > handle.sync_target_height:
            handle.sync_target_height = target_height
        if total_missing_blocks is not None:
            handle.sync_total_missing_blocks = max(0, total_missing_blocks)
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
                    "sync complete peer=%s final_local_height=%s peer_target_height=%s best_header_height=%s",
                    self._format_peer_for_logs(session),
                    local_height,
                    target_height,
                    None if self.sync_manager.best_header_record() is None else self.sync_manager.best_header_record().height,
                )
            handle.sync_target_height = None
            handle.sync_total_missing_blocks = None
            handle.sync_next_log_height = None
            return

        if not force and handle.sync_next_log_height is not None and local_height < handle.sync_next_log_height:
            return

        remaining_blocks = max(0, target_height - local_height)
        total_blocks = (
            max(0, handle.sync_total_missing_blocks)
            if handle.sync_total_missing_blocks is not None
            else remaining_blocks
        )
        synced_blocks = max(0, total_blocks - remaining_blocks)
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

        local_tip = self.service.chain_tip()
        local_height = None if local_tip is None else local_tip.height
        if self._sync_in_progress(session):
            self.logger.debug(
                "%s peer=%s height=%s block=%s activated_tip=%s accepted_blocks=%s",
                "reorg applied" if reorged else "block applied",
                self._format_peer_for_logs(session),
                local_height,
                result.block_hash,
                result.activated_tip,
                result.accepted_blocks,
            )
            self._log_sync_progress(session)
            return

        self.logger.info(
            "%s peer=%s height=%s block=%s activated_tip=%s accepted_blocks=%s",
            "reorg applied" if reorged else "block applied",
            self._format_peer_for_logs(session),
            local_height,
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
            self.logger.debug("removed self-alias peer=%s:%s from outbound peer set", alias.host, alias.port)

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
            self.logger.debug("removed startup self-alias peer=%s:%s", peer.host, peer.port)

    def _purge_undialable_persisted_peers(self) -> None:
        """Drop persisted peer endpoints that should never be redialed automatically."""

        peers = [
            peer
            for peer in self.service.list_peers()
            if (
                (peer.source == "discovered" and not self._is_reusable_discovered_peer(peer))
                or (peer.source not in {"manual", "seed", "discovered"} and not self._is_persisted_peer_host_dialable(peer.host))
            )
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
            self.logger.debug("removed startup duplicate alias peer=%s:%s", peer.host, peer.port)

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
        classification = classify_peer_error(error)
        applied_penalty = self._penalty_for_error(error) if penalty is None else penalty
        new_score = self._updated_peer_score(peer.host, peer.port, delta=-applied_penalty)
        if self._should_penalize_as_misbehavior(error, handshake_complete=False):
            action = self._observe_peer_misbehavior(
                host=peer.host,
                port=peer.port,
                event=classification or "connect_failure",
                delta=applied_penalty,
                direction="outbound",
                handshake_complete=False,
                reconnect_attempts=attempts,
                backoff_until=backoff_until if backoff_until > now else now + 1,
                disconnect_count=0 if info is None or info.disconnect_count is None else info.disconnect_count + 1,
                last_error=error_text,
                protocol_error_class_name=classification,
                score=new_score,
            )
        else:
            action = "backoff"
            self.service.record_peer_observation(
                host=peer.host,
                port=peer.port,
                source=None if info is None else info.source,
                direction="outbound",
                handshake_complete=False,
                last_failure=now,
                failure_count=1 if info is None or info.failure_count is None else info.failure_count + 1,
                last_known_height=None if info is None else info.last_known_height,
                node_id=None if info is None else info.node_id,
                score=new_score,
                reconnect_attempts=attempts,
                backoff_until=backoff_until if backoff_until > now else now + 1,
                last_error=error_text,
                last_error_at=now,
                protocol_error_class=classification,
                disconnect_count=0 if info is None or info.disconnect_count is None else info.disconnect_count + 1,
                session_started_at=None if info is None else info.session_started_at,
                misbehavior_score=None if info is None else info.misbehavior_score,
                misbehavior_last_updated_at=None if info is None else info.misbehavior_last_updated_at,
                ban_until=None if info is None else info.ban_until,
                last_penalty_reason=None if info is None else info.last_penalty_reason,
                last_penalty_at=None if info is None else info.last_penalty_at,
            )
            self._trim_peerbook_to_capacity()
        log = self.logger.info if self._should_log_peer_failure_info(info, attempts=attempts, score=new_score) else self.logger.debug
        log(
            "peer backoff applied peer=%s:%s reconnect_attempts=%s backoff_until=%s score=%s action=%s error=%s",
            peer.host,
            peer.port,
            attempts,
            backoff_until if backoff_until > now else now + 1,
            new_score,
            action,
            error_text,
        )

    def _should_log_peer_failure_info(self, info, *, attempts: int, score: int) -> bool:
        """Keep terminally noisy reconnect churn out of INFO while preserving state changes."""

        if attempts <= 3:
            return True
        previous_score = 0 if info is None or info.score is None else info.score
        return previous_score > -100 >= score

    def _is_low_value_session_drop(self, error: Exception | str | None) -> bool:
        """Return whether one session drop is expected churn better kept out of INFO."""

        return classify_peer_error(error) == "duplicate_connection"

    def _should_penalize_as_misbehavior(self, error: Exception | str | None, *, handshake_complete: bool) -> bool:
        """Return whether one peer failure should contribute to misbehavior score."""

        if isinstance(error, BlockRequestStalledError):
            return False
        classification = classify_peer_error(error)
        if classification in {"wrong_network_magic", "checksum_error", "malformed_message", "invalid_block", "invalid_tx"}:
            return True
        return False

    def _apply_session_penalty(self, session: PeerProtocol, *, error: Exception | str, penalty: int) -> None:
        """Penalize a peer session using the observed endpoint."""

        handle = self._sessions.get(session)
        endpoint = self._session_endpoint(session, handle)
        if endpoint is None:
            return
        info = self._known_peer_info(endpoint.host, endpoint.port)
        error_text = str(error)
        classification = classify_peer_error(error)
        if self._should_penalize_as_misbehavior(error, handshake_complete=bool(session.state.handshake_complete)):
            self._observe_peer_misbehavior(
                host=endpoint.host,
                port=endpoint.port,
                event=classification or "protocol_violation",
                delta=penalty,
                direction=None if handle is None else ("outbound" if handle.outbound else "inbound"),
                handshake_complete=False if not session.state.handshake_complete else True,
                last_known_height=None if session.state.remote_version is None else session.state.remote_version.start_height,
                node_id=None if session.state.remote_version is None else session.state.remote_version.node_id,
                reconnect_attempts=None if info is None else info.reconnect_attempts,
                backoff_until=None if info is None else info.backoff_until,
                disconnect_count=None if info is None else info.disconnect_count,
                session_started_at=None if info is None else info.session_started_at,
                last_error=error_text,
                protocol_error_class_name=classification,
                score=self._updated_peer_score(endpoint.host, endpoint.port, delta=-penalty),
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
        delay_seconds = min(
            self.peer_retry_backoff_max_seconds,
            self.peer_retry_backoff_base_seconds * (2 ** min(attempts - 1, 5)),
        )
        now = self.service.time_provider()
        return attempts, now + max(1, int(delay_seconds))

    def _penalty_for_error(self, error: Exception | str) -> int:
        """Map transport/protocol errors to a small peer score penalty."""

        classification = protocol_error_class(error)
        if classification in {"wrong_network_magic", "checksum_error", "malformed_message", "invalid_block"}:
            return self._SEVERE_MISBEHAVIOR_DELTA
        if classification == "handshake_failed":
            return 25
        if classification == "timeout":
            return 10
        if classification == "duplicate_connection":
            return 1
        if classification == "invalid_tx":
            return 10
        if classification in {"connection_closed", "connection_failed"}:
            return 5
        return 5

    def _local_identity(self) -> LocalPeerIdentity:
        """Build the local identity used for the next session."""

        tip = self.service.chain_tip()
        return LocalPeerIdentity(
            node_id=self.node_id,
            network=self.service.network,
            start_height=0 if tip is None else tip.height,
            user_agent=f"/chipcoin-v2:{__version__}/",
            network_magic=get_network_config(self.service.network).magic,
        )

    def _spawn_task(self, coro, name: str) -> asyncio.Task:
        """Create and track a background task."""

        task = asyncio.create_task(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

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
