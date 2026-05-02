from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import asyncio
import logging

from chipcoin.consensus.params import DEVNET_PARAMS, MAINNET_PARAMS
from chipcoin.consensus.models import Block, OutPoint, Transaction, TxInput, TxOutput
from chipcoin.consensus.pow import verify_proof_of_work
from chipcoin.consensus.serialization import serialize_transaction
from chipcoin.consensus.validation import (
    ContextualValidationError,
    ValidationContext,
    validate_block,
    ValidationError,
    transaction_signature_digest,
)
from chipcoin.consensus.utxo import InMemoryUtxoView
from chipcoin.crypto.signatures import sign_digest
from chipcoin.node.mempool import MempoolPolicy
from chipcoin.node.mining import transaction_weight_units
from chipcoin.node.peers import PeerInfo, PeerManager
from chipcoin.node.messages import AddrMessage, HeadersMessage, MessageEnvelope, PeerAddress
from chipcoin.node.p2p.errors import BlockRequestStalledError, DuplicateConnectionError, InvalidBlockError, ProtocolError
from chipcoin.node.p2p.errors import HandshakeFailedError, TransportTimeoutError
from chipcoin.node.runtime import NodeRuntime, OutboundPeer, SessionHandle
from chipcoin.node.p2p.transport import PeerEndpoint
from chipcoin.node.service import NodeService
from chipcoin.node.sync import BlockDownloadAssignment, BlockIngestResult, BlockRequestState, HeaderIngestResult
from chipcoin.storage.peers import SQLitePeerRepository
from chipcoin.storage.mempool import MempoolEntry
from chipcoin.wallet.signer import TransactionSigner
from tests.helpers import put_wallet_utxo, signed_payment, spend_candidates_for_wallet, wallet_key


def _make_service(database_path: Path) -> NodeService:
    timestamps = iter(range(1_700_000_000, 1_700_000_100))
    return NodeService.open_sqlite(database_path, time_provider=lambda: next(timestamps))


def _make_service_with_params(database_path: Path, params) -> NodeService:
    timestamps = iter(range(1_700_000_000, 1_700_000_200))
    return NodeService.open_sqlite(database_path, params=params, time_provider=lambda: next(timestamps))


def _mine_block(block: Block) -> Block:
    for nonce in range(2_000_000):
        mined_header = replace(block.header, nonce=nonce)
        if verify_proof_of_work(mined_header):
            return replace(block, header=mined_header)
    raise AssertionError("Expected to find a valid nonce for the easy target.")


def _spend_transaction(outpoint: OutPoint, *, input_value: int, output_value: int):
    return signed_payment(
        outpoint,
        value=input_value,
        sender=wallet_key(0),
        amount=output_value,
        fee=input_value - output_value,
    )


def test_peer_manager_keeps_local_peerbook() -> None:
    peerbook = PeerManager()
    peer = PeerInfo(host="127.0.0.1", port=8333, network="mainnet")

    peerbook.add(peer)

    assert peerbook.list_all() == [peer]
    assert peerbook.list_all(network="mainnet") == [peer]

    peerbook.remove(peer)
    assert peerbook.list_all() == []


def test_node_service_opens_devnet_with_devnet_params() -> None:
    with TemporaryDirectory() as tempdir:
        service = NodeService.open_sqlite(Path(tempdir) / "chipcoin-devnet.sqlite3", network="devnet")

        assert service.network == "devnet"
        assert service.params == DEVNET_PARAMS


def test_runtime_does_not_redial_or_advertise_inbound_ephemeral_peers() -> None:
    with TemporaryDirectory() as tempdir:
        service = _make_service(Path(tempdir) / "chipcoin.sqlite3")
        service.add_peer("127.0.0.1", 18444, source="manual")
        service.record_peer_observation(
            host="172.18.0.2",
            port=36672,
            direction="inbound",
            handshake_complete=True,
            node_id="ephemeral-inbound",
        )
        runtime = NodeRuntime(
            service=service,
            listen_host="127.0.0.1",
            listen_port=18445,
            outbound_peers=[OutboundPeer("127.0.0.1", 18444)],
        )

        desired = runtime._desired_outbound_peers()
        assert desired == [OutboundPeer("127.0.0.1", 18444)]
        peers = service.list_peers()
        outbound_peer = next(peer for peer in peers if peer.host == "127.0.0.1")
        inbound_peer = next(peer for peer in peers if peer.host == "172.18.0.2")
        assert runtime._is_advertisable_peer(outbound_peer) is True
        assert runtime._is_advertisable_peer(inbound_peer) is False


def test_runtime_canonicalizes_public_inbound_peer_to_known_default_p2p_port() -> None:
    with TemporaryDirectory() as tempdir:
        service = NodeService.open_sqlite(Path(tempdir) / "chipcoin-devnet.sqlite3", network="devnet")
        service.record_peer_observation(
            host="188.218.213.92",
            port=18444,
            direction=None,
            handshake_complete=True,
            node_id="mac-node-id",
        )
        runtime = NodeRuntime(service=service, listen_host="0.0.0.0", listen_port=18444)

        canonical = runtime._canonicalize_reusable_inbound_endpoint(
            PeerEndpoint(host="188.218.213.92", port=56693),
            inbound=True,
            node_id="mac-node-id",
        )

        assert canonical == OutboundPeer("188.218.213.92", 18444)


def test_runtime_canonicalizes_unknown_public_inbound_peer_to_default_p2p_port() -> None:
    with TemporaryDirectory() as tempdir:
        service = NodeService.open_sqlite(Path(tempdir) / "chipcoin-devnet.sqlite3", network="devnet")
        runtime = NodeRuntime(service=service, listen_host="0.0.0.0", listen_port=18444)

        canonical = runtime._canonicalize_reusable_inbound_endpoint(
            PeerEndpoint(host="188.218.213.92", port=56693),
            inbound=True,
            node_id="mac-node-id",
        )

        assert canonical == OutboundPeer("188.218.213.92", 18444)


def test_runtime_does_not_canonicalize_public_inbound_peer_when_canonical_endpoint_belongs_to_other_node_id() -> None:
    with TemporaryDirectory() as tempdir:
        service = NodeService.open_sqlite(Path(tempdir) / "chipcoin-devnet.sqlite3", network="devnet")
        service.record_peer_observation(
            host="188.217.94.86",
            port=18444,
            direction=None,
            handshake_complete=True,
            node_id="tobia-node-id",
        )
        runtime = NodeRuntime(service=service, listen_host="0.0.0.0", listen_port=18444)

        canonical = runtime._canonicalize_reusable_inbound_endpoint(
            PeerEndpoint(host="188.217.94.86", port=47740),
            inbound=True,
            node_id="tobia-miner-id",
        )

        assert canonical is None


def test_runtime_does_not_canonicalize_private_inbound_peer() -> None:
    with TemporaryDirectory() as tempdir:
        service = NodeService.open_sqlite(Path(tempdir) / "chipcoin-devnet.sqlite3", network="devnet")
        runtime = NodeRuntime(service=service, listen_host="0.0.0.0", listen_port=18444)

        canonical = runtime._canonicalize_reusable_inbound_endpoint(
            PeerEndpoint(host="172.18.0.2", port=36672),
            inbound=True,
        )

        assert canonical is None


def test_runtime_persists_unknown_public_inbound_peer_on_canonical_endpoint() -> None:
    async def scenario() -> None:
        with TemporaryDirectory() as tempdir:
            service = NodeService.open_sqlite(Path(tempdir) / "chipcoin-devnet.sqlite3", network="devnet")
            runtime = NodeRuntime(service=service, listen_host="0.0.0.0", listen_port=18444)

            class _FakeRemote:
                node_id = "mac-node-id"
                start_height = 3801

            class _FakeState:
                closed = False
                handshake_complete = True
                remote_version = _FakeRemote()
                errors: list[str] = []
                error_causes: list[Exception] = []

            class _FakeTransport:
                @staticmethod
                def peer_endpoint():
                    return type("_Peer", (), {"host": "188.218.213.92", "port": 56693})()

            class _FakeSession:
                inbound = True
                state = _FakeState()
                transport = _FakeTransport()

                async def send_message(self, message: MessageEnvelope) -> None:
                    return None

                async def close(self, *, reason: str | None = None, error: Exception | None = None) -> None:
                    self.state.closed = True

            session = _FakeSession()
            runtime._sessions[session] = SessionHandle(protocol=session, outbound=False)

            await runtime._on_handshake_complete(session)

            peers = service.list_peers()
            assert any(
                peer.host == "188.218.213.92"
                and peer.port == 18444
                and peer.node_id == "mac-node-id"
                and peer.direction is None
                and peer.source == "discovered"
                and peer.success_count == 1
                for peer in peers
            )
            assert not any(peer.host == "188.218.213.92" and peer.port == 56693 for peer in peers)
            assert runtime._desired_outbound_peers() == [OutboundPeer("188.218.213.92", 18444)]
            observed_peer = next(peer for peer in peers if peer.host == "188.218.213.92" and peer.port == 18444)
            assert runtime._is_advertisable_peer(observed_peer) is True

            await runtime._drop_session(session)

            dropped_peer = next(
                peer for peer in service.list_peers() if peer.host == "188.218.213.92" and peer.port == 18444
            )
            assert dropped_peer.direction is None
            assert runtime._desired_outbound_peers() == [OutboundPeer("188.218.213.92", 18444)]

    asyncio.run(scenario())


