import asyncio

from chipcoin.config import DEVNET_CONFIG, MAINNET_CONFIG
from chipcoin.node.messages import MessageEnvelope, PingMessage, VersionMessage
from chipcoin.node.p2p.codec import decode_message, encode_message
from chipcoin.node.p2p.protocol import LocalPeerIdentity, PeerProtocol, ProtocolError
from chipcoin.node.p2p.errors import HandshakeFailedError, WrongNetworkMagicError
from chipcoin.node.p2p.transport import TCPTransport


def test_tcp_transport_sends_and_receives_framed_messages_over_connected_streams() -> None:
    async def scenario() -> None:
        left_transport, right_transport = _make_transport_pair()
        try:
            frame = encode_message(MessageEnvelope(command="ping", payload=PingMessage(nonce=77)))
            await left_transport.send(frame)
            assert decode_message(await right_transport.receive()) == MessageEnvelope(
                command="ping",
                payload=PingMessage(nonce=77),
            )
        finally:
            await left_transport.close()
            await right_transport.close()

    asyncio.run(scenario())


def test_peer_handshake_completes_between_two_local_sessions() -> None:
    async def scenario() -> None:
        outbound_transport, inbound_transport = _make_transport_pair()
        outbound = PeerProtocol(
            transport=outbound_transport,
            identity=LocalPeerIdentity(
                node_id="node-a",
                network="mainnet",
                start_height=10,
                user_agent="/chipcoin:test-a/",
                network_magic=MAINNET_CONFIG.magic,
            ),
            inbound=False,
            handshake_timeout=2.0,
        )
        inbound = PeerProtocol(
            transport=inbound_transport,
            identity=LocalPeerIdentity(
                node_id="node-b",
                network="mainnet",
                start_height=12,
                user_agent="/chipcoin:test-b/",
                network_magic=MAINNET_CONFIG.magic,
            ),
            inbound=True,
            handshake_timeout=2.0,
        )
        try:
            await asyncio.gather(outbound.start(), inbound.start())

            assert outbound.state.handshake_complete is True
            assert inbound.state.handshake_complete is True
            assert outbound.state.remote_version is not None
            assert inbound.state.remote_version is not None
            assert outbound.state.remote_version.node_id == "node-b"
            assert inbound.state.remote_version.node_id == "node-a"
        finally:
            await outbound.close()
            await inbound.close()

    asyncio.run(scenario())


def test_ping_pong_roundtrip_over_local_session() -> None:
    async def scenario() -> None:
        outbound_transport, inbound_transport = _make_transport_pair()
        outbound = PeerProtocol(
            transport=outbound_transport,
            identity=LocalPeerIdentity(
                node_id="node-a",
                network="mainnet",
                start_height=2,
                user_agent="/chipcoin:test-a/",
                network_magic=MAINNET_CONFIG.magic,
            ),
            inbound=False,
            handshake_timeout=2.0,
        )
        inbound = PeerProtocol(
            transport=inbound_transport,
            identity=LocalPeerIdentity(
                node_id="node-b",
                network="mainnet",
                start_height=3,
                user_agent="/chipcoin:test-b/",
                network_magic=MAINNET_CONFIG.magic,
            ),
            inbound=True,
            handshake_timeout=2.0,
        )
        try:
            await asyncio.gather(outbound.start(), inbound.start())
            await outbound.ping(123456789, timeout=2.0)

            assert outbound.state.last_pong_nonce == 123456789
            assert inbound.state.last_ping_nonce == 123456789
        finally:
            await outbound.close()
            await inbound.close()

    asyncio.run(scenario())


def test_handshake_rejects_network_mismatch() -> None:
    async def scenario() -> None:
        outbound_transport, inbound_transport = _make_transport_pair()
        outbound = PeerProtocol(
            transport=outbound_transport,
            identity=LocalPeerIdentity(
                node_id="node-a",
                network="mainnet",
                start_height=0,
                user_agent="/chipcoin:test-a/",
                network_magic=MAINNET_CONFIG.magic,
            ),
            inbound=False,
            handshake_timeout=0.5,
        )
        inbound = PeerProtocol(
            transport=inbound_transport,
            identity=LocalPeerIdentity(
                node_id="node-b",
                network="testnet",
                start_height=0,
                user_agent="/chipcoin:test-b/",
                network_magic=MAINNET_CONFIG.magic,
            ),
            inbound=True,
            handshake_timeout=0.5,
        )
        try:
            results = await asyncio.gather(outbound.start(), inbound.start(), return_exceptions=True)
            assert any(isinstance(result, HandshakeFailedError) for result in results)
            assert outbound.state.closed is True or inbound.state.closed is True
        finally:
            await outbound.close()
            await inbound.close()

    asyncio.run(scenario())


