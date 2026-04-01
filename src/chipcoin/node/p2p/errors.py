"""Typed error hierarchy for P2P codec, protocol, and runtime diagnostics."""

from __future__ import annotations


class P2PError(Exception):
    """Base class for typed P2P/runtime failures."""


class CodecError(P2PError, ValueError):
    """Raised when a P2P frame or payload is malformed."""


class WrongNetworkMagicError(CodecError):
    """Raised when a frame magic does not match the current network."""


class MalformedMessageError(CodecError):
    """Raised when a frame or payload structure is malformed."""


class ChecksumError(CodecError):
    """Raised when a frame checksum does not match the payload."""


class ProtocolError(P2PError):
    """Raised when a peer violates the higher-level session protocol."""


class HandshakeFailedError(ProtocolError):
    """Raised when the session handshake cannot complete successfully."""


class ProtocolTimeoutError(ProtocolError):
    """Raised when the peer protocol exceeds a configured timeout."""


class DuplicateConnectionError(ProtocolError):
    """Raised when a duplicate connection is observed for the same peer."""


class InvalidBlockError(ProtocolError):
    """Raised when a peer sends an invalid block."""


class InvalidTxError(ProtocolError):
    """Raised when a peer sends an invalid transaction."""


class TransportError(P2PError):
    """Raised for transport-level failures."""


class ConnectionClosedError(TransportError):
    """Raised when the peer closes the TCP stream."""


class TransportTimeoutError(TransportError):
    """Raised when a transport operation times out."""


def protocol_error_class(error: Exception | str | None) -> str | None:
    """Return a stable protocol error class from a typed exception or fallback string."""

    if error is None:
        return None
    if isinstance(error, WrongNetworkMagicError):
        return "wrong_network_magic"
    if isinstance(error, ChecksumError):
        return "checksum_error"
    if isinstance(error, MalformedMessageError):
        return "malformed_message"
    if isinstance(error, HandshakeFailedError):
        return "handshake_failed"
    if isinstance(error, (ProtocolTimeoutError, TransportTimeoutError)):
        return "timeout"
    if isinstance(error, InvalidBlockError):
        return "invalid_block"
    if isinstance(error, InvalidTxError):
        return "invalid_tx"
    if isinstance(error, DuplicateConnectionError):
        return "duplicate_connection"
    if isinstance(error, ConnectionClosedError):
        return "connection_closed"
    if isinstance(error, str):
        lowered = error.lower()
    else:
        lowered = str(error).lower()
    if "unexpected network magic" in lowered:
        return "wrong_network_magic"
    if "checksum" in lowered:
        return "checksum_error"
    if "frame is shorter" in lowered or "payload length does not match" in lowered or "malformed" in lowered:
        return "malformed_message"
    if "handshake timed out" in lowered or "timed out waiting for handshake completion" in lowered:
        return "handshake_failed"
    if "timed out" in lowered or "ping" in lowered or "pong" in lowered:
        return "timeout"
    if "invalid block" in lowered:
        return "invalid_block"
    if "invalid tx" in lowered:
        return "invalid_tx"
    if "duplicate peer connection" in lowered:
        return "duplicate_connection"
    if "closed while reading frame" in lowered or "closed during send" in lowered or "connection closed" in lowered:
        return "connection_closed"
    if "different network" in lowered or "duplicate version" in lowered or "verack" in lowered or "version" in lowered:
        return "handshake_failed"
    return "malformed_message"