def test_runtime_keeps_conflicting_public_inbound_peer_on_ephemeral_port() -> None:
    async def scenario() -> None:
        with TemporaryDirectory() as tempdir:
            service = NodeService.open_sqlite(Path(tempdir) / "chipcoin-devnet.sqlite3", network="devnet")
            service.record_peer_observation(
                host="188.217.94.86",
                port=18444,
                direction=None,
                handshake_complete=True,
                node_id="tobia-node-id",
            )
            runtime = NodeRuntime(service=service, listen_host="0.0.0.0", listen_port=18444)

            class _FakeRemote:
                node_id = "tobia-miner-id"
                start_height = 3921

            class _FakeState:
                closed = False
                handshake_complete = True
                remote_version = _FakeRemote()
                errors: list[str] = []
                error_causes: list[Exception] = []

            class _FakeTransport:
                @staticmethod
                def peer_endpoint():
                    return type("_Peer", (), {"host": "188.217.94.86", "port": 47740})()

            class _FakeSession:
                inbound = True
                state = _FakeState()
                transport = _FakeTransport()

                async def send_message(self, message: MessageEnvelope) -> None:
                    return None

                async def close(self, *, reason: str | None = None, error: Exception | None = None) -> None:
                    self.state.closed = True

            session = _FakeSession()
            runtime._sessions[session] = SessionHandle(protocol=session, outbound=False)

            await runtime._on_handshake_complete(session)

            peers = service.list_peers()
            assert any(
                peer.host == "188.217.94.86"
                and peer.port == 18444
                and peer.node_id == "tobia-node-id"
                for peer in peers
            )
            assert any(
                peer.host == "188.217.94.86"
                and peer.port == 47740
                and peer.node_id == "tobia-miner-id"
                and peer.direction == "inbound"
                for peer in peers
            )
            assert OutboundPeer("188.217.94.86", 47740) not in runtime._desired_outbound_peers()

    asyncio.run(scenario())


def test_runtime_does_not_promote_new_ephemeral_inbound_alias_for_same_node_id(caplog) -> None:
    async def scenario() -> None:
        with TemporaryDirectory() as tempdir:
            service = NodeService.open_sqlite(Path(tempdir) / "chipcoin-devnet.sqlite3", network="devnet")
            runtime = NodeRuntime(service=service, listen_host="0.0.0.0", listen_port=18444)

            class _FakeRemote:
                node_id = "tobia-miner-id"
                start_height = 3921

            class _FakeState:
                closed = False
                handshake_complete = True
                remote_version = _FakeRemote()
                errors: list[str] = []
                error_causes: list[Exception] = []

            class _FirstTransport:
                @staticmethod
                def peer_endpoint():
                    return type("_Peer", (), {"host": "188.217.94.86", "port": 41914})()

            class _SecondTransport:
                @staticmethod
                def peer_endpoint():
                    return type("_Peer", (), {"host": "188.217.94.86", "port": 43336})()

            class _FakeSession:
                inbound = True
                state = _FakeState()

                async def send_message(self, message: MessageEnvelope) -> None:
                    return None

                async def close(self, *, reason: str | None = None, error: Exception | None = None) -> None:
                    self.state.closed = True

            first = _FakeSession()
            first.transport = _FirstTransport()
            runtime._sessions[first] = SessionHandle(protocol=first, outbound=False)
            await runtime._on_handshake_complete(first)
            first_peers = {(peer.host, peer.port, peer.node_id) for peer in service.list_peers()}
            await runtime._drop_session(first)

            second = _FakeSession()
            second.transport = _SecondTransport()
            runtime._sessions[second] = SessionHandle(protocol=second, outbound=False)
            with caplog.at_level(logging.INFO):
                await runtime._on_handshake_complete(second)

            peers = service.list_peers()
            assert any(peer.host == "188.217.94.86" and peer.port == 18444 and peer.node_id == "tobia-miner-id" for peer in peers)
            assert ("188.217.94.86", 18444, "tobia-miner-id") in first_peers
            assert not any(peer.host == "188.217.94.86" and peer.port in {41914, 43336} for peer in peers)
            assert "removed peer alias node_id=tobia-miner-id" not in caplog.text

    asyncio.run(scenario())


def test_runtime_reuses_canonicalized_public_peer_after_restart() -> None:
    with TemporaryDirectory() as tempdir:
        database_path = Path(tempdir) / "chipcoin-devnet.sqlite3"
        service = NodeService.open_sqlite(database_path, network="devnet")
        service.record_peer_observation(
            host="188.218.213.92",
            port=18444,
            direction=None,
            handshake_complete=True,
            node_id="mac-node-id",
        )
        service.add_peer("tiltmediaconsulting.com", 18444, source="manual")
        runtime = NodeRuntime(
            service=service,
            listen_host="0.0.0.0",
            listen_port=18444,
            outbound_peers=[OutboundPeer("tiltmediaconsulting.com", 18444)],
        )

        desired = runtime._desired_outbound_peers()

        assert OutboundPeer("188.218.213.92", 18444) in desired
        assert OutboundPeer("tiltmediaconsulting.com", 18444) in desired


def test_runtime_logs_initial_peer_failures_at_info_then_suppresses_terminal_churn() -> None:
    with TemporaryDirectory() as tempdir:
        service = _make_service(Path(tempdir) / "chipcoin.sqlite3")
        runtime = NodeRuntime(service=service, listen_host="127.0.0.1", listen_port=18445)

        assert runtime._should_log_peer_failure_info(None, attempts=1, score=-20) is True

        service.record_peer_observation(
            host="173.212.193.13",
            port=18444,
            direction="outbound",
            handshake_complete=False,
            score=-100,
            reconnect_attempts=12,
            backoff_until=1_775_056_440,
            last_error="connect failed",
        )

        info = next(
            (peer for peer in service.list_peers() if peer.host == "173.212.193.13" and peer.port == 18444),
            None,
        )

        assert info is not None
        assert runtime._should_log_peer_failure_info(info, attempts=13, score=-100) is False


def test_runtime_accumulates_misbehavior_and_bans_peer_after_threshold() -> None:
    with TemporaryDirectory() as tempdir:
        service = _make_service(Path(tempdir) / "chipcoin.sqlite3")
        runtime = NodeRuntime(service=service, listen_host="127.0.0.1", listen_port=18445)

        runtime._observe_peer_misbehavior(
            host="node-a.example",
            port=18444,
            event="handshake_failed",
            delta=25,
            direction="outbound",
            handshake_complete=False,
        )
        runtime._observe_peer_misbehavior(
            host="node-a.example",
            port=18444,
            event="timeout",
            delta=10,
            direction="outbound",
            handshake_complete=False,
        )
        action = runtime._observe_peer_misbehavior(
            host="node-a.example",
            port=18444,
            event="malformed_message",
            delta=70,
            direction="outbound",
            handshake_complete=False,
        )

        info = next(peer for peer in service.list_peers() if peer.host == "node-a.example" and peer.port == 18444)
        assert action == "ban"
        assert info.misbehavior_score == 105
        assert info.ban_until is not None
        assert runtime._is_peer_currently_banned("node-a.example", 18444) is True
        assert OutboundPeer("node-a.example", 18444) not in runtime._desired_outbound_peers()


def test_runtime_transport_failures_do_not_accumulate_misbehavior_bans() -> None:
    with TemporaryDirectory() as tempdir:
        service = _make_service(Path(tempdir) / "chipcoin.sqlite3")
        runtime = NodeRuntime(service=service, listen_host="127.0.0.1", listen_port=18445)
        peer = OutboundPeer("203.0.113.20", 18444)

        for _ in range(5):
            runtime._register_peer_failure(peer, error="[Errno 111] Connect call failed ('203.0.113.20', 18444)")

        info = next(peer for peer in service.list_peers() if peer.host == "203.0.113.20" and peer.port == 18444)
        assert info.protocol_error_class == "connection_failed"
        assert info.misbehavior_score in (None, 0)
        assert info.ban_until is None
        assert runtime._is_peer_currently_banned("203.0.113.20", 18444) is False


def test_runtime_sync_complete_log_reports_final_local_and_peer_target_heights(caplog) -> None:
    with TemporaryDirectory() as tempdir:
        service = _make_service(Path(tempdir) / "chipcoin.sqlite3")
        runtime = NodeRuntime(service=service, listen_host="127.0.0.1", listen_port=18445)

        class _FakeRemote:
            node_id = "peer-a"
            start_height = 12

        class _FakeState:
            closed = False
            handshake_complete = True
            remote_version = _FakeRemote()

        class _FakeSession:
            inbound = False
            state = _FakeState()
            transport = type(
                "_FakeTransport",
                (),
                {"peer_endpoint": staticmethod(lambda: type("_Peer", (), {"host": "198.51.100.20", "port": 18444})())},
            )()

        mined = _mine_block(service.build_candidate_block("CHCminer").block)
        service.apply_block(mined)
        session = _FakeSession()
        runtime._sessions[session] = SessionHandle(
            protocol=session,
            outbound=True,
            endpoint=OutboundPeer("198.51.100.20", 18444),
            sync_target_height=0,
        )

        with caplog.at_level(logging.INFO, logger="chipcoin.node.runtime"):
            runtime._log_sync_progress(session)

        assert "sync complete" in caplog.text
        assert "final_local_height=0" in caplog.text
        assert "peer_target_height=0" in caplog.text
        assert "best_header_height" in caplog.text


def test_runtime_decays_misbehavior_score_over_time() -> None:
    with TemporaryDirectory() as tempdir:
        service = NodeService.open_sqlite(Path(tempdir) / "chipcoin.sqlite3", time_provider=lambda: 1_700_000_900)
        service.record_peer_observation(
            host="node-b.example",
            port=18444,
            misbehavior_score=55,
            misbehavior_last_updated_at=1_700_000_000,
        )
        runtime = NodeRuntime(
            service=service,
            listen_host="127.0.0.1",
            listen_port=18445,
            misbehavior_decay_interval_seconds=300,
            misbehavior_decay_step=10,
        )

        info = next(peer for peer in service.list_peers() if peer.host == "node-b.example" and peer.port == 18444)
        score, updated_at = runtime._decayed_misbehavior_state(info, now=service.time_provider())

        assert score == 25
        assert updated_at == 1_700_000_900


def test_runtime_allows_reconnect_after_ban_expiry() -> None:
    with TemporaryDirectory() as tempdir:
        service = NodeService.open_sqlite(Path(tempdir) / "chipcoin.sqlite3", time_provider=lambda: 1_700_000_500)
        service.record_peer_observation(
            host="node-c.example",
            port=18444,
            direction="outbound",
            misbehavior_score=100,
            misbehavior_last_updated_at=1_700_000_000,
            ban_until=1_700_000_200,
        )
        runtime = NodeRuntime(service=service, listen_host="127.0.0.1", listen_port=18445)

        assert runtime._is_peer_currently_banned("node-c.example", 18444) is False
        assert runtime._desired_outbound_peers() == [OutboundPeer("node-c.example", 18444)]


