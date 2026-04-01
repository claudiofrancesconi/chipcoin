"""Minimal peer session protocol with handshake and liveness support."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from ...config import MAINNET_CONFIG
from ..messages import (
    EmptyPayload,
    MessageEnvelope,
    PingMessage,
    PongMessage,
    VersionMessage,
)
from .codec import CodecError, decode_message, encode_message
from .errors import (
    ConnectionClosedError,
    DuplicateConnectionError,
    HandshakeFailedError,
    MalformedMessageError,
    P2PError,
    ProtocolError,
    ProtocolTimeoutError,
)
from .transport import TCPTransport, Transport, TransportError, TransportTimeoutError


@dataclass(frozen=True)
class LocalPeerIdentity:
    """Static identity and handshake values advertised by a local peer."""

    node_id: str
    network: str
    start_height: int
    user_agent: str
    protocol_version: int = 1
    relay: bool = True
    network_magic: bytes = MAINNET_CONFIG.magic


@dataclass
class SessionState:
    """Runtime state for a single peer session."""

    version_sent: bool = False
    version_received: bool = False
    verack_sent: bool = False
    verack_received: bool = False
    handshake_complete: bool = False
    closed: bool = False
    remote_version: VersionMessage | None = None
    last_ping_nonce: int | None = None
    last_pong_nonce: int | None = None
    errors: list[str] = field(default_factory=list)
    error_causes: list[Exception] = field(default_factory=list)


class PeerProtocol:
    """Handle a single peer session over a transport."""

    def __init__(
        self,
        *,
        transport: Transport,
        identity: LocalPeerIdentity,
        inbound: bool,
        handshake_timeout: float = 5.0,
        on_message=None,
        on_handshake_complete=None,
    ) -> None:
        self.transport = transport
        self.identity = identity
        self.inbound = inbound
        self.handshake_timeout = handshake_timeout
        self.on_message = on_message
        self.on_handshake_complete = on_handshake_complete
        self.state = SessionState()
        self._reader_task: asyncio.Task[None] | None = None
        self._handshake_callback_task: asyncio.Task[None] | None = None
        self._closed_event = asyncio.Event()
        self._handshake_event = asyncio.Event()
        self._pong_waiters: dict[int, asyncio.Future[None]] = {}

    @classmethod
    async def connect(
        cls,
        host: str,
        port: int,
        *,
        identity: LocalPeerIdentity,
        connect_timeout: float = 5.0,
        read_timeout: float = 5.0,
        write_timeout: float = 5.0,
        handshake_timeout: float = 5.0,
    ) -> "PeerProtocol":
        """Open an outbound TCP session and complete the initial handshake."""

        transport = await TCPTransport.connect(
            host,
            port,
            connect_timeout=connect_timeout,
            read_timeout=read_timeout,
            write_timeout=write_timeout,
        )
        protocol = cls(
            transport=transport,
            identity=identity,
            inbound=False,
            handshake_timeout=handshake_timeout,
            on_message=None,
            on_handshake_complete=None,
        )
        await protocol.start()
        return protocol

    async def start(self) -> None:
        """Start the reader loop and complete the version/verack handshake."""

        self._reader_task = asyncio.create_task(self._reader_loop())
        if not self.inbound:
            await self._send_version()
        handshake_wait = asyncio.create_task(self._handshake_event.wait())
        closed_wait = asyncio.create_task(self._closed_event.wait())
        try:
            done, pending = await asyncio.wait(
                {handshake_wait, closed_wait},
                timeout=self.handshake_timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
        except TimeoutError as exc:
            error = HandshakeFailedError("Timed out waiting for handshake completion.")
            await self.close(reason="Handshake timed out.", error=error)
            raise error from exc
        finally:
            for task in (handshake_wait, closed_wait):
                if not task.done():
                    task.cancel()
            for task in (handshake_wait, closed_wait):
                if task.done() and not task.cancelled():
                    try:
                        task.result()
                    except Exception:
                        pass
        if self.state.handshake_complete:
            return
        if not done:
            error = HandshakeFailedError("Timed out waiting for handshake completion.")
            await self.close(reason="Handshake timed out.", error=error)
            raise error
        if handshake_wait in done:
            return
        if self.state.error_causes:
            raise self.state.error_causes[-1]
        error = self.state.errors[-1] if self.state.errors else "Peer connection closed before handshake completion."
        raise HandshakeFailedError(error)

    async def handle_message(self, message: MessageEnvelope) -> None:
        """Process a decoded P2P message."""

        if message.command == "version":
            await self._handle_version(message.payload)
            return
        if message.command == "verack":
            self.state.verack_received = True
            self._update_handshake_state()
            return
        if message.command == "ping":
            self.state.last_ping_nonce = message.payload.nonce
            await self.send_message(MessageEnvelope(command="pong", payload=PongMessage(nonce=message.payload.nonce)))
            return
        if message.command == "pong":
            self.state.last_pong_nonce = message.payload.nonce
            waiter = self._pong_waiters.pop(message.payload.nonce, None)
            if waiter is not None and not waiter.done():
                waiter.set_result(None)
            return
        if self.on_message is not None:
            await self.on_message(self, message)

    async def send_message(self, message: MessageEnvelope) -> None:
        """Encode and send a message over the transport."""

        await self.transport.send(encode_message(message, magic=self.identity.network_magic))

    async def ping(self, nonce: int, *, timeout: float = 5.0) -> None:
        """Send a ping and wait for the matching pong."""

        if self.state.closed:
            raise ConnectionClosedError("Cannot ping on a closed session.")
        loop = asyncio.get_running_loop()
        waiter: asyncio.Future[None] = loop.create_future()
        self._pong_waiters[nonce] = waiter
        await self.send_message(MessageEnvelope(command="ping", payload=PingMessage(nonce=nonce)))
        try:
            await asyncio.wait_for(waiter, timeout=timeout)
        except TimeoutError as exc:
            self._pong_waiters.pop(nonce, None)
            raise ProtocolTimeoutError("Timed out waiting for pong response.") from exc

    async def close(self, *, reason: str | None = None, error: Exception | None = None) -> None:
        """Close the session and underlying transport."""

        if self.state.closed:
            return
        self.state.closed = True
        if error is not None:
            self.state.error_causes.append(error)
        if reason is not None:
            self.state.errors.append(reason)
        elif error is not None:
            self.state.errors.append(str(error))
        if self._reader_task is not None and self._reader_task is not asyncio.current_task():
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass
        if (
            self._handshake_callback_task is not None
            and self._handshake_callback_task is not asyncio.current_task()
        ):
            self._handshake_callback_task.cancel()
            try:
                await self._handshake_callback_task
            except asyncio.CancelledError:
                pass
        for waiter in self._pong_waiters.values():
            if not waiter.done():
                waiter.cancel()
        self._pong_waiters.clear()
        await self.transport.close()
        self._closed_event.set()

    async def wait_closed(self) -> None:
        """Wait until the session has been closed."""

        await self._closed_event.wait()

    async def _reader_loop(self) -> None:
        """Continuously receive and dispatch frames until the session closes."""

        try:
            while not self.state.closed:
                try:
                    frame = await self.transport.receive()
                    message = decode_message(frame, expected_magic=self.identity.network_magic)
                    await self.handle_message(message)
                except TransportTimeoutError as exc:
                    if self.state.handshake_complete and not self.state.closed:
                        # Once the session is established, idle reads are not fatal by themselves.
                        # Liveness is enforced by the runtime ping/pong loop instead.
                        self.state.errors.append(str(exc))
                        continue
                    await self.close(reason=str(exc), error=exc)
                    return
        except asyncio.CancelledError:
            return
        except (P2PError, TransportError) as exc:
            await self.close(reason=str(exc), error=exc)

    async def _handle_version(self, payload: VersionMessage) -> None:
        """Handle peer version negotiation and verack exchange."""

        if self.state.version_received:
            raise HandshakeFailedError("Peer sent duplicate version message.")
        if payload.network != self.identity.network:
            raise HandshakeFailedError("Peer announced a different network.")

        self.state.remote_version = payload
        self.state.version_received = True

        if self.inbound and not self.state.version_sent:
            await self._send_version()
        if not self.state.verack_sent:
            await self.send_message(MessageEnvelope(command="verack", payload=EmptyPayload()))
            self.state.verack_sent = True
        self._update_handshake_state()

    async def _send_version(self) -> None:
        """Send the local version message exactly once."""

        if self.state.version_sent:
            return
        await self.send_message(
            MessageEnvelope(
                command="version",
                payload=VersionMessage(
                    protocol_version=self.identity.protocol_version,
                    network=self.identity.network,
                    node_id=self.identity.node_id,
                    start_height=self.identity.start_height,
                    user_agent=self.identity.user_agent,
                    relay=self.identity.relay,
                ),
            )
        )
        self.state.version_sent = True

    def _update_handshake_state(self) -> None:
        """Mark the handshake as complete when both sides exchanged version/verack."""

        complete = (
            self.state.version_sent
            and self.state.version_received
            and self.state.verack_sent
            and self.state.verack_received
        )
        self.state.handshake_complete = complete
        if complete:
            self._handshake_event.set()
            if self.on_handshake_complete is not None and self._handshake_callback_task is None:
                self._handshake_callback_task = asyncio.create_task(self.on_handshake_complete(self))