def test_protocol_rejects_duplicate_version_message() -> None:
    async def scenario() -> None:
        outbound_transport, inbound_transport = _make_transport_pair()
        outbound = PeerProtocol(
            transport=outbound_transport,
            identity=LocalPeerIdentity(
                node_id="node-a",
                network="mainnet",
                start_height=0,
                user_agent="/chipcoin:test-a/",
                network_magic=MAINNET_CONFIG.magic,
            ),
            inbound=False,
            handshake_timeout=2.0,
        )
        inbound = PeerProtocol(
            transport=inbound_transport,
            identity=LocalPeerIdentity(
                node_id="node-b",
                network="mainnet",
                start_height=0,
                user_agent="/chipcoin:test-b/",
                network_magic=MAINNET_CONFIG.magic,
            ),
            inbound=True,
            handshake_timeout=2.0,
        )
        try:
            await asyncio.gather(outbound.start(), inbound.start())
            await outbound.send_message(
                MessageEnvelope(
                    command="version",
                    payload=VersionMessage(
                        protocol_version=1,
                        network="mainnet",
                        node_id="node-a-duplicate",
                        start_height=0,
                        user_agent="/chipcoin:test-a/",
                    ),
                )
            )
            await asyncio.sleep(0.05)
            assert inbound.state.closed is True
        finally:
            await outbound.close()
            await inbound.close()

    asyncio.run(scenario())


def test_handshake_rejects_wrong_network_magic_before_application_handshake() -> None:
    async def scenario() -> None:
        outbound_transport, inbound_transport = _make_transport_pair()
        outbound = PeerProtocol(
            transport=outbound_transport,
            identity=LocalPeerIdentity(
                node_id="node-a",
                network="mainnet",
                start_height=0,
                user_agent="/chipcoin:test-a/",
                network_magic=MAINNET_CONFIG.magic,
            ),
            inbound=False,
            handshake_timeout=0.5,
        )
        inbound = PeerProtocol(
            transport=inbound_transport,
            identity=LocalPeerIdentity(
                node_id="node-b",
                network="devnet",
                start_height=0,
                user_agent="/chipcoin:test-b/",
                network_magic=DEVNET_CONFIG.magic,
            ),
            inbound=True,
            handshake_timeout=0.5,
        )
        try:
            results = await asyncio.gather(outbound.start(), inbound.start(), return_exceptions=True)
            assert any(isinstance(result, WrongNetworkMagicError) for result in results)
            combined_errors = " ".join(outbound.state.errors + inbound.state.errors).lower()
            assert "unexpected network magic" in combined_errors
        finally:
            await outbound.close()
            await inbound.close()

    asyncio.run(scenario())


def _make_transport_pair() -> tuple[TCPTransport, TCPTransport]:
    left_reader = _MemoryReader()
    right_reader = _MemoryReader()
    left_writer = _MemoryWriter(peer_reader=right_reader)
    right_writer = _MemoryWriter(peer_reader=left_reader)
    return (
        TCPTransport(left_reader, left_writer, read_timeout=2.0, write_timeout=2.0),
        TCPTransport(right_reader, right_writer, read_timeout=2.0, write_timeout=2.0),
    )


class _MemoryReader:
    def __init__(self) -> None:
        self._buffer = bytearray()
        self._event = asyncio.Event()
        self._closed = False

    async def readexactly(self, size: int) -> bytes:
        while len(self._buffer) < size:
            if self._closed:
                raise asyncio.IncompleteReadError(partial=bytes(self._buffer), expected=size)
            await self._event.wait()
            self._event.clear()
        chunk = bytes(self._buffer[:size])
        del self._buffer[:size]
        return chunk

    def feed_data(self, data: bytes) -> None:
        self._buffer.extend(data)
        self._event.set()

    def close(self) -> None:
        self._closed = True
        self._event.set()


class _MemoryWriter:
    def __init__(self, *, peer_reader: _MemoryReader) -> None:
        self._peer_reader = peer_reader
        self._closed = False

    def write(self, data: bytes) -> None:
        if self._closed:
            raise ConnectionError("writer is closed")
        self._peer_reader.feed_data(data)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self._closed = True
        self._peer_reader.close()

    async def wait_closed(self) -> None:
        return None