def test_runtime_bans_severe_invalid_block_violation() -> None:
    with TemporaryDirectory() as tempdir:
        service = _make_service(Path(tempdir) / "chipcoin.sqlite3")
        runtime = NodeRuntime(service=service, listen_host="127.0.0.1", listen_port=18445)

        class _FakeRemote:
            node_id = "bad-peer"
            start_height = 12

        class _FakeState:
            closed = False
            handshake_complete = True
            remote_version = _FakeRemote()

        class _FakeSession:
            inbound = False
            state = _FakeState()
            transport = type(
                "_FakeTransport",
                (),
                {"peer_endpoint": staticmethod(lambda: type("_Peer", (), {"host": "198.51.100.20", "port": 18444})())},
            )()

        session = _FakeSession()
        runtime._sessions[session] = SessionHandle(
            protocol=session,
            outbound=True,
            endpoint=OutboundPeer("198.51.100.20", 18444),
        )

        runtime._apply_session_penalty(
            session,
            error=InvalidBlockError("invalid block: bad merkle root"),
            penalty=runtime._SEVERE_MISBEHAVIOR_DELTA,
        )

        info = next(peer for peer in service.list_peers() if peer.host == "198.51.100.20" and peer.port == 18444)
        assert info.misbehavior_score == runtime._SEVERE_MISBEHAVIOR_DELTA
        assert info.ban_until is not None
        assert info.last_penalty_reason == "invalid_block"


def test_runtime_treats_duplicate_connection_drops_as_low_value_churn() -> None:
    with TemporaryDirectory() as tempdir:
        service = _make_service(Path(tempdir) / "chipcoin.sqlite3")
        runtime = NodeRuntime(service=service, listen_host="127.0.0.1", listen_port=18445)

        assert runtime._is_low_value_session_drop(DuplicateConnectionError("Duplicate peer connection.")) is True
        assert runtime._is_low_value_session_drop("Duplicate peer connection.") is True
        assert runtime._is_low_value_session_drop("Peer connection closed while reading frame.") is False


def test_runtime_does_not_redial_persisted_private_ip_peers() -> None:
    with TemporaryDirectory() as tempdir:
        service = _make_service(Path(tempdir) / "chipcoin.sqlite3")
        service.record_peer_observation(
            host="172.18.0.2",
            port=18444,
            direction="outbound",
            handshake_complete=True,
            node_id="docker-alias",
        )
        runtime = NodeRuntime(
            service=service,
            listen_host="127.0.0.1",
            listen_port=18445,
        )

        assert runtime._desired_outbound_peers() == []


def test_runtime_start_purges_persisted_private_ip_peers() -> None:
    with TemporaryDirectory() as tempdir:
        service = _make_service(Path(tempdir) / "chipcoin.sqlite3")
        service.record_peer_observation(
            host="172.18.0.2",
            port=18444,
            direction="outbound",
            handshake_complete=True,
            node_id="docker-alias",
        )
        runtime = NodeRuntime(
            service=service,
            listen_host="127.0.0.1",
            listen_port=18445,
        )

        runtime._purge_undialable_persisted_peers()

        peers = service.list_peers()
        assert not any(peer.host == "172.18.0.2" and peer.port == 18444 for peer in peers)


def test_runtime_start_purges_persisted_startup_duplicate_aliases() -> None:
    with TemporaryDirectory() as tempdir:
        service = _make_service(Path(tempdir) / "chipcoin.sqlite3")
        service.record_peer_observation(
            host="tiltmediaconsulting.com",
            port=18444,
            direction="outbound",
            handshake_complete=False,
            protocol_error_class="duplicate_connection",
        )
        runtime = NodeRuntime(
            service=service,
            listen_host="0.0.0.0",
            listen_port=18444,
        )

        runtime._purge_persisted_startup_duplicate_aliases()

        assert not any(
            peer.host == "tiltmediaconsulting.com" and peer.port == 18444
            for peer in service.list_peers()
        )


def test_runtime_ignores_private_ip_addresses_announced_by_peers() -> None:
    async def scenario() -> None:
        with TemporaryDirectory() as tempdir:
            service = _make_service(Path(tempdir) / "chipcoin.sqlite3")
            runtime = NodeRuntime(service=service, listen_host="127.0.0.1", listen_port=18445)

            class _FakeSessionState:
                closed = False
                handshake_complete = True
                remote_version = None
                errors: list[str] = []
                error_causes: list[Exception] = []

            class _FakeSession:
                inbound = False
                state = _FakeSessionState()

            session = _FakeSession()
            await runtime._on_peer_message(
                session,
                MessageEnvelope(
                    command="addr",
                    payload=AddrMessage(
                        addresses=(
                            PeerAddress(host="172.18.0.2", port=18444, services=0, timestamp=1_700_000_000),
                        )
                    ),
                ),
            )

            assert runtime._desired_outbound_peers() == []
            assert not any(peer.host == "172.18.0.2" and peer.port == 18444 for peer in service.list_peers())

    asyncio.run(scenario())


def test_runtime_rejects_invalid_announced_hostnames() -> None:
    async def scenario() -> None:
        with TemporaryDirectory() as tempdir:
            service = _make_service(Path(tempdir) / "chipcoin.sqlite3")
            runtime = NodeRuntime(service=service, listen_host="127.0.0.1", listen_port=18445)

            class _FakeSessionState:
                closed = False
                handshake_complete = True
                remote_version = None
                errors: list[str] = []
                error_causes: list[Exception] = []

            class _FakeSession:
                inbound = False
                state = _FakeSessionState()

            await runtime._on_peer_message(
                _FakeSession(),
                MessageEnvelope(
                    command="addr",
                    payload=AddrMessage(
                        addresses=(
                            PeerAddress(host="bad host name", port=18444, services=0, timestamp=1_700_000_000),
                            PeerAddress(host="node?.example", port=18444, services=0, timestamp=1_700_000_000),
                        )
                    ),
                ),
            )

            assert runtime._desired_outbound_peers() == []
            assert service.list_peers() == []

    asyncio.run(scenario())


def test_runtime_ignores_announced_alias_of_known_peer(monkeypatch) -> None:
    async def scenario() -> None:
        with TemporaryDirectory() as tempdir:
            service = _make_service(Path(tempdir) / "chipcoin.sqlite3")
            service.add_peer("173.212.193.13", 18444, source="manual")
            runtime = NodeRuntime(service=service, listen_host="127.0.0.1", listen_port=18445)

            def fake_getaddrinfo(host: str, port: int, type: int):
                if port != 18444:
                    raise OSError("unexpected port")
                if host in {"173.212.193.13", "tiltmediaconsulting.com"}:
                    return [(None, None, None, None, ("173.212.193.13", port))]
                raise OSError("unresolvable")

            monkeypatch.setattr("chipcoin.node.runtime.socket.getaddrinfo", fake_getaddrinfo)

            class _FakeSessionState:
                closed = False
                handshake_complete = True
                remote_version = None
                errors: list[str] = []
                error_causes: list[Exception] = []

            class _FakeSession:
                inbound = False
                state = _FakeSessionState()

            session = _FakeSession()
            await runtime._on_peer_message(
                session,
                MessageEnvelope(
                    command="addr",
                    payload=AddrMessage(
                        addresses=(
                            PeerAddress(
                                host="tiltmediaconsulting.com",
                                port=18444,
                                services=0,
                                timestamp=1_700_000_000,
                            ),
                        )
                    ),
                ),
            )

            assert runtime._desired_outbound_peers() == [OutboundPeer("173.212.193.13", 18444)]
            peers = service.list_peers()
            assert any(peer.host == "173.212.193.13" and peer.port == 18444 for peer in peers)
            assert not any(peer.host == "tiltmediaconsulting.com" and peer.port == 18444 for peer in peers)

    asyncio.run(scenario())


def test_runtime_learns_discovered_peers_from_addr_gossip_and_persists_source() -> None:
    async def scenario() -> None:
        with TemporaryDirectory() as tempdir:
            database_path = Path(tempdir) / "chipcoin-devnet.sqlite3"
            service = NodeService.open_sqlite(database_path, network="devnet")
            runtime = NodeRuntime(service=service, listen_host="127.0.0.1", listen_port=18445)

            class _FakeSessionState:
                closed = False
                handshake_complete = True
                remote_version = None
                errors: list[str] = []
                error_causes: list[Exception] = []

            class _FakeSession:
                inbound = False
                state = _FakeSessionState()

            await runtime._on_peer_message(
                _FakeSession(),
                MessageEnvelope(
                    command="addr",
                    payload=AddrMessage(
                        addresses=(PeerAddress(host="188.218.213.92", port=18444, services=0, timestamp=1_700_000_000),)
                    ),
                ),
            )

            restarted = NodeRuntime(
                service=NodeService.open_sqlite(database_path, network="devnet"),
                listen_host="127.0.0.1",
                listen_port=18445,
            )
            peers = restarted.service.list_peers()
            learned = next(peer for peer in peers if peer.host == "188.218.213.92" and peer.port == 18444)
            assert learned.source == "discovered"
            assert learned.first_seen is not None
            assert OutboundPeer("188.218.213.92", 18444) in restarted._desired_outbound_peers()

    asyncio.run(scenario())


def test_runtime_canonicalizes_ephemeral_addr_gossip_to_default_p2p_port() -> None:
    async def scenario() -> None:
        with TemporaryDirectory() as tempdir:
            database_path = Path(tempdir) / "chipcoin-devnet.sqlite3"
            service = NodeService.open_sqlite(database_path, network="devnet")
            runtime = NodeRuntime(service=service, listen_host="127.0.0.1", listen_port=18445)

            class _FakeSessionState:
                closed = False
                handshake_complete = True
                remote_version = None
                errors: list[Exception] = []
                error_causes: list[Exception] = []

            class _FakeSession:
                inbound = False
                state = _FakeSessionState()

            await runtime._on_peer_message(
                _FakeSession(),
                MessageEnvelope(
                    command="addr",
                    payload=AddrMessage(
                        addresses=(PeerAddress(host="188.217.94.86", port=58236, services=0, timestamp=1_700_000_000),)
                    ),
                ),
            )

            peers = service.list_peers()
            assert any(peer.host == "188.217.94.86" and peer.port == 18444 for peer in peers)
            assert not any(peer.host == "188.217.94.86" and peer.port == 58236 for peer in peers)

    asyncio.run(scenario())


