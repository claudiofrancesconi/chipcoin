"""Async TCP transport for framed P2P sessions."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from .errors import ConnectionClosedError, TransportError, TransportTimeoutError


FRAME_HEADER_SIZE = 24


class Transport:
    """Send and receive framed bytes over the network."""

    async def send(self, payload: bytes) -> None:
        """Transmit raw bytes to a peer."""

        raise NotImplementedError

    async def receive(self) -> bytes:
        """Receive a complete framed payload from a peer."""

        raise NotImplementedError

    async def close(self) -> None:
        """Close the underlying transport."""

        raise NotImplementedError


@dataclass(frozen=True)
class PeerEndpoint:
    """Resolved peer socket endpoint."""

    host: str
    port: int


class TCPTransport(Transport):
    """Asyncio stream transport for framed P2P messages."""

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        read_timeout: float = 5.0,
        write_timeout: float = 5.0,
    ) -> None:
        self.reader = reader
        self.writer = writer
        self.read_timeout = read_timeout
        self.write_timeout = write_timeout

    @classmethod
    async def connect(
        cls,
        host: str,
        port: int,
        *,
        connect_timeout: float = 5.0,
        read_timeout: float = 5.0,
        write_timeout: float = 5.0,
    ) -> "TCPTransport":
        """Open an outbound TCP connection to a peer."""

        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port),
                timeout=connect_timeout,
            )
        except TimeoutError as exc:
            raise TransportTimeoutError("Timed out while connecting to peer.") from exc
        return cls(reader, writer, read_timeout=read_timeout, write_timeout=write_timeout)

    def peer_endpoint(self) -> PeerEndpoint:
        """Return the remote socket endpoint when available."""

        peer = self.writer.get_extra_info("peername")
        if not peer:
            return PeerEndpoint(host="unknown", port=0)
        host, port = peer[0], peer[1]
        return PeerEndpoint(host=host, port=port)

    async def send(self, payload: bytes) -> None:
        """Transmit a complete framed message."""

        try:
            self.writer.write(payload)
            await asyncio.wait_for(self.writer.drain(), timeout=self.write_timeout)
        except TimeoutError as exc:
            raise TransportTimeoutError("Timed out while sending data to peer.") from exc
        except (ConnectionError, OSError) as exc:
            raise ConnectionClosedError("Peer connection closed during send.") from exc

    async def receive(self) -> bytes:
        """Receive a complete framed message using the header length field."""

        try:
            header = await asyncio.wait_for(
                self.reader.readexactly(FRAME_HEADER_SIZE),
                timeout=self.read_timeout,
            )
            payload_length = int.from_bytes(header[16:20], byteorder="little", signed=False)
            payload = await asyncio.wait_for(
                self.reader.readexactly(payload_length),
                timeout=self.read_timeout,
            )
        except TimeoutError as exc:
            raise TransportTimeoutError("Timed out while receiving data from peer.") from exc
        except asyncio.IncompleteReadError as exc:
            raise ConnectionClosedError("Peer connection closed while reading frame.") from exc
        except (ConnectionError, OSError) as exc:
            raise ConnectionClosedError("Peer connection failed while reading frame.") from exc
        return header + payload

    async def close(self) -> None:
        """Close the underlying writer cleanly."""

        self.writer.close()
        try:
            await asyncio.wait_for(
                self.writer.wait_closed(),
                timeout=max(1.0, self.write_timeout),
            )
        except (TimeoutError, ConnectionError, OSError):
            return None