def test_runtime_startup_prefers_persisted_healthy_peer_over_manual_seed() -> None:
    with TemporaryDirectory() as tempdir:
        service = _make_service(Path(tempdir) / "chipcoin.sqlite3")
        service.record_peer_observation(
            host="188.218.213.92",
            port=18444,
            source="discovered",
            handshake_complete=True,
            success_count=2,
            last_success=1_700_000_010,
            score=5,
        )
        runtime = NodeRuntime(
            service=service,
            listen_host="127.0.0.1",
            listen_port=18445,
            outbound_peers=[OutboundPeer("tiltmediaconsulting.com", 18444)],
        )
        runtime._outbound_target_sources[( "tiltmediaconsulting.com", 18444)] = "seed"

        assert runtime._desired_outbound_peers() == [OutboundPeer("188.218.213.92", 18444)]


def test_runtime_purges_stale_discovered_peers_but_keeps_manual_peers() -> None:
    with TemporaryDirectory() as tempdir:
        service = NodeService.open_sqlite(Path(tempdir) / "chipcoin.sqlite3", time_provider=lambda: 1_800_000_000)
        assert isinstance(service.peer_repository, SQLitePeerRepository)
        service.peer_repository.add(
            PeerInfo(
                host="188.218.213.92",
                port=18444,
                network="mainnet",
                source="discovered",
                first_seen=1_700_000_000,
                last_seen=1_700_000_000,
            )
        )
        service.peer_repository.add(
            PeerInfo(
                host="manual.example",
                port=18444,
                network="mainnet",
                source="manual",
                first_seen=1_700_000_000,
                last_seen=1_700_000_000,
            )
        )
        runtime = NodeRuntime(service=service, listen_host="127.0.0.1", listen_port=18445, peer_stale_after_seconds=60)

        runtime._purge_stale_persisted_peers()

        peers = service.list_peers()
        assert not any(peer.host == "188.218.213.92" for peer in peers)
        assert any(peer.host == "manual.example" for peer in peers)


def test_runtime_start_purges_persisted_discovered_ephemeral_port_peers() -> None:
    with TemporaryDirectory() as tempdir:
        service = NodeService.open_sqlite(Path(tempdir) / "chipcoin-devnet.sqlite3", network="devnet")
        assert isinstance(service.peer_repository, SQLitePeerRepository)
        service.peer_repository.add(
            PeerInfo(
                host="188.217.94.86",
                port=58236,
                network="devnet",
                source="discovered",
                first_seen=1_700_000_000,
                last_seen=1_700_000_000,
            )
        )
        service.peer_repository.add(
            PeerInfo(
                host="tiltmediaconsulting.com",
                port=18444,
                network="devnet",
                source="manual",
                first_seen=1_700_000_000,
                last_seen=1_700_000_000,
            )
        )

        runtime = NodeRuntime(service=service, listen_host="127.0.0.1", listen_port=18444)
        runtime._purge_undialable_persisted_peers()

        peers = service.list_peers()
        assert not any(peer.host == "188.217.94.86" and peer.port == 58236 for peer in peers)
        assert any(peer.host == "tiltmediaconsulting.com" and peer.port == 18444 for peer in peers)


def test_runtime_limits_addr_relay_per_message_and_interval() -> None:
    async def scenario() -> None:
        with TemporaryDirectory() as tempdir:
            service = NodeService.open_sqlite(
                Path(tempdir) / "chipcoin-devnet.sqlite3",
                network="devnet",
                time_provider=lambda: 1_700_000_100,
            )
            for index in range(5):
                service.record_peer_observation(
                    host=f"188.218.213.{index + 10}",
                    port=18444,
                    source="discovered",
                    success_count=1,
                    last_success=1_700_000_010 + index,
                )
            runtime = NodeRuntime(
                service=service,
                listen_host="127.0.0.1",
                listen_port=18445,
                peer_addr_max_per_message=2,
                peer_addr_relay_limit_per_interval=3,
                peer_addr_relay_interval_seconds=60,
            )

            sent_messages: list[MessageEnvelope] = []

            class _FakeSessionState:
                closed = False
                handshake_complete = True
                remote_version = None
                errors: list[str] = []
                error_causes: list[Exception] = []

            class _FakeSession:
                inbound = False
                state = _FakeSessionState()

                async def send_message(self, message: MessageEnvelope) -> None:
                    sent_messages.append(message)

            session = _FakeSession()
            runtime._sessions[session] = SessionHandle(protocol=session, outbound=False)

            await runtime._send_known_peers(session)
            await runtime._send_known_peers(session)
            await runtime._send_known_peers(session)

            assert [len(message.payload.addresses) for message in sent_messages] == [2, 1]

    asyncio.run(scenario())


def test_runtime_does_not_relay_banned_peers_in_addr_messages() -> None:
    async def scenario() -> None:
        with TemporaryDirectory() as tempdir:
            service = NodeService.open_sqlite(
                Path(tempdir) / "chipcoin-devnet.sqlite3",
                network="devnet",
                time_provider=lambda: 1_700_000_100,
            )
            service.record_peer_observation(
                host="198.51.100.10",
                port=18444,
                source="discovered",
                success_count=1,
                last_success=1_700_000_010,
            )
            service.record_peer_observation(
                host="198.51.100.11",
                port=18444,
                source="discovered",
                success_count=1,
                last_success=1_700_000_011,
                ban_until=1_700_000_999,
            )
            runtime = NodeRuntime(service=service, listen_host="127.0.0.1", listen_port=18445)

            sent_messages: list[MessageEnvelope] = []

            class _FakeSessionState:
                closed = False
                handshake_complete = True
                remote_version = None
                errors: list[str] = []
                error_causes: list[Exception] = []

            class _FakeSession:
                inbound = False
                state = _FakeSessionState()

                async def send_message(self, message: MessageEnvelope) -> None:
                    sent_messages.append(message)

            session = _FakeSession()
            runtime._sessions[session] = SessionHandle(protocol=session, outbound=False)

            await runtime._send_known_peers(session)

            relayed_hosts = [address.host for address in sent_messages[0].payload.addresses]
            assert "198.51.100.10" in relayed_hosts
            assert "198.51.100.11" not in relayed_hosts

    asyncio.run(scenario())


def test_runtime_dedupes_unidentified_outbound_aliases(monkeypatch) -> None:
    with TemporaryDirectory() as tempdir:
        service = _make_service(Path(tempdir) / "chipcoin.sqlite3")
        service.add_peer("173.212.193.13", 18444, source="manual")
        service.add_peer("tiltmediaconsulting.com", 18444, source="manual")
        runtime = NodeRuntime(service=service, listen_host="127.0.0.1", listen_port=18445)

        def fake_getaddrinfo(host: str, port: int, type: int):
            if port != 18444:
                raise OSError("unexpected port")
            if host in {"173.212.193.13", "tiltmediaconsulting.com"}:
                return [(None, None, None, None, ("173.212.193.13", port))]
            raise OSError("unresolvable")

        monkeypatch.setattr("chipcoin.node.runtime.socket.getaddrinfo", fake_getaddrinfo)

        assert runtime._desired_outbound_peers() == [OutboundPeer("173.212.193.13", 18444)]


def test_runtime_treats_alias_of_active_endpoint_as_already_connected(monkeypatch) -> None:
    with TemporaryDirectory() as tempdir:
        service = _make_service(Path(tempdir) / "chipcoin.sqlite3")
        runtime = NodeRuntime(service=service, listen_host="127.0.0.1", listen_port=18445)

        def fake_getaddrinfo(host: str, port: int, type: int):
            if port != 18444:
                raise OSError("unexpected port")
            if host in {"173.212.193.13", "tiltmediaconsulting.com"}:
                return [(None, None, None, None, ("173.212.193.13", port))]
            raise OSError("unresolvable")

        monkeypatch.setattr("chipcoin.node.runtime.socket.getaddrinfo", fake_getaddrinfo)

        class _FakeState:
            closed = False
            handshake_complete = True
            remote_version = None

        class _FakeSession:
            state = _FakeState()

        session = _FakeSession()
        runtime._sessions[session] = SessionHandle(
            protocol=session,
            outbound=True,
            endpoint=OutboundPeer("173.212.193.13", 18444),
        )

        assert runtime._has_active_endpoint(OutboundPeer("tiltmediaconsulting.com", 18444)) is True


def test_runtime_forget_self_alias_removes_equivalent_peer_targets(monkeypatch) -> None:
    with TemporaryDirectory() as tempdir:
        service = _make_service(Path(tempdir) / "chipcoin.sqlite3")
        service.add_peer("173.212.193.13", 18444, source="manual")
        service.add_peer("tiltmediaconsulting.com", 18444, source="manual")
        runtime = NodeRuntime(service=service, listen_host="127.0.0.1", listen_port=18445)
        runtime._outbound_targets[("173.212.193.13", 18444)] = OutboundPeer("173.212.193.13", 18444)
        runtime._outbound_targets[("tiltmediaconsulting.com", 18444)] = OutboundPeer("tiltmediaconsulting.com", 18444)

        def fake_getaddrinfo(host: str, port: int, type: int):
            if port != 18444:
                raise OSError("unexpected port")
            if host in {"173.212.193.13", "tiltmediaconsulting.com"}:
                return [(None, None, None, None, ("173.212.193.13", port))]
            raise OSError("unresolvable")

        monkeypatch.setattr("chipcoin.node.runtime.socket.getaddrinfo", fake_getaddrinfo)

        class _FakeState:
            closed = False
            handshake_complete = True
            remote_version = None

        class _FakeSession:
            state = _FakeState()

        session = _FakeSession()
        runtime._sessions[session] = SessionHandle(
            protocol=session,
            outbound=True,
            endpoint=OutboundPeer("173.212.193.13", 18444),
        )

        runtime._forget_self_alias(session)

        assert runtime._outbound_targets == {}
        assert not any(peer.port == 18444 and peer.host in {"173.212.193.13", "tiltmediaconsulting.com"} for peer in service.list_peers())


def test_service_remove_peer_deletes_persisted_entry() -> None:
    with TemporaryDirectory() as tempdir:
        service = _make_service(Path(tempdir) / "chipcoin.sqlite3")
        service.add_peer("node-a", 18444, source="manual")

        assert any(peer.host == "node-a" and peer.port == 18444 for peer in service.list_peers())
        service.remove_peer("node-a", 18444)
        assert not any(peer.host == "node-a" and peer.port == 18444 for peer in service.list_peers())


def test_runtime_canonicalizes_peer_aliases_by_node_id() -> None:
    with TemporaryDirectory() as tempdir:
        service = _make_service(Path(tempdir) / "chipcoin.sqlite3")
        service.record_peer_observation(
            host="node-a",
            port=18444,
            direction="outbound",
            handshake_complete=True,
            node_id="node-a-id",
        )
        service.record_peer_observation(
            host="172.18.0.2",
            port=18444,
            direction="outbound",
            handshake_complete=True,
            node_id="node-a-id",
        )
        runtime = NodeRuntime(
            service=service,
            listen_host="127.0.0.1",
            listen_port=18445,
            outbound_peers=[OutboundPeer("node-a", 18444)],
        )

        runtime._canonicalize_peer_aliases(
            "node-a-id",
            canonical_host="172.18.0.2",
            canonical_port=18444,
            prefer_configured=OutboundPeer("node-a", 18444),
        )

        peers = service.list_peers()
        assert any(peer.host == "node-a" and peer.port == 18444 for peer in peers)
        assert not any(peer.host == "172.18.0.2" and peer.port == 18444 for peer in peers)
        assert runtime._desired_outbound_peers() == [OutboundPeer("node-a", 18444)]


def test_runtime_tolerates_one_transient_ping_timeout_before_dropping_session() -> None:
    class _FakeSessionState:
        handshake_complete = True
        closed = False
        remote_version = None

    class _FakeSession:
        def __init__(self) -> None:
            self.state = _FakeSessionState()
            self.ping_calls = 0
            self.close_calls = 0

        async def ping(self, nonce: int, *, timeout: float = 5.0) -> None:
            self.ping_calls += 1
            if self.ping_calls == 1:
                raise TimeoutError("Timed out waiting for pong response.")

        async def close(self, *, reason: str | None = None, error: Exception | None = None) -> None:
            self.close_calls += 1
            self.state.closed = True

    async def scenario() -> None:
        with TemporaryDirectory() as tempdir:
            service = _make_service(Path(tempdir) / "chipcoin.sqlite3")
            runtime = NodeRuntime(service=service, ping_interval=0.01, read_timeout=0.1, max_consecutive_ping_failures=3)
            session = _FakeSession()
            handle = SessionHandle(protocol=session, outbound=False)
            runtime._sessions[session] = handle
            dropped: list[_FakeSession] = []
            penalties: list[str] = []

            async def drop_session(_session) -> None:
                dropped.append(_session)
                runtime._sessions.pop(_session, None)

            runtime._drop_session = drop_session  # type: ignore[method-assign]
            runtime._apply_session_penalty = lambda _session, *, error, penalty: penalties.append(str(error))  # type: ignore[method-assign]
            runtime._format_peer_for_logs = lambda _session: "fake-peer"  # type: ignore[method-assign]
            runtime._running = True

            task = asyncio.create_task(runtime._ping_loop())
            try:
                await asyncio.sleep(0.05)
            finally:
                runtime._running = False
                await task

            assert session.close_calls == 0
            assert dropped == []
            assert penalties == []
            assert handle.consecutive_ping_failures == 0
            assert session.ping_calls >= 2

    asyncio.run(scenario())


def test_runtime_chunks_block_getdata_requests_to_inventory_limit(monkeypatch) -> None:
    async def scenario() -> None:
        with TemporaryDirectory() as tempdir:
            service = _make_service(Path(tempdir) / "chipcoin.sqlite3")
            runtime = NodeRuntime(
                service=service,
                listen_host="127.0.0.1",
                listen_port=18445,
                max_inventory_items=2,
                headers_sync_enabled=False,
            )

            missing_hashes = tuple(f"{index:064x}" for index in range(5))
            monkeypatch.setattr(
                runtime.sync_manager,
                "ingest_headers",
                lambda headers, **_kwargs: HeaderIngestResult(
                    headers_received=len(headers),
                    parent_unknown=None,
                    best_tip_hash=missing_hashes[-1],
                    best_tip_height=4,
                    missing_block_hashes=missing_hashes,
                    needs_more_headers=False,
                ),
            )

            sent_messages: list[MessageEnvelope] = []

            class _FakeSessionState:
                closed = False
                handshake_complete = True
                remote_version = None
                errors: list[str] = []
                error_causes: list[Exception] = []

            class _FakeSession:
                inbound = False
                state = _FakeSessionState()
                transport = type(
                    "_FakeTransport",
                    (),
                    {"peer_endpoint": staticmethod(lambda: type("_Peer", (), {"host": "127.0.0.1", "port": 18444})())},
                )()

                async def send_message(self, message: MessageEnvelope) -> None:
                    sent_messages.append(message)

            session = _FakeSession()
            await runtime._on_peer_message(
                session,
                MessageEnvelope(command="headers", payload=HeadersMessage(headers=())),
            )

            getdata_messages = [message for message in sent_messages if message.command == "getdata"]
            assert [len(message.payload.items) for message in getdata_messages] == [2, 2, 1]
            assert [item.object_hash for message in getdata_messages for item in message.payload.items] == list(missing_hashes)

    asyncio.run(scenario())


def test_runtime_requests_headers_from_parallel_peers_up_to_limit() -> None:
    class _FakeSessionState:
        closed = False
        handshake_complete = True
        errors: list[str] = []
        error_causes: list[Exception] = []

        def __init__(self, node_id: str, start_height: int) -> None:
            self.remote_version = type("_Remote", (), {"node_id": node_id, "start_height": start_height})()

    class _FakeSession:
        inbound = False

        def __init__(self, node_id: str, start_height: int) -> None:
            self.state = _FakeSessionState(node_id, start_height)

        async def send_message(self, message: MessageEnvelope) -> None:
            sent_messages.append((self.state.remote_version.node_id, message.command))

    async def scenario() -> None:
        with TemporaryDirectory() as tempdir:
            service = _make_service(Path(tempdir) / "chipcoin.sqlite3")
            runtime = NodeRuntime(service=service, headers_sync_parallel_peers=2, headers_sync_start_height_gap_threshold=1)
            sessions = [_FakeSession(f"peer-{index}", 10) for index in range(3)]
            for session in sessions:
                runtime._sessions[session] = SessionHandle(
                    protocol=session,
                    outbound=True,
                    endpoint=OutboundPeer("127.0.0.1", 18000 + int(session.state.remote_version.node_id.split("-")[-1])),
                )
            await runtime._drive_header_sync()
            requested = [peer_id for peer_id, command in sent_messages if command == "getheaders"]
            assert requested == ["peer-0", "peer-1"]

    sent_messages: list[tuple[str, str]] = []
    asyncio.run(scenario())


def test_runtime_dispatches_block_downloads_across_multiple_peers(monkeypatch) -> None:
    class _FakeSessionState:
        closed = False
        handshake_complete = True
        errors: list[str] = []
        error_causes: list[Exception] = []

        def __init__(self, node_id: str, start_height: int) -> None:
            self.remote_version = type("_Remote", (), {"node_id": node_id, "start_height": start_height})()

    class _FakeSession:
        inbound = False

        def __init__(self, node_id: str, start_height: int) -> None:
            self.state = _FakeSessionState(node_id, start_height)

        async def send_message(self, message: MessageEnvelope) -> None:
            sent_messages.append((self.state.remote_version.node_id, message))

    async def scenario() -> None:
        with TemporaryDirectory() as tempdir:
            service = _make_service(Path(tempdir) / "chipcoin.sqlite3")
            runtime = NodeRuntime(service=service, max_inventory_items=10)
            sessions = [_FakeSession("peer-a", 20), _FakeSession("peer-b", 20)]
            runtime._sessions[sessions[0]] = SessionHandle(
                protocol=sessions[0],
                outbound=True,
                endpoint=OutboundPeer("127.0.0.1", 18444),
            )
            runtime._sessions[sessions[1]] = SessionHandle(
                protocol=sessions[1],
                outbound=True,
                endpoint=OutboundPeer("127.0.0.1", 18445),
            )
            monkeypatch.setattr(
                runtime.sync_manager,
                "best_header_height",
                lambda: 20,
            )
            monkeypatch.setattr(
                runtime.sync_manager,
                "reserve_block_downloads",
                lambda **_kwargs: (
                    BlockDownloadAssignment(block_hash="aa" * 32, peer_id="peer-a", deadline_at=10.0, attempt=1),
                    BlockDownloadAssignment(block_hash="bb" * 32, peer_id="peer-b", deadline_at=10.0, attempt=1),
                    BlockDownloadAssignment(block_hash="cc" * 32, peer_id="peer-a", deadline_at=10.0, attempt=1),
                ),
            )
            await runtime._dispatch_block_downloads()

            messages_by_peer = {
                peer_id: [item.object_hash for item in message.payload.items]
                for peer_id, message in sent_messages
                if message.command == "getdata"
            }
            assert messages_by_peer == {
                "peer-a": ["aa" * 32, "cc" * 32],
                "peer-b": ["bb" * 32],
            }
            assert runtime._sessions[sessions[0]].inflight_block_hashes == {"aa" * 32, "cc" * 32}
            assert runtime._sessions[sessions[1]].inflight_block_hashes == {"bb" * 32}

    sent_messages: list[tuple[str, MessageEnvelope]] = []
    asyncio.run(scenario())


def test_runtime_reassigns_stalled_block_requests_and_disconnects_repeat_offender(monkeypatch) -> None:
    class _FakeSessionState:
        closed = False
        handshake_complete = True
        remote_version = type("_Remote", (), {"node_id": "peer-a", "start_height": 20})()
        errors: list[str] = []
        error_causes: list[Exception] = []

    class _FakeSession:
        inbound = False
        state = _FakeSessionState()

        async def close(self, *, reason: str | None = None, error: Exception | None = None) -> None:
            close_calls.append((reason, error))
            self.state.closed = True

    async def scenario() -> None:
        with TemporaryDirectory() as tempdir:
            service = _make_service(Path(tempdir) / "chipcoin.sqlite3")
            runtime = NodeRuntime(service=service)
            session = _FakeSession()
            handle = SessionHandle(protocol=session, outbound=True, inflight_block_hashes={"aa" * 32}, block_stall_count=1)
            runtime._sessions[session] = handle
            penalties: list[str] = []
            dropped: list[_FakeSession] = []

            async def drop_session(_session) -> None:
                dropped.append(_session)
                runtime._sessions.pop(_session, None)

            runtime._drop_session = drop_session  # type: ignore[method-assign]
            runtime._apply_session_penalty = lambda _session, *, error, penalty: penalties.append(f"{error}:{penalty}")  # type: ignore[method-assign]
            monkeypatch.setattr(
                runtime.sync_manager,
                "expire_block_requests",
                lambda **_kwargs: (
                    BlockRequestState(block_hash="aa" * 32, peer_id="peer-a", requested_at=0.0, deadline_at=0.0, attempt=2),
                ),
            )
            await runtime._expire_stalled_block_requests()

            assert penalties == ["block request stalled:10"]
            assert close_calls and isinstance(close_calls[0][1], BlockRequestStalledError)
            assert dropped == [session]

    close_calls: list[tuple[str | None, Exception | None]] = []
    asyncio.run(scenario())


def test_runtime_allows_block_download_from_peer_covering_current_window(monkeypatch) -> None:
    with TemporaryDirectory() as tempdir:
        service = _make_service(Path(tempdir) / "chipcoin.sqlite3")
        runtime = NodeRuntime(service=service, block_download_window_size=32)
        session = type(
            "_FakeSession",
            (),
            {
                "state": type(
                    "_FakeState",
                    (),
                    {
                        "closed": False,
                        "handshake_complete": True,
                        "remote_version": type("_Remote", (), {"node_id": "peer-a", "start_height": 5748})(),
                    },
                )(),
            },
        )()
        runtime._sessions[session] = SessionHandle(
            protocol=session,
            outbound=True,
            sync_target_height=5788,
        )
        monkeypatch.setattr(runtime.sync_manager, "block_download_window_end_height", lambda **_kwargs: 5788)

        assert runtime._session_can_download_blocks(session, best_header_height=5813) is True


def test_runtime_does_not_classify_block_request_stall_as_misbehavior() -> None:
    with TemporaryDirectory() as tempdir:
        service = _make_service(Path(tempdir) / "chipcoin.sqlite3")
        service.add_peer("127.0.0.1", 18444, source="manual")
        runtime = NodeRuntime(service=service)
        session = type(
            "_FakeSession",
            (),
            {
                "state": type(
                    "_FakeState",
                    (),
                    {
                        "handshake_complete": True,
                        "remote_version": type("_Remote", (), {"node_id": "peer-a", "start_height": 20})(),
                    },
                )(),
            },
        )()
        runtime._sessions[session] = SessionHandle(
            protocol=session,
            outbound=True,
            endpoint=OutboundPeer("127.0.0.1", 18444),
        )
        events: list[str] = []
        runtime._observe_peer_misbehavior = lambda **kwargs: events.append(str(kwargs["event"]))  # type: ignore[method-assign]

        runtime._apply_session_penalty(session, error=BlockRequestStalledError("block request stalled"), penalty=10)

        assert runtime._should_penalize_as_misbehavior(
            BlockRequestStalledError("block request stalled"),
            handshake_complete=True,
        ) is False
        assert events == []


def test_runtime_logs_applied_block_height(caplog) -> None:
    class _FakeSessionState:
        remote_version = type("_Remote", (), {"node_id": "peer-a", "start_height": 0})()

    class _FakeSession:
        state = _FakeSessionState()

    with TemporaryDirectory() as tempdir:
        service = _make_service(Path(tempdir) / "chipcoin.sqlite3")
        runtime = NodeRuntime(service=service)
        session = _FakeSession()
        runtime._sessions[session] = SessionHandle(
            protocol=session,
            outbound=True,
            endpoint=OutboundPeer("127.0.0.1", 18444),
        )
        mined = _mine_block(service.build_candidate_block("miner").block)
        service.apply_block(mined)
        result = BlockIngestResult(
            block_hash=mined.block_hash(),
            activated_tip=mined.block_hash(),
            reorged=False,
            accepted_blocks=1,
        )

        with caplog.at_level(logging.INFO):
            runtime._log_block_application(session, result, reorged=False)

        assert "block applied peer=127.0.0.1:18444/peer-a height=0" in caplog.text


def test_runtime_does_not_classify_post_handshake_timeouts_as_misbehavior() -> None:
    with TemporaryDirectory() as tempdir:
        service = _make_service(Path(tempdir) / "chipcoin.sqlite3")
        service.add_peer("127.0.0.1", 18444, source="manual")
        runtime = NodeRuntime(service=service)
        session = type(
            "_FakeSession",
            (),
            {
                "state": type(
                    "_FakeState",
                    (),
                    {
                        "handshake_complete": True,
                        "remote_version": type("_Remote", (), {"node_id": "peer-a", "start_height": 20})(),
                    },
                )(),
            },
        )()
        runtime._sessions[session] = SessionHandle(
            protocol=session,
            outbound=True,
            endpoint=OutboundPeer("127.0.0.1", 18444),
        )
        events: list[str] = []
        runtime._observe_peer_misbehavior = lambda **kwargs: events.append(str(kwargs["event"]))  # type: ignore[method-assign]

        runtime._apply_session_penalty(session, error=TransportTimeoutError("Timed out while sending data to peer."), penalty=10)
        runtime._apply_session_penalty(session, error=HandshakeFailedError("Timed out waiting for handshake completion."), penalty=10)

        assert runtime._should_penalize_as_misbehavior(
            TransportTimeoutError("Timed out while sending data to peer."),
            handshake_complete=True,
        ) is False
        assert runtime._should_penalize_as_misbehavior(
            HandshakeFailedError("Timed out waiting for handshake completion."),
            handshake_complete=True,
        ) is False
        assert events == []


def test_connect_loop_does_not_overlap_outbound_dials() -> None:
    async def scenario() -> None:
        with TemporaryDirectory() as tempdir:
            service = _make_service(Path(tempdir) / "chipcoin.sqlite3")
            runtime = NodeRuntime(
                service=service,
                listen_host="127.0.0.1",
                listen_port=18445,
                connect_interval=0.01,
            )
            peer = OutboundPeer("173.212.193.13", 18444)
            runtime._running = True
            attempts: list[str] = []

            runtime._desired_outbound_peers = lambda: [peer]  # type: ignore[method-assign]
            runtime._is_peer_currently_banned = lambda *_args, **_kwargs: False  # type: ignore[method-assign]
            runtime._is_backoff_active = lambda *_args, **_kwargs: False  # type: ignore[method-assign]

            async def slow_connect(_peer: OutboundPeer) -> None:
                attempts.append(f"{_peer.host}:{_peer.port}")
                await asyncio.sleep(0.05)
                runtime._running = False

            runtime._connect_outbound = slow_connect  # type: ignore[method-assign]

            await runtime._connect_loop()

            assert attempts == ["173.212.193.13:18444"]

    asyncio.run(scenario())


def test_connect_loop_respects_max_outbound_session_budget() -> None:
    async def scenario() -> None:
        with TemporaryDirectory() as tempdir:
            service = _make_service(Path(tempdir) / "chipcoin.sqlite3")
            runtime = NodeRuntime(
                service=service,
                listen_host="127.0.0.1",
                listen_port=18445,
                connect_interval=0.01,
                max_outbound_sessions=2,
            )
            peers = [
                OutboundPeer("173.212.193.13", 18444),
                OutboundPeer("188.217.94.86", 18444),
                OutboundPeer("188.218.213.92", 18444),
            ]
            runtime._running = True
            attempts: list[str] = []

            runtime._desired_outbound_peers = lambda: peers  # type: ignore[method-assign]
            runtime._is_peer_currently_banned = lambda *_args, **_kwargs: False  # type: ignore[method-assign]
            runtime._is_backoff_active = lambda *_args, **_kwargs: False  # type: ignore[method-assign]

            async def connect(_peer: OutboundPeer) -> None:
                attempts.append(f"{_peer.host}:{_peer.port}")
                if len(attempts) >= 2:
                    runtime._running = False

            runtime._connect_outbound = connect  # type: ignore[method-assign]

            await runtime._connect_loop()

            assert attempts == [
                "173.212.193.13:18444",
                "188.217.94.86:18444",
            ]

    asyncio.run(scenario())


def test_runtime_rate_limits_repeated_inbound_handshakes_from_same_host() -> None:
    async def scenario() -> None:
        with TemporaryDirectory() as tempdir:
            service = _make_service(Path(tempdir) / "chipcoin.sqlite3")
            runtime = NodeRuntime(
                service=service,
                listen_host="127.0.0.1",
                listen_port=18445,
                inbound_handshake_rate_limit_per_minute=2,
            )
            runtime._running = True
            assert runtime._inbound_rate_limited("198.51.100.10") is False
            assert runtime._inbound_rate_limited("198.51.100.10") is False
            assert runtime._inbound_rate_limited("198.51.100.10") is True

    asyncio.run(scenario())


def test_runtime_skips_sync_when_peer_height_is_not_ahead() -> None:
    async def scenario() -> None:
        with TemporaryDirectory() as tempdir:
            service = _make_service(Path(tempdir) / "chipcoin.sqlite3")
            mined = _mine_block(service.build_candidate_block("CHCminer").block)
            service.apply_block(mined)
            local_height = service.chain_tip().height
            runtime = NodeRuntime(service=service, listen_host="127.0.0.1", listen_port=18445)

            class _State:
                closed = False
                handshake_complete = True
                remote_version = type("_Remote", (), {"node_id": "peer-a", "start_height": local_height})()
                errors: list[str] = []
                error_causes: list[Exception] = []

            class _Session:
                inbound = False
                state = _State()

                async def close(self, *, reason: str = "", error=None) -> None:
                    self.state.closed = True

                async def send_message(self, _message) -> None:
                    pass

            session = _Session()
            requested: list[str] = []

            async def request_headers(*_args, **_kwargs) -> None:
                requested.append("headers")

            async def drive_header_sync() -> None:
                requested.append("drive")

            async def noop(*_args, **_kwargs) -> None:
                return None

            runtime._sessions[session] = SessionHandle(  # type: ignore[arg-type]
                protocol=session,  # type: ignore[arg-type]
                outbound=True,
                endpoint=OutboundPeer("node-a", 18444),
                opened_at=1.0,
            )
            runtime._request_headers = request_headers  # type: ignore[method-assign]
            runtime._drive_header_sync = drive_header_sync  # type: ignore[method-assign]
            runtime._send_known_peers = noop  # type: ignore[method-assign]
            runtime._announce_current_mempool = noop  # type: ignore[method-assign]

            await runtime._on_handshake_complete(session)  # type: ignore[arg-type]

            assert requested == []

    asyncio.run(scenario())


def test_runtime_rejects_invalid_headers_message(monkeypatch) -> None:
    class _FakeSessionState:
        closed = False
        handshake_complete = True
        remote_version = type("_Remote", (), {"node_id": "peer-a", "start_height": 20})()
        errors: list[str] = []
        error_causes: list[Exception] = []

    class _FakeSession:
        inbound = False
        state = _FakeSessionState()

        async def close(self, *, reason: str | None = None, error: Exception | None = None) -> None:
            close_calls.append((reason, error))
            self.state.closed = True

    async def scenario() -> None:
        with TemporaryDirectory() as tempdir:
            service = _make_service(Path(tempdir) / "chipcoin.sqlite3")
            runtime = NodeRuntime(service=service)
            session = _FakeSession()
            runtime._sessions[session] = SessionHandle(protocol=session, outbound=True)
            penalties: list[int] = []
            dropped: list[_FakeSession] = []

            async def drop_session(_session) -> None:
                dropped.append(_session)

            runtime._drop_session = drop_session  # type: ignore[method-assign]
            runtime._apply_session_penalty = lambda _session, *, error, penalty: penalties.append(penalty)  # type: ignore[method-assign]
            monkeypatch.setattr(
                runtime.sync_manager,
                "ingest_headers",
                lambda *args, **kwargs: (_ for _ in ()).throw(ContextualValidationError("bad header linkage")),
            )
            await runtime._on_peer_message(
                session,
                MessageEnvelope(command="headers", payload=HeadersMessage(headers=())),
            )

            assert penalties == [runtime._SEVERE_MISBEHAVIOR_DELTA]
            assert close_calls
            assert dropped == [session]

    close_calls: list[tuple[str | None, Exception | None]] = []
    asyncio.run(scenario())


def test_runtime_drops_session_after_reaching_ping_failure_threshold() -> None:
    class _FakeSessionState:
        handshake_complete = True
        closed = False
        remote_version = None

    class _FakeSession:
        def __init__(self) -> None:
            self.state = _FakeSessionState()
            self.close_calls = 0

        async def ping(self, nonce: int, *, timeout: float = 5.0) -> None:
            raise TimeoutError("Timed out waiting for pong response.")

        async def close(self, *, reason: str | None = None, error: Exception | None = None) -> None:
            self.close_calls += 1
            self.state.closed = True

    async def scenario() -> None:
        with TemporaryDirectory() as tempdir:
            service = _make_service(Path(tempdir) / "chipcoin.sqlite3")
            runtime = NodeRuntime(service=service, ping_interval=0.01, read_timeout=0.1, max_consecutive_ping_failures=2)
            session = _FakeSession()
            runtime._sessions[session] = SessionHandle(protocol=session, outbound=False)
            dropped: list[_FakeSession] = []
            penalties: list[str] = []

            async def drop_session(_session) -> None:
                dropped.append(_session)
                runtime._sessions.pop(_session, None)

            runtime._drop_session = drop_session  # type: ignore[method-assign]
            runtime._apply_session_penalty = lambda _session, *, error, penalty: penalties.append(str(error))  # type: ignore[method-assign]
            runtime._format_peer_for_logs = lambda _session: "fake-peer"  # type: ignore[method-assign]
            runtime._running = True

            task = asyncio.create_task(runtime._ping_loop())
            try:
                await asyncio.sleep(0.05)
            finally:
                runtime._running = False
                await task

            assert session.close_calls == 1
            assert dropped == [session]
            assert penalties == ["Timed out waiting for pong response."]

    asyncio.run(scenario())


def test_runtime_tolerates_ping_timeout_while_peer_is_recently_active() -> None:
    class _FakeSessionState:
        handshake_complete = True
        closed = False
        remote_version = None

    class _FakeSession:
        def __init__(self) -> None:
            self.state = _FakeSessionState()
            self.close_calls = 0
            self.ping_calls = 0

        async def ping(self, nonce: int, *, timeout: float = 5.0) -> None:
            self.ping_calls += 1
            raise TimeoutError("Timed out waiting for pong response.")

        async def close(self, *, reason: str | None = None, error: Exception | None = None) -> None:
            self.close_calls += 1
            self.state.closed = True

    async def scenario() -> None:
        with TemporaryDirectory() as tempdir:
            service = _make_service(Path(tempdir) / "chipcoin.sqlite3")
            runtime = NodeRuntime(service=service, ping_interval=0.01, read_timeout=0.1, max_consecutive_ping_failures=2)
            session = _FakeSession()
            handle = SessionHandle(protocol=session, outbound=False)
            runtime._sessions[session] = handle
            dropped: list[_FakeSession] = []
            penalties: list[str] = []

            async def drop_session(_session) -> None:
                dropped.append(_session)
                runtime._sessions.pop(_session, None)

            runtime._drop_session = drop_session  # type: ignore[method-assign]
            runtime._apply_session_penalty = lambda _session, *, error, penalty: penalties.append(str(error))  # type: ignore[method-assign]
            runtime._format_peer_for_logs = lambda _session: "fake-peer"  # type: ignore[method-assign]
            runtime._running = True
            runtime._mark_session_activity(session)

            task = asyncio.create_task(runtime._ping_loop())
            try:
                await asyncio.sleep(0.05)
            finally:
                runtime._running = False
                await task

            assert session.close_calls == 0
            assert dropped == []
            assert penalties == []
            assert handle.consecutive_ping_failures == 0
            assert session.ping_calls >= 1

    asyncio.run(scenario())


def test_node_service_accepts_transaction_and_builds_candidate_block() -> None:
    with TemporaryDirectory() as tempdir:
        service = _make_service(Path(tempdir) / "chipcoin.sqlite3")
        funding_outpoint = OutPoint(txid="11" * 32, index=0)
        put_wallet_utxo(service, funding_outpoint, value=100, owner=wallet_key(0))
        transaction = _spend_transaction(funding_outpoint, input_value=100, output_value=90)

        accepted = service.receive_transaction(transaction)
        template = service.build_candidate_block("CHCminer")

        assert accepted.fee == 10
        assert service.list_mempool_transactions() == [transaction]
        assert template.total_fees == 10
        assert template.block.transactions[1] == transaction
        assert int(template.block.transactions[0].outputs[0].value) == 50 * 100_000_000 + 10


def test_node_service_rejects_conflicting_mempool_spends_by_policy() -> None:
    with TemporaryDirectory() as tempdir:
        service = _make_service(Path(tempdir) / "chipcoin.sqlite3")
        funding_outpoint = OutPoint(txid="22" * 32, index=0)
        put_wallet_utxo(service, funding_outpoint, value=100, owner=wallet_key(0))

        first = _spend_transaction(funding_outpoint, input_value=100, output_value=90)
        second = _spend_transaction(funding_outpoint, input_value=100, output_value=80)

        service.receive_transaction(first)
        try:
            service.receive_transaction(second)
        except ValidationError:
            pass
        else:
            raise AssertionError("Expected conflicting mempool spend to be rejected.")


def test_node_service_applies_mined_block_and_updates_local_state() -> None:
    with TemporaryDirectory() as tempdir:
        service = _make_service(Path(tempdir) / "chipcoin.sqlite3")
        funding_outpoint = OutPoint(txid="33" * 32, index=0)
        put_wallet_utxo(service, funding_outpoint, value=100, owner=wallet_key(0))
        transaction = _spend_transaction(funding_outpoint, input_value=100, output_value=90)
        service.receive_transaction(transaction)

        template = service.build_candidate_block("CHCminer")
        mined_block = _mine_block(template.block)
        total_fees = service.apply_block(mined_block)

        assert total_fees == 10
        assert service.chain_tip() is not None
        assert service.chain_tip().block_hash == mined_block.block_hash()
        assert service.headers.get(mined_block.block_hash()) == mined_block.header
        assert service.blocks.get(mined_block.block_hash()) == mined_block
        assert service.list_mempool_transactions() == []
        assert service.chainstate.get_utxo(funding_outpoint) is None
        assert service.chainstate.get_utxo(OutPoint(txid=transaction.txid(), index=0)) is not None


def test_node_service_rejects_transaction_with_invalid_signature() -> None:
    with TemporaryDirectory() as tempdir:
        service = _make_service(Path(tempdir) / "chipcoin.sqlite3")
        funding_outpoint = OutPoint(txid="44" * 32, index=0)
        put_wallet_utxo(service, funding_outpoint, value=100, owner=wallet_key(0))
        valid = _spend_transaction(funding_outpoint, input_value=100, output_value=90)
        invalid_input = replace(
            valid.inputs[0],
            signature=valid.inputs[0].signature[:-1] + bytes((valid.inputs[0].signature[-1] ^ 0x01,)),
        )
        invalid = replace(valid, inputs=(invalid_input,))

        try:
            service.receive_transaction(invalid)
        except ValidationError:
            return
        raise AssertionError("Expected invalid signature transaction to be rejected.")


def test_node_service_rejects_transaction_with_tampered_signed_payload() -> None:
    with TemporaryDirectory() as tempdir:
        service = _make_service(Path(tempdir) / "chipcoin.sqlite3")
        funding_outpoint = OutPoint(txid="55" * 32, index=0)
        put_wallet_utxo(service, funding_outpoint, value=100, owner=wallet_key(0))
        valid = _spend_transaction(funding_outpoint, input_value=100, output_value=90)
        tampered = replace(
            valid,
            outputs=(
                replace(valid.outputs[0], value=91),
            ),
        )

        try:
            service.receive_transaction(tampered)
        except ValidationError:
            return
        raise AssertionError("Expected payload-tampered transaction to be rejected.")


def test_node_service_rejects_transaction_below_minimum_mempool_fee_policy() -> None:
    with TemporaryDirectory() as tempdir:
        service = _make_service(Path(tempdir) / "chipcoin.sqlite3")
        service.mempool.policy = MempoolPolicy(min_fee_chipbits_normal_tx=11)
        funding_outpoint = OutPoint(txid="66" * 32, index=0)
        put_wallet_utxo(service, funding_outpoint, value=100, owner=wallet_key(0))
        transaction = _spend_transaction(funding_outpoint, input_value=100, output_value=90)

        try:
            service.receive_transaction(transaction)
        except ValidationError as exc:
            assert "minimum" in str(exc)
            return
        raise AssertionError("Expected below-minimum-fee transaction to be rejected by mempool policy.")


def test_node_service_accepts_transaction_with_sufficient_mempool_fee_policy() -> None:
    with TemporaryDirectory() as tempdir:
        service = _make_service(Path(tempdir) / "chipcoin.sqlite3")
        service.mempool.policy = MempoolPolicy(min_fee_chipbits_normal_tx=10)
        funding_outpoint = OutPoint(txid="77" * 32, index=0)
        put_wallet_utxo(service, funding_outpoint, value=100, owner=wallet_key(0))
        transaction = _spend_transaction(funding_outpoint, input_value=100, output_value=90)

        accepted = service.receive_transaction(transaction)

        assert accepted.fee == 10
        assert service.find_transaction(transaction.txid()) is not None


def test_node_service_rejects_duplicate_transaction_in_mempool() -> None:
    with TemporaryDirectory() as tempdir:
        service = _make_service(Path(tempdir) / "chipcoin.sqlite3")
        funding_outpoint = OutPoint(txid="88" * 32, index=0)
        put_wallet_utxo(service, funding_outpoint, value=100, owner=wallet_key(0))
        transaction = _spend_transaction(funding_outpoint, input_value=100, output_value=90)

        service.receive_transaction(transaction)
        try:
            service.receive_transaction(transaction)
        except ValidationError as exc:
            assert "already present" in str(exc)
            return
        raise AssertionError("Expected duplicate mempool transaction to be rejected.")


def test_node_service_rejects_transaction_with_invalid_output_address_by_policy() -> None:
    with TemporaryDirectory() as tempdir:
        service = _make_service(Path(tempdir) / "chipcoin.sqlite3")
        owner = wallet_key(0)
        funding_outpoint = OutPoint(txid="99" * 32, index=0)
        put_wallet_utxo(service, funding_outpoint, value=100, owner=owner)

        unsigned = Transaction(
            version=1,
            inputs=(TxInput(previous_output=funding_outpoint),),
            outputs=(TxOutput(value=90, recipient="CHC-invalid-address"),),
            metadata={"kind": "payment"},
        )
        digest = transaction_signature_digest(
            unsigned,
            0,
            previous_output=TxOutput(value=100, recipient=owner.address),
        )
        signed = replace(
            unsigned,
            inputs=(
                replace(
                    unsigned.inputs[0],
                    signature=sign_digest(owner.private_key, digest),
                    public_key=owner.public_key,
                ),
            ),
        )

        try:
            service.receive_transaction(signed)
        except ValidationError as exc:
            assert "valid CHC address" in str(exc)
            return
        raise AssertionError("Expected invalid output address to be rejected by mempool policy.")


def test_mempool_eviction_prefers_higher_fee_transactions() -> None:
    with TemporaryDirectory() as tempdir:
        service = _make_service(Path(tempdir) / "chipcoin.sqlite3")
        service.mempool.policy = MempoolPolicy(max_mempool_transactions=1)
        first_outpoint = OutPoint(txid="aa" * 32, index=0)
        second_outpoint = OutPoint(txid="bb" * 32, index=0)
        put_wallet_utxo(service, first_outpoint, value=100, owner=wallet_key(0))
        put_wallet_utxo(service, second_outpoint, value=100, owner=wallet_key(0))
        low_fee = signed_payment(first_outpoint, value=100, sender=wallet_key(0), fee=5)
        high_fee = signed_payment(second_outpoint, value=100, sender=wallet_key(0), fee=10)

        service.receive_transaction(low_fee)
        service.receive_transaction(high_fee)

        mempool_txids = [transaction.txid() for transaction in service.list_mempool_transactions()]
        assert mempool_txids == [high_fee.txid()]


def test_block_template_prefers_higher_fee_rate_over_higher_absolute_fee() -> None:
    with TemporaryDirectory() as tempdir:
        service = _make_service(Path(tempdir) / "chipcoin.sqlite3")
        signer = TransactionSigner(wallet_key(0))
        low_rate_outpoint = OutPoint(txid="ab" * 32, index=0)
        high_rate_outpoint = OutPoint(txid="cd" * 32, index=0)
        put_wallet_utxo(service, low_rate_outpoint, value=500, owner=wallet_key(0))
        put_wallet_utxo(service, high_rate_outpoint, value=200, owner=wallet_key(0))

        low_rate = signer.build_signed_transaction(
            spend_candidates=spend_candidates_for_wallet(low_rate_outpoint, value=500, owner=wallet_key(0)),
            recipient=wallet_key(1).address,
            amount_chipbits=450,
            fee_chipbits=50,
            metadata={"kind": "payment", "padding": "x" * 400},
        ).transaction
        high_rate = signed_payment(high_rate_outpoint, value=200, sender=wallet_key(0), fee=20)

        service.receive_transaction(low_rate)
        service.receive_transaction(high_rate)
        template = service.build_candidate_block(wallet_key(2).address)

        assert template.block.transactions[1].txid() == high_rate.txid()
        assert template.block.transactions[2].txid() == low_rate.txid()


def test_block_template_respects_max_block_weight_limit() -> None:
    with TemporaryDirectory() as tempdir:
        base_service = _make_service(Path(tempdir) / "base.sqlite3")
        outpoint_a = OutPoint(txid="da" * 32, index=0)
        outpoint_b = OutPoint(txid="db" * 32, index=0)
        put_wallet_utxo(base_service, outpoint_a, value=100, owner=wallet_key(0))
        put_wallet_utxo(base_service, outpoint_b, value=100, owner=wallet_key(0))
        tx_a = signed_payment(outpoint_a, value=100, sender=wallet_key(0), fee=10)
        tx_b = signed_payment(outpoint_b, value=100, sender=wallet_key(0), fee=9)

        coinbase_weight = transaction_weight_units(
            Transaction(version=1, inputs=(), outputs=(TxOutput(value=0, recipient=wallet_key(2).address),), metadata={"coinbase": "true", "height": "0"})
        )
        small_limit = coinbase_weight + transaction_weight_units(tx_a) + 1
        constrained_params = replace(MAINNET_PARAMS, coinbase_maturity=0, max_block_weight=small_limit)
        service = _make_service_with_params(Path(tempdir) / "limited.sqlite3", constrained_params)
        put_wallet_utxo(service, outpoint_a, value=100, owner=wallet_key(0))
        put_wallet_utxo(service, outpoint_b, value=100, owner=wallet_key(0))
        service.receive_transaction(tx_a)
        service.receive_transaction(tx_b)

        template = service.build_candidate_block(wallet_key(2).address)

        assert len(template.block.transactions) == 2
        assert template.block.transactions[1].txid() == tx_a.txid()


def test_block_template_orders_parent_before_child() -> None:
    with TemporaryDirectory() as tempdir:
        service = _make_service_with_params(Path(tempdir) / "chipcoin.sqlite3", replace(MAINNET_PARAMS, coinbase_maturity=0))
        funding_outpoint = OutPoint(txid="ea" * 32, index=0)
        put_wallet_utxo(service, funding_outpoint, value=100, owner=wallet_key(0))
        parent = signed_payment(funding_outpoint, value=100, sender=wallet_key(0), recipient=wallet_key(1).address, amount=80, fee=20)
        child = signed_payment(
            OutPoint(txid=parent.txid(), index=0),
            value=int(parent.outputs[0].value),
            sender=wallet_key(1),
            recipient=wallet_key(2).address,
            amount=70,
            fee=10,
        )

        service.receive_transaction(parent)
        service.receive_transaction(child)
        template = service.build_candidate_block(wallet_key(2).address)

        assert [tx.txid() for tx in template.block.transactions[1:3]] == [parent.txid(), child.txid()]


def test_block_template_excludes_child_if_parent_is_absent() -> None:
    with TemporaryDirectory() as tempdir:
        service = _make_service(Path(tempdir) / "chipcoin.sqlite3")
        parent = signed_payment(
            OutPoint(txid="fa" * 32, index=0),
            value=100,
            sender=wallet_key(0),
            recipient=wallet_key(1).address,
            amount=80,
            fee=20,
        )
        child = signed_payment(
            OutPoint(txid=parent.txid(), index=0),
            value=int(parent.outputs[0].value),
            sender=wallet_key(1),
            recipient=wallet_key(2).address,
            amount=70,
            fee=10,
        )
        template = service.mining.build_block_template(
            previous_block_hash="00" * 32,
            height=0,
            miner_address=wallet_key(2).address,
            bits=service.params.genesis_bits,
            mempool_entries=[MempoolEntry(transaction=child, fee=10, added_at=1)],
            node_registry_view=service.node_registry.snapshot(),
            confirmed_transaction_ids=set(),
        )

        assert len(template.block.transactions) == 1


def test_built_block_remains_consensus_valid_under_weight_limit() -> None:
    with TemporaryDirectory() as tempdir:
        params = replace(MAINNET_PARAMS, coinbase_maturity=0)
        service = _make_service_with_params(Path(tempdir) / "chipcoin.sqlite3", params)
        outpoint = OutPoint(txid="fb" * 32, index=0)
        put_wallet_utxo(service, outpoint, value=100, owner=wallet_key(0))
        transaction = signed_payment(outpoint, value=100, sender=wallet_key(0), fee=10)
        service.receive_transaction(transaction)

        template = service.build_candidate_block(wallet_key(1).address)
        mined_block = _mine_block(template.block)
        tip = service.chain_tip()
        context = ValidationContext(
            height=0 if tip is None else tip.height + 1,
            median_time_past=0,
            params=params,
            utxo_view=InMemoryUtxoView.from_entries(service.chainstate.list_utxos()),
            node_registry_view=service.node_registry.snapshot(),
            expected_previous_block_hash="00" * 32,
            expected_bits=params.genesis_bits,
            enforce_coinbase_maturity=False,
        )

        assert validate_block(mined_block, context) == 10
