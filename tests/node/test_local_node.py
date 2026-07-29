from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import asyncio
import json
import logging

from chipcoin.consensus.epoch_settlement import REWARD_ATTESTATION_BUNDLE_KIND, parse_reward_attestation_bundle_metadata
from chipcoin.consensus.params import DEVNET_PARAMS, MAINNET_PARAMS
from chipcoin.consensus.models import Block, BlockHeader, OutPoint, Transaction, TxInput, TxOutput
from chipcoin.consensus.pow import verify_proof_of_work
from chipcoin.consensus.pq_activation import PQ_SUPPORT_TESTNET_ACTIVATION_HEIGHT
from chipcoin.consensus.serialization import serialize_transaction
from chipcoin.consensus.validation import (
    ContextualValidationError,
    ValidationContext,
    validate_block,
    ValidationError,
    transaction_signature_digest,
)
from chipcoin.consensus.utxo import InMemoryUtxoView
from chipcoin.crypto.addresses import public_key_to_pq_address
from chipcoin.crypto.keys import parse_private_key_hex
from chipcoin.crypto.pq import SIG_SCHEME_ML_DSA_44
from chipcoin.crypto.signatures import sign_digest
from chipcoin.node.mempool import MempoolPolicy
from chipcoin.node.mining import transaction_weight_units
from chipcoin.node.peers import PeerInfo, PeerManager
from chipcoin.node.messages import AddrMessage, BlockMessage, GetBlocksMessage, GetDataMessage, GetHeadersMessage, HeadersMessage, InvMessage, InventoryVector, MessageEnvelope, PeerAddress, TransactionMessage
from chipcoin.node.p2p.errors import BlockRequestStalledError, DuplicateConnectionError, InvalidBlockError, InvalidTxError, ProtocolError
from chipcoin.node.p2p.errors import HandshakeFailedError, TransportTimeoutError
from chipcoin.node.runtime import NodeRuntime, OutboundPeer, SessionHandle
from chipcoin.node.p2p.transport import PeerEndpoint
from chipcoin.node.service import NodeService
from chipcoin.node.sync import BlockDownloadAssignment, BlockIngestResult, BlockRequestState, HeaderIngestResult
from chipcoin.storage.peers import SQLitePeerRepository
from chipcoin.storage.mempool import MempoolEntry
from chipcoin.wallet.signer import TransactionSigner, wallet_key_from_mldsa44_seed, wallet_key_from_private_key
from tests.helpers import put_wallet_utxo, signed_payment, spend_candidates_for_wallet, wallet_key


def _make_service(database_path: Path) -> NodeService:
    timestamps = iter(range(1_700_000_000, 1_700_000_100))
    return NodeService.open_sqlite(database_path, time_provider=lambda: next(timestamps))


def _make_service_with_params(database_path: Path, params) -> NodeService:
    timestamps = iter(range(1_700_000_000, 1_700_000_200))
    return NodeService.open_sqlite(database_path, params=params, time_provider=lambda: next(timestamps))


async def _wait_until(predicate, *, timeout: float = 2.0, interval: float = 0.01) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        if predicate():
            return
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("condition was not met before timeout")
        await asyncio.sleep(interval)


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


def _reward_attestation_bundle_transaction(
    *,
    epoch_index: int = 80,
    bundle_window_index: int = 1,
    check_window_index: int | None = None,
    bundle_submitter_node_id: str = "submitter-a",
    candidate_node_id: str = "candidate-a",
    verifier_node_id: str = "verifier-a",
    extra_attestations: tuple[tuple[str, str], ...] = (),
) -> Transaction:
    attestation_window = (
        bundle_window_index
        if check_window_index is None
        else check_window_index
    )
    attestation = {
        "epoch_index": epoch_index,
        "check_window_index": attestation_window,
        "candidate_node_id": candidate_node_id,
        "verifier_node_id": verifier_node_id,
        "result_code": "pass",
        "observed_sync_gap": 0,
        "endpoint_commitment": "endpoint",
        "concentration_key": "concentration",
        "signature_hex": "00" * 64,
    }
    attestations = [attestation]
    for index, (candidate, verifier) in enumerate(extra_attestations, start=1):
        attestations.append(
            {
                **attestation,
                "candidate_node_id": candidate,
                "verifier_node_id": verifier,
                "signature_hex": f"{index:0128x}",
            }
        )
    return Transaction(
        version=1,
        inputs=(),
        outputs=(),
        metadata={
            "kind": REWARD_ATTESTATION_BUNDLE_KIND,
            "epoch_index": str(epoch_index),
            "bundle_window_index": str(bundle_window_index),
            "bundle_submitter_node_id": bundle_submitter_node_id,
            "attestation_count": str(len(attestations)),
            "attestations_json": json.dumps(
                attestations,
                sort_keys=True,
                separators=(",", ":"),
            ),
        },
    )


def test_peer_manager_keeps_local_peerbook() -> None:
    peerbook = PeerManager()
    peer = PeerInfo(host="127.0.0.1", port=8333, network="mainnet")

    peerbook.add(peer)

    assert peerbook.list_all() == [peer]
    assert peerbook.list_all(network="mainnet") == [peer]

    peerbook.remove(peer)
    assert peerbook.list_all() == []


def test_runtime_peerbook_trim_does_not_reload_peers_per_sort_key() -> None:
    with TemporaryDirectory() as tempdir:
        service = _make_service(Path(tempdir) / "chipcoin.sqlite3")
        for index in range(24):
            service.record_peer_observation(
                host=f"198.51.100.{index}",
                port=18444,
                source="discovered",
                handshake_complete=False,
                first_seen=1_700_000_000 + index,
                score=-index,
                last_error="Peer connection closed while reading frame.",
            )
        runtime = NodeRuntime(service=service, peerbook_max_size=16)
        original_list_peers = service.list_peers
        calls = 0

        def counted_list_peers():
            nonlocal calls
            calls += 1
            return original_list_peers()

        service.list_peers = counted_list_peers  # type: ignore[method-assign]

        runtime._trim_peerbook_to_capacity()

        assert calls == 1
        assert len(original_list_peers()) == 16


def test_runtime_clamps_tight_loop_intervals() -> None:
    with TemporaryDirectory() as tempdir:
        service = _make_service(Path(tempdir) / "chipcoin.sqlite3")
        runtime = NodeRuntime(
            service=service,
            connect_interval=0,
            ping_interval=0,
            read_timeout=0,
            write_timeout=0,
            handshake_timeout=0,
        )

        assert runtime.connect_interval == 0.5
        assert runtime.ping_interval == 0.5
        assert runtime.read_timeout == 1.0
        assert runtime.write_timeout == 1.0
        assert runtime.handshake_timeout == 1.0


def test_runtime_stack_dump_signal_registration_is_best_effort() -> None:
    with TemporaryDirectory() as tempdir:
        service = _make_service(Path(tempdir) / "chipcoin.sqlite3")
        runtime = NodeRuntime(service=service)

        with patch("chipcoin.node.runtime.faulthandler.register", side_effect=RuntimeError("unavailable")):
            runtime._enable_stack_dump_signal()


def test_runtime_desired_outbound_peers_does_not_reload_peers_per_filter() -> None:
    with TemporaryDirectory() as tempdir:
        service = _make_service(Path(tempdir) / "chipcoin.sqlite3")
        for index in range(24):
            service.record_peer_observation(
                host=f"node-{index}.example",
                port=18444,
                source="manual",
                success_count=1,
                last_success=1_700_000_000 + index,
                score=1,
            )
        runtime = NodeRuntime(service=service)
        original_list_peers = service.list_peers
        calls = 0

        def counted_list_peers():
            nonlocal calls
            calls += 1
            return original_list_peers()

        service.list_peers = counted_list_peers  # type: ignore[method-assign]

        desired = runtime._desired_outbound_peers()

        assert calls == 1
        assert desired


def test_runtime_canonicalizes_inbound_peer_when_canonical_record_is_stale() -> None:
    with TemporaryDirectory() as tempdir:
        service = _make_service(Path(tempdir) / "chipcoin.sqlite3")
        service.record_peer_observation(
            host="8.8.8.8",
            port=8333,
            source="manual",
            handshake_complete=False,
            node_id="old-node",
            last_error="Peer connection closed while reading frame.",
        )
        runtime = NodeRuntime(service=service)

        canonical = runtime._canonicalize_reusable_inbound_endpoint(
            PeerEndpoint("8.8.8.8", 53124),
            inbound=True,
            node_id="new-node",
        )

        assert canonical == OutboundPeer("8.8.8.8", 8333)


def test_runtime_does_not_reassign_active_canonical_peer_to_different_node_id() -> None:
    with TemporaryDirectory() as tempdir:
        service = _make_service(Path(tempdir) / "chipcoin.sqlite3")
        service.record_peer_observation(
            host="8.8.4.4",
            port=8333,
            source="manual",
            handshake_complete=True,
            node_id="active-node",
            last_success=1_700_000_000,
            last_error=None,
        )
        runtime = NodeRuntime(service=service)

        canonical = runtime._canonicalize_reusable_inbound_endpoint(
            PeerEndpoint("8.8.4.4", 53124),
            inbound=True,
            node_id="other-node",
        )

        assert canonical is None


def test_node_service_opens_devnet_with_devnet_params() -> None:
    with TemporaryDirectory() as tempdir:
        service = NodeService.open_sqlite(Path(tempdir) / "chipcoin-devnet.sqlite3", network="devnet")

        assert service.network == "devnet"
        assert service.params == DEVNET_PARAMS


def test_status_ignores_harmless_stalled_peers_when_synced() -> None:
    with TemporaryDirectory() as tempdir:
        service = _make_service(Path(tempdir) / "chipcoin.sqlite3")
        service.record_peer_observation(
            host="node-a",
            port=8333,
            source="manual",
            direction="outbound",
            handshake_complete=True,
            node_id="peer-a",
            last_success=1_700_000_010,
            success_count=1,
            score=1,
            last_known_height=999,
        )
        service.set_runtime_sync_status(
            {
                "mode": "synced",
                "phase": "synced",
                "local_height": 999,
                "remote_height": 999,
                "validated_tip_height": 999,
                "validated_tip_hash": "aa" * 32,
                "best_header_height": 999,
                "best_header_hash": "aa" * 32,
                "missing_block_count": 0,
                "queued_block_count": 0,
                "inflight_block_count": 0,
                "stalled_peers": ({"peer_id": "peer-b", "stall_count": 2},),
            }
        )

        summary = service.status()["operator_summary"]

        assert summary["connectivity_state"] == "connected"
        assert "stalled_peers_present" not in summary["warnings"]
        assert summary["peer_attention"] is False


def test_status_warns_for_stalled_peers_when_sync_debt_remains() -> None:
    with TemporaryDirectory() as tempdir:
        service = _make_service(Path(tempdir) / "chipcoin.sqlite3")
        service.record_peer_observation(
            host="node-a",
            port=8333,
            source="manual",
            direction="outbound",
            handshake_complete=True,
            node_id="peer-a",
            last_success=1_700_000_010,
            success_count=1,
            score=1,
            last_known_height=1000,
        )
        service.set_runtime_sync_status(
            {
                "mode": "blocks",
                "phase": "syncing_from_genesis",
                "local_height": 990,
                "remote_height": 1000,
                "validated_tip_height": 990,
                "validated_tip_hash": "aa" * 32,
                "best_header_height": 1000,
                "best_header_hash": "bb" * 32,
                "missing_block_count": 10,
                "queued_block_count": 8,
                "inflight_block_count": 2,
                "stalled_peers": ({"peer_id": "peer-a", "stall_count": 2},),
            }
        )

        warnings = service.status()["operator_summary"]["warnings"]

        assert "stalled_peers_present" in warnings
        assert "missing_blocks_for_best_header" in warnings


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


def test_runtime_start_clears_persisted_handshake_session_state() -> None:
    async def scenario() -> None:
        with TemporaryDirectory() as tempdir:
            database_path = Path(tempdir) / "chipcoin-devnet.sqlite3"
            service = NodeService.open_sqlite(
                database_path,
                network="devnet",
                time_provider=lambda: 1_700_000_020,
            )
            service.record_peer_observation(
                host="188.217.94.86",
                port=18444,
                source="discovered",
                direction="outbound",
                handshake_complete=True,
                last_success=1_700_000_010,
                last_known_height=4148,
                node_id="tobia-node-id",
                session_started_at=1_700_000_010,
            )
            runtime = NodeRuntime(service=service, listen_host="127.0.0.1", listen_port=18445, http_port=None)

            class _FakeSocket:
                def getsockname(self):
                    return ("127.0.0.1", 18445)

            class _FakeServer:
                sockets = [_FakeSocket()]

                def close(self) -> None:
                    pass

                async def wait_closed(self) -> None:
                    pass

            async def _fake_start_server(*args, **kwargs):
                return _FakeServer()

            with patch("chipcoin.node.runtime.asyncio.start_server", new=_fake_start_server):
                await runtime.start()
                await runtime.stop()

            restarted = NodeService.open_sqlite(database_path, network="devnet")
            peer = next(peer for peer in restarted.list_peers() if peer.host == "188.217.94.86")
            assert peer.handshake_complete is False
            assert peer.session_started_at is None
            assert peer.last_success == 1_700_000_010
            assert peer.last_known_height == 4148
            assert peer.node_id == "tobia-node-id"

    asyncio.run(scenario())


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


def test_runtime_extends_backoff_for_terminal_peer_churn() -> None:
    with TemporaryDirectory() as tempdir:
        now = 1_700_000_000
        service = NodeService.open_sqlite(
            Path(tempdir) / "chipcoin.sqlite3",
            network="devnet",
            time_provider=lambda: now,
        )
        runtime = NodeRuntime(service=service, listen_host="127.0.0.1", listen_port=18445)
        service.record_peer_observation(
            host="188.218.213.92",
            port=18444,
            direction="outbound",
            handshake_complete=False,
            score=-100,
            reconnect_attempts=280,
            backoff_until=now + 30,
            disconnect_count=280,
            last_error="Peer connection closed while reading frame.",
        )
        info = next(peer for peer in service.list_peers() if peer.host == "188.218.213.92")

        attempts, backoff_until = runtime._next_backoff_state(info)

        assert attempts == 281
        assert backoff_until - now == runtime._EXTENDED_BACKOFF_MAX_SECONDS


def test_runtime_caches_reward_assignments_for_current_tip() -> None:
    with TemporaryDirectory() as tempdir:
        service = _make_service(Path(tempdir) / "chipcoin.sqlite3")
        runtime = NodeRuntime(service=service, listen_host="127.0.0.1", listen_port=18445)
        calls: list[int] = []

        def fake_assignments(*, epoch_index: int | None = None, node_id: str | None = None):
            assert node_id is None
            calls.append(-1 if epoch_index is None else epoch_index)
            return [{"epoch_index": epoch_index, "node_id": "reward-node-a"}]

        service.native_reward_assignments = fake_assignments  # type: ignore[method-assign]

        assert runtime._reward_assignments(77) == [{"epoch_index": 77, "node_id": "reward-node-a"}]
        assert runtime._reward_assignments(77) == [{"epoch_index": 77, "node_id": "reward-node-a"}]
        assert runtime._reward_assignments(78) == [{"epoch_index": 78, "node_id": "reward-node-a"}]
        assert calls == [77, 78]


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


def test_runtime_truncates_oversized_addr_gossip_without_dropping_peer() -> None:
    async def scenario() -> None:
        with TemporaryDirectory() as tempdir:
            service = NodeService.open_sqlite(Path(tempdir) / "chipcoin-devnet.sqlite3", network="devnet")
            runtime = NodeRuntime(
                service=service,
                listen_host="127.0.0.1",
                listen_port=18445,
                peer_addr_max_per_message=2,
            )

            class _FakeSessionState:
                closed = False
                handshake_complete = True
                remote_version = None
                errors: list[str] = []
                error_causes: list[Exception] = []

            class _FakeSession:
                inbound = False
                state = _FakeSessionState()
                close_calls = 0

                async def close(self, *, reason: str | None = None, error: Exception | None = None) -> None:
                    self.close_calls += 1

            session = _FakeSession()
            await runtime._on_peer_message(
                session,
                MessageEnvelope(
                    command="addr",
                    payload=AddrMessage(
                        addresses=(
                            PeerAddress(host="188.218.213.10", port=18444, services=0, timestamp=1_700_000_000),
                            PeerAddress(host="188.218.213.11", port=18444, services=0, timestamp=1_700_000_000),
                            PeerAddress(host="188.218.213.12", port=18444, services=0, timestamp=1_700_000_000),
                        )
                    ),
                ),
            )

            peers = service.list_peers()
            assert session.close_calls == 0
            assert any(peer.host == "188.218.213.10" and peer.port == 18444 for peer in peers)
            assert any(peer.host == "188.218.213.11" and peer.port == 18444 for peer in peers)
            assert not any(peer.host == "188.218.213.12" and peer.port == 18444 for peer in peers)

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


def test_runtime_startup_keeps_persisted_manual_peer_with_healthy_persisted_peers() -> None:
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
        service.record_peer_observation(
            host="188.217.94.86",
            port=18444,
            source="discovered",
            handshake_complete=True,
            success_count=8,
            last_success=1_700_000_020,
            score=10,
        )
        service.add_peer("tiltmediaconsulting.com", 18444, source="manual")
        runtime = NodeRuntime(service=service, listen_host="127.0.0.1", listen_port=18445)

        desired = runtime._desired_outbound_peers()

        assert desired[0] == OutboundPeer("tiltmediaconsulting.com", 18444)
        assert OutboundPeer("188.218.213.92", 18444) in desired
        assert OutboundPeer("188.217.94.86", 18444) in desired


def test_add_peer_promotes_existing_seed_to_manual_and_keeps_it_manual() -> None:
    with TemporaryDirectory() as tempdir:
        service = _make_service(Path(tempdir) / "chipcoin.sqlite3")

        service.add_peer("appartamento4310.zapto.org", 18444, source="seed")
        promoted = service.add_peer("appartamento4310.zapto.org", 18444, source="manual")
        service.add_peer("appartamento4310.zapto.org", 18444, source="seed")

        assert promoted.source == "manual"
        [peer] = [
            peer
            for peer in service.list_peers()
            if peer.host == "appartamento4310.zapto.org" and peer.port == 18444
        ]
        assert peer.source == "manual"


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


def test_runtime_send_known_peers_does_not_reload_peers_per_filter() -> None:
    async def scenario() -> None:
        with TemporaryDirectory() as tempdir:
            service = NodeService.open_sqlite(
                Path(tempdir) / "chipcoin-devnet.sqlite3",
                network="devnet",
                time_provider=lambda: 1_700_000_100,
            )
            for index in range(24):
                service.record_peer_observation(
                    host=f"198.51.100.{index}",
                    port=18444,
                    source="discovered",
                    success_count=1,
                    last_success=1_700_000_000 + index,
                )
            runtime = NodeRuntime(service=service, listen_host="127.0.0.1", listen_port=18445)
            original_list_peers = service.list_peers
            calls = 0

            def counted_list_peers():
                nonlocal calls
                calls += 1
                return original_list_peers()

            service.list_peers = counted_list_peers  # type: ignore[method-assign]

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
                    pass

            session = _FakeSession()
            runtime._sessions[session] = SessionHandle(protocol=session, outbound=False)

            await runtime._send_known_peers(session)

            assert calls == 1

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


def test_runtime_does_not_treat_inbound_alias_as_outbound_endpoint(monkeypatch) -> None:
    with TemporaryDirectory() as tempdir:
        service = _make_service(Path(tempdir) / "chipcoin.sqlite3")
        runtime = NodeRuntime(service=service, listen_host="127.0.0.1", listen_port=18445)

        def fake_getaddrinfo(host: str, port: int, type: int):
            if host in {"188.218.213.92", "appartamento4310.zapto.org"}:
                return [(None, None, None, None, ("188.218.213.92", port))]
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
            outbound=False,
            endpoint=OutboundPeer("188.218.213.92", 61375),
        )

        assert runtime._has_active_endpoint(OutboundPeer("appartamento4310.zapto.org", 18444)) is False


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


def test_runtime_canonicalizes_peer_aliases_by_node_id(caplog) -> None:
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
        service.record_peer_observation(
            host="172.18.0.3",
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

        with caplog.at_level(logging.INFO):
            runtime._canonicalize_peer_aliases(
                "node-a-id",
                canonical_host="172.18.0.2",
                canonical_port=18444,
                prefer_configured=OutboundPeer("node-a", 18444),
            )

        peers = service.list_peers()
        assert any(peer.host == "node-a" and peer.port == 18444 for peer in peers)
        assert not any(peer.host == "172.18.0.2" and peer.port == 18444 for peer in peers)
        assert not any(peer.host == "172.18.0.3" and peer.port == 18444 for peer in peers)
        assert runtime._desired_outbound_peers() == [OutboundPeer("node-a", 18444)]
        assert caplog.text.count("removed peer aliases node_id=node-a-id") == 1
        assert "count=2" in caplog.text


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
                await _wait_until(lambda: session.ping_calls >= 2)
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


def test_runtime_tx_inventory_uses_mempool_lookup_without_chain_scan(monkeypatch) -> None:
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

    async def scenario() -> None:
        with TemporaryDirectory() as tempdir:
            service = _make_service(Path(tempdir) / "chipcoin.sqlite3")
            runtime = NodeRuntime(service=service)
            txid = "aa" * 32

            def fail_historical_lookup(_txid: str):
                raise AssertionError("tx inventory must not scan historical blocks")

            monkeypatch.setattr(service, "get_transaction", fail_historical_lookup)

            await runtime._on_peer_message(
                _FakeSession(),
                MessageEnvelope(
                    command="inv",
                    payload=InvMessage(items=(InventoryVector(object_type="tx", object_hash=txid),)),
                ),
            )

            assert [message.command for message in sent_messages] == ["getdata"]
            assert sent_messages[0].payload.items == (InventoryVector(object_type="tx", object_hash=txid),)

    sent_messages: list[MessageEnvelope] = []
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
                "has_pending_block_downloads",
                lambda: True,
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
        monkeypatch.setattr(
            runtime.sync_manager,
            "sync_status",
            lambda: {"download_window": {"start_height": 5788, "end_height": 5813, "size": 26}},
        )

        assert runtime._session_can_download_blocks(session, best_header_height=5813) is True


def test_runtime_activates_ready_best_chain_when_no_blocks_are_missing() -> None:
    with TemporaryDirectory() as tempdir:
        source = _make_service(Path(tempdir) / "source.sqlite3")
        target = _make_service(Path(tempdir) / "target.sqlite3")
        blocks: list[Block] = []
        for _ in range(3):
            template = source.build_candidate_block("CHCminer-source")
            block = _mine_block(template.block)
            source.apply_block(block)
            blocks.append(block)

        manager = NodeRuntime(service=target).sync_manager
        manager.ingest_headers(tuple(block.header for block in blocks), peer_id="peer-a")
        for block in blocks:
            target.blocks.put(block)
        manager._missing_blocks_cache = {}

        status_before = manager.sync_status()
        assert status_before["best_header_height"] == 2
        assert status_before["validated_tip_height"] is None
        assert status_before["missing_block_count"] == 0

        runtime = NodeRuntime(service=target)
        runtime._activate_ready_best_chain()

        assert target.chain_tip() is not None
        assert target.chain_tip().block_hash == blocks[-1].block_hash()


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


def test_runtime_logs_side_branch_stored_distinctly(caplog) -> None:
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
        main_block = _mine_block(service.build_candidate_block("miner-main").block)
        service.apply_block(main_block)
        side_result = BlockIngestResult(
            block_hash="11" * 32,
            activated_tip=main_block.block_hash(),
            reorged=False,
            accepted_blocks=1,
        )

        with caplog.at_level(logging.INFO):
            runtime._log_block_application(session, side_result, reorged=False)

        assert "side branch stored peer=127.0.0.1:18444/peer-a height=0" in caplog.text
        assert f"activated_tip={main_block.block_hash()}" in caplog.text
        assert "accepted_blocks=1" in caplog.text
        assert "reorged=False" in caplog.text


def test_runtime_ignores_duplicate_known_block_payload(caplog) -> None:
    async def scenario() -> None:
        class _FakeSessionState:
            closed = False
            handshake_complete = True
            remote_version = type("_Remote", (), {"node_id": "peer-a", "start_height": 0})()

        class _FakeSession:
            state = _FakeSessionState()

            async def send_message(self, message: MessageEnvelope) -> None:
                raise AssertionError("duplicate known blocks must not be rebroadcast")

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
            runtime.sync_manager.receive_block = lambda block: (_ for _ in ()).throw(  # type: ignore[method-assign]
                AssertionError("known duplicate block should not enter sync ingestion")
            )

            with caplog.at_level(logging.INFO):
                await runtime._on_peer_message(
                    session,
                    MessageEnvelope(command="block", payload=BlockMessage(block=mined)),
                )

            assert "duplicate block ignored peer=127.0.0.1:18444/peer-a height=0" in caplog.text

    asyncio.run(scenario())


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


def test_runtime_treats_premature_reward_relay_as_benign_during_sync() -> None:
    with TemporaryDirectory() as tempdir:
        service = _make_service(Path(tempdir) / "chipcoin.sqlite3")
        service.set_runtime_sync_status(
            {
                "mode": "blocks",
                "phase": "syncing_from_genesis",
                "local_height": 75,
                "remote_height": 1801,
                "validated_tip_height": 75,
                "validated_tip_hash": "aa" * 32,
                "best_header_height": 1801,
                "best_header_hash": "bb" * 32,
                "missing_block_count": 1726,
                "queued_block_count": 1718,
                "inflight_block_count": 8,
            }
        )
        runtime = NodeRuntime(service=service)

        assert runtime._has_sync_debt() is True
        assert runtime._is_benign_tx_relay_error(
            "reward_attestation_bundle transactions are not active before node_reward_activation_height."
        ) is True
        assert runtime._is_benign_tx_relay_error(
            "reward_settle_epoch transactions are not active before node_reward_activation_height."
        ) is True
        assert runtime._is_benign_tx_relay_error(
            "Transaction is already confirmed in the active chain."
        ) is True


def test_runtime_penalizes_premature_reward_relay_when_synced() -> None:
    with TemporaryDirectory() as tempdir:
        service = _make_service(Path(tempdir) / "chipcoin.sqlite3")
        service.set_runtime_sync_status(
            {
                "mode": "synced",
                "phase": "synced",
                "local_height": 75,
                "remote_height": 75,
                "validated_tip_height": 75,
                "validated_tip_hash": "aa" * 32,
                "best_header_height": 75,
                "best_header_hash": "aa" * 32,
                "missing_block_count": 0,
                "queued_block_count": 0,
                "inflight_block_count": 0,
            }
        )
        runtime = NodeRuntime(service=service)

        assert runtime._has_sync_debt() is False
        assert runtime._is_benign_tx_relay_error(
            "reward_attestation_bundle transactions are not active before node_reward_activation_height."
        ) is False


def test_runtime_penalizes_non_standard_tx_relay_more_aggressively() -> None:
    with TemporaryDirectory() as tempdir:
        service = _make_service(Path(tempdir) / "chipcoin.sqlite3")
        runtime = NodeRuntime(service=service)

        assert (
            runtime._tx_relay_penalty("Transaction exceeds mempool size policy.")
            == runtime._NON_STANDARD_TX_MISBEHAVIOR_DELTA
        )
        assert (
            runtime._tx_relay_penalty("Transaction output recipient is not a valid CHC address.")
            == runtime._NON_STANDARD_TX_MISBEHAVIOR_DELTA
        )
        assert runtime._tx_relay_penalty("Missing UTXO for input.") == 5


def test_runtime_misbehavior_log_includes_peer_identity_and_action(caplog) -> None:
    with TemporaryDirectory() as tempdir:
        service = _make_service(Path(tempdir) / "chipcoin.sqlite3")
        runtime = NodeRuntime(service=service)

        class _FakeRemote:
            node_id = "bad-relay-peer"
            start_height = 12

        class _FakeState:
            closed = False
            handshake_complete = True
            remote_version = _FakeRemote()

        class _FakeSession:
            inbound = False
            state = _FakeState()
            transport = None

        session = _FakeSession()
        runtime._sessions[session] = SessionHandle(
            protocol=session,
            outbound=True,
            endpoint=OutboundPeer("198.51.100.21", 18444),
        )

        with caplog.at_level(logging.INFO, logger="chipcoin.node.runtime"):
            action = runtime._apply_session_penalty(
                session,
                error=InvalidTxError("invalid tx: Transaction exceeds mempool size policy."),
                penalty=runtime._NON_STANDARD_TX_MISBEHAVIOR_DELTA,
            )

        assert action == "warn"
        assert "peer misbehavior peer=198.51.100.21:18444" in caplog.text
        assert "node_id=bad-relay-peer" in caplog.text
        assert "event=invalid_tx" in caplog.text
        assert "protocol_error_class=invalid_tx" in caplog.text
        assert "score_delta=25" in caplog.text
        assert "action=warn" in caplog.text


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


def test_runtime_rejects_getheaders_with_too_many_locator_hashes() -> None:
    class _FakeSessionState:
        closed = False
        handshake_complete = True
        remote_version = type("_Remote", (), {"node_id": "peer-a", "start_height": 20})()
        errors: list[str] = []
        error_causes: list[Exception] = []

    class _FakeSession:
        inbound = False
        state = _FakeSessionState()
        sent_messages: list[MessageEnvelope] = []

        async def close(self, *, reason: str | None = None, error: Exception | None = None) -> None:
            close_calls.append((reason, error))
            self.state.closed = True

        async def send_message(self, message: MessageEnvelope) -> None:
            self.sent_messages.append(message)

    async def scenario() -> None:
        with TemporaryDirectory() as tempdir:
            service = _make_service(Path(tempdir) / "chipcoin.sqlite3")
            runtime = NodeRuntime(service=service, max_locator_hashes=2)
            session = _FakeSession()
            runtime._sessions[session] = SessionHandle(protocol=session, outbound=True)
            penalties: list[int] = []
            dropped: list[_FakeSession] = []
            service_calls = 0

            async def drop_session(_session) -> None:
                dropped.append(_session)

            def handle_getheaders(_request, *, limit=2000):
                nonlocal service_calls
                del limit
                service_calls += 1
                return HeadersMessage(headers=())

            runtime._drop_session = drop_session  # type: ignore[method-assign]
            runtime._apply_session_penalty = lambda _session, *, error, penalty: penalties.append(penalty)  # type: ignore[method-assign]
            service.handle_getheaders = handle_getheaders  # type: ignore[method-assign]

            await runtime._on_peer_message(
                session,
                MessageEnvelope(
                    command="getheaders",
                    payload=GetHeadersMessage(
                        protocol_version=1,
                        locator_hashes=("11" * 32, "22" * 32, "33" * 32),
                        stop_hash="00" * 32,
                    ),
                ),
            )

            assert service_calls == 0
            assert penalties == [25]
            assert close_calls
            assert "getheaders locator hash count exceeded limit" in str(close_calls[0][1])
            assert dropped == [session]
            assert session.sent_messages == []

    close_calls: list[tuple[str | None, Exception | None]] = []
    asyncio.run(scenario())


def test_runtime_rejects_getblocks_with_too_many_locator_hashes() -> None:
    class _FakeSessionState:
        closed = False
        handshake_complete = True
        remote_version = type("_Remote", (), {"node_id": "peer-a", "start_height": 20})()
        errors: list[str] = []
        error_causes: list[Exception] = []

    class _FakeSession:
        inbound = False
        state = _FakeSessionState()
        sent_messages: list[MessageEnvelope] = []

        async def close(self, *, reason: str | None = None, error: Exception | None = None) -> None:
            close_calls.append((reason, error))
            self.state.closed = True

        async def send_message(self, message: MessageEnvelope) -> None:
            self.sent_messages.append(message)

    async def scenario() -> None:
        with TemporaryDirectory() as tempdir:
            service = _make_service(Path(tempdir) / "chipcoin.sqlite3")
            runtime = NodeRuntime(service=service, max_locator_hashes=2)
            session = _FakeSession()
            runtime._sessions[session] = SessionHandle(protocol=session, outbound=True)
            penalties: list[int] = []
            dropped: list[_FakeSession] = []
            service_calls = 0

            async def drop_session(_session) -> None:
                dropped.append(_session)

            def handle_getblocks(_request, *, limit=500):
                nonlocal service_calls
                del limit
                service_calls += 1
                return InvMessage(items=())

            runtime._drop_session = drop_session  # type: ignore[method-assign]
            runtime._apply_session_penalty = lambda _session, *, error, penalty: penalties.append(penalty)  # type: ignore[method-assign]
            service.handle_getblocks = handle_getblocks  # type: ignore[method-assign]

            await runtime._on_peer_message(
                session,
                MessageEnvelope(
                    command="getblocks",
                    payload=GetBlocksMessage(
                        protocol_version=1,
                        locator_hashes=("11" * 32, "22" * 32, "33" * 32),
                        stop_hash="00" * 32,
                    ),
                ),
            )

            assert service_calls == 0
            assert penalties == [25]
            assert close_calls
            assert "getblocks locator hash count exceeded limit" in str(close_calls[0][1])
            assert dropped == [session]
            assert session.sent_messages == []

    close_calls: list[tuple[str | None, Exception | None]] = []
    asyncio.run(scenario())


def test_runtime_updates_peer_height_from_getdata_requests() -> None:
    class _FakeSessionState:
        closed = False
        handshake_complete = True
        remote_version = type("_Remote", (), {"node_id": "peer-a", "start_height": 0})()
        errors: list[str] = []
        error_causes: list[Exception] = []

    class _FakeSession:
        inbound = True
        state = _FakeSessionState()
        transport = None

        async def send_message(self, message: MessageEnvelope) -> None:
            sent_messages.append(message)

    async def scenario() -> None:
        with TemporaryDirectory() as tempdir:
            service = _make_service(Path(tempdir) / "chipcoin.sqlite3")
            first = _mine_block(service.build_candidate_block("CHCtest").block)
            service.apply_block(first)
            second = _mine_block(service.build_candidate_block("CHCtest").block)
            service.apply_block(second)
            second_hash = second.block_hash()
            service.record_peer_observation(
                host="node-a",
                port=18444,
                source="manual",
                direction="inbound",
                handshake_complete=True,
                node_id="peer-a",
                last_success=1_700_000_010,
                success_count=1,
                last_known_height=0,
            )
            runtime = NodeRuntime(service=service)
            session = _FakeSession()
            runtime._sessions[session] = SessionHandle(  # type: ignore[arg-type]
                protocol=session,  # type: ignore[arg-type]
                outbound=False,
                endpoint=OutboundPeer("node-a", 18444),
                opened_at=1.0,
            )

            await runtime._on_peer_message(
                session,  # type: ignore[arg-type]
                MessageEnvelope(
                    command="getdata",
                    payload=GetDataMessage(items=(InventoryVector(object_type="block", object_hash=second_hash),)),
                ),
            )

            peer = next(peer for peer in service.list_peers() if peer.node_id == "peer-a")
            assert peer.last_known_height == 1
            assert sent_messages

    sent_messages: list[MessageEnvelope] = []
    asyncio.run(scenario())


def test_runtime_deduplicates_getdata_requests_and_logs_penalty(caplog) -> None:
    class _FakeSessionState:
        closed = False
        handshake_complete = True
        remote_version = type("_Remote", (), {"node_id": "peer-a", "start_height": 0})()
        errors: list[str] = []
        error_causes: list[Exception] = []

    class _FakeSession:
        inbound = True
        state = _FakeSessionState()
        transport = None

        async def send_message(self, message: MessageEnvelope) -> None:
            sent_messages.append(message)

    async def scenario() -> None:
        with TemporaryDirectory() as tempdir:
            service = _make_service(Path(tempdir) / "chipcoin.sqlite3")
            block = _mine_block(service.build_candidate_block("CHCtest").block)
            service.apply_block(block)
            block_hash = block.block_hash()
            runtime = NodeRuntime(service=service)
            session = _FakeSession()
            runtime._sessions[session] = SessionHandle(  # type: ignore[arg-type]
                protocol=session,  # type: ignore[arg-type]
                outbound=False,
                endpoint=OutboundPeer("node-a", 18444),
                opened_at=1.0,
            )
            penalties: list[int] = []

            def record_penalty(_session, *, error, penalty):
                penalties.append(penalty)
                return "observe"

            runtime._apply_session_penalty = record_penalty  # type: ignore[method-assign]

            with caplog.at_level(logging.INFO, logger="chipcoin.node.runtime"):
                await runtime._on_peer_message(
                    session,  # type: ignore[arg-type]
                    MessageEnvelope(
                        command="getdata",
                        payload=GetDataMessage(
                            items=(
                                InventoryVector(object_type="block", object_hash=block_hash),
                                InventoryVector(object_type="block", object_hash=block_hash),
                            )
                        ),
                    ),
                )

            assert [message.command for message in sent_messages] == ["block"]
            assert penalties == [1]
            assert "duplicate getdata requests peer=node-a:18444/peer-a duplicate_items=1 unique_items=1" in caplog.text
            assert "served getdata peer=node-a:18444/peer-a requested_blocks=1 served_blocks=1" in caplog.text
            assert "duplicate_items=1" in caplog.text

    sent_messages: list[MessageEnvelope] = []
    asyncio.run(scenario())


def test_runtime_logs_and_penalizes_repeated_getdata_misses(caplog) -> None:
    class _FakeSessionState:
        closed = False
        handshake_complete = True
        remote_version = type("_Remote", (), {"node_id": "peer-a", "start_height": 0})()
        errors: list[str] = []
        error_causes: list[Exception] = []

    class _FakeSession:
        inbound = True
        state = _FakeSessionState()
        transport = None

        async def send_message(self, message: MessageEnvelope) -> None:
            sent_messages.append(message)

    async def scenario() -> None:
        with TemporaryDirectory() as tempdir:
            service = _make_service(Path(tempdir) / "chipcoin.sqlite3")
            runtime = NodeRuntime(service=service)
            session = _FakeSession()
            handle = SessionHandle(  # type: ignore[arg-type]
                protocol=session,  # type: ignore[arg-type]
                outbound=False,
                endpoint=OutboundPeer("node-a", 18444),
                opened_at=1.0,
            )
            runtime._sessions[session] = handle
            penalties: list[int] = []

            def record_penalty(_session, *, error, penalty):
                penalties.append(penalty)
                return "warn"

            runtime._apply_session_penalty = record_penalty  # type: ignore[method-assign]
            message = MessageEnvelope(
                command="getdata",
                payload=GetDataMessage(items=(InventoryVector(object_type="block", object_hash="aa" * 32),)),
            )

            with caplog.at_level(logging.INFO, logger="chipcoin.node.runtime"):
                for _ in range(runtime._GETDATA_MISS_PENALTY_THRESHOLD):
                    await runtime._on_peer_message(session, message)  # type: ignore[arg-type]

            assert sent_messages == []
            assert handle.getdata_miss_count == runtime._GETDATA_MISS_PENALTY_THRESHOLD
            assert penalties == [5]
            assert "getdata miss peer=node-a:18444/peer-a requested=1 served=0" in caplog.text
            assert "penalty=5 action=warn" in caplog.text

    sent_messages: list[MessageEnvelope] = []
    asyncio.run(scenario())


def test_runtime_ignores_duplicate_mempool_transaction_relay(monkeypatch) -> None:
    class _FakeSessionState:
        closed = False
        handshake_complete = True
        remote_version = type("_Remote", (), {"node_id": "peer-a", "start_height": 1})()
        errors: list[str] = []
        error_causes: list[Exception] = []

    class _FakeSession:
        inbound = False
        state = _FakeSessionState()
        transport = None

    async def scenario() -> None:
        with TemporaryDirectory() as tempdir:
            service = _make_service(Path(tempdir) / "chipcoin.sqlite3")
            funding_outpoint = OutPoint(txid="12" * 32, index=0)
            put_wallet_utxo(service, funding_outpoint, value=100, owner=wallet_key(0))
            transaction = _spend_transaction(funding_outpoint, input_value=100, output_value=90)
            service.receive_transaction(transaction)
            runtime = NodeRuntime(service=service)
            session = _FakeSession()
            runtime._sessions[session] = SessionHandle(
                protocol=session,  # type: ignore[arg-type]
                outbound=True,
                endpoint=OutboundPeer("node-a", 18444),
            )
            penalties: list[int] = []
            runtime._apply_session_penalty = lambda _session, *, error, penalty: penalties.append(penalty)  # type: ignore[method-assign]

            await runtime._on_peer_message(
                session,  # type: ignore[arg-type]
                MessageEnvelope(command="tx", payload=TransactionMessage(transaction=transaction)),
            )

            assert penalties == []
            assert service.list_mempool_transactions() == [transaction]

    asyncio.run(scenario())


def test_runtime_keeps_local_transaction_eligible_for_periodic_relay() -> None:
    async def scenario() -> None:
        with TemporaryDirectory() as tempdir:
            service = _make_service(Path(tempdir) / "chipcoin.sqlite3")
            funding_outpoint = OutPoint(txid="56" * 32, index=0)
            put_wallet_utxo(service, funding_outpoint, value=100, owner=wallet_key(0))
            transaction = _spend_transaction(funding_outpoint, input_value=100, output_value=90)
            runtime = NodeRuntime(service=service)
            broadcasts: list[InventoryVector] = []

            async def broadcast_inventory(item, *, exclude=None) -> None:
                broadcasts.append(item)

            runtime._broadcast_inventory = broadcast_inventory  # type: ignore[method-assign]

            await runtime.submit_transaction(transaction)

            assert broadcasts == [InventoryVector(object_type="tx", object_hash=transaction.txid())]
            assert transaction.txid() not in runtime._relayed_mempool_txids

    asyncio.run(scenario())


def test_runtime_skips_recent_peer_transaction_before_validation() -> None:
    class _FakeSessionState:
        closed = False
        handshake_complete = True
        remote_version = type("_Remote", (), {"node_id": "peer-a", "start_height": 1})()
        errors: list[str] = []
        error_causes: list[Exception] = []

    class _FakeSession:
        inbound = False
        state = _FakeSessionState()
        transport = None

    async def scenario() -> None:
        with TemporaryDirectory() as tempdir:
            service = _make_service(Path(tempdir) / "chipcoin.sqlite3")
            funding_outpoint = OutPoint(txid="34" * 32, index=0)
            put_wallet_utxo(service, funding_outpoint, value=100, owner=wallet_key(0))
            transaction = _spend_transaction(funding_outpoint, input_value=100, output_value=90)
            original_receive_transaction = service.receive_transaction
            receive_calls = 0

            def counted_receive_transaction(transaction: Transaction):
                nonlocal receive_calls
                receive_calls += 1
                return original_receive_transaction(transaction)

            service.receive_transaction = counted_receive_transaction  # type: ignore[method-assign]
            runtime = NodeRuntime(service=service)
            session = _FakeSession()
            runtime._sessions[session] = SessionHandle(
                protocol=session,  # type: ignore[arg-type]
                outbound=True,
                endpoint=OutboundPeer("node-a", 18444),
            )
            message = MessageEnvelope(command="tx", payload=TransactionMessage(transaction=transaction))

            await runtime._on_peer_message(session, message)  # type: ignore[arg-type]
            await runtime._on_peer_message(session, message)  # type: ignore[arg-type]

            assert receive_calls == 1
            assert service.list_mempool_transactions() == [transaction]

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
                await _wait_until(lambda: bool(dropped))
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


def test_node_service_caches_active_chain_transaction_lookup(monkeypatch) -> None:
    with TemporaryDirectory() as tempdir:
        service = _make_service(Path(tempdir) / "chipcoin.sqlite3")
        for _ in range(3):
            template = service.build_candidate_block("CHCminer")
            service.apply_block(_mine_block(template.block))

        original_get = service.blocks.get
        calls = 0

        def counted_get(block_hash: str):
            nonlocal calls
            calls += 1
            return original_get(block_hash)

        monkeypatch.setattr(service.blocks, "get", counted_get)

        missing_txid = "ff" * 32
        assert service._find_transaction_in_active_chain(missing_txid) is None
        assert calls > 0

        calls = 0
        assert service._find_transaction_in_active_chain(missing_txid) is None
        assert calls == 0


def test_node_service_caches_reward_validation_state_for_mempool_admission() -> None:
    with TemporaryDirectory() as tempdir:
        service = _make_service(Path(tempdir) / "chipcoin.sqlite3")
        first_outpoint = OutPoint(txid="21" * 32, index=0)
        second_outpoint = OutPoint(txid="22" * 32, index=0)
        put_wallet_utxo(service, first_outpoint, value=100, owner=wallet_key(0))
        put_wallet_utxo(service, second_outpoint, value=100, owner=wallet_key(0))
        first = _spend_transaction(first_outpoint, input_value=100, output_value=90)
        second = _spend_transaction(second_outpoint, input_value=100, output_value=90)
        original_list_bundles = service.reward_attestations.list_bundles
        calls = 0

        def counted_list_bundles(*args, **kwargs):
            nonlocal calls
            calls += 1
            return original_list_bundles(*args, **kwargs)

        service.reward_attestations.list_bundles = counted_list_bundles  # type: ignore[method-assign]

        service.receive_transaction(first)
        service.receive_transaction(second)

        assert calls == 1


def test_mempool_rejects_duplicate_pending_reward_attestation_bundle_identity() -> None:
    with TemporaryDirectory() as tempdir:
        service = _make_service(Path(tempdir) / "chipcoin.sqlite3")
        first = _reward_attestation_bundle_transaction(
            epoch_index=80,
            bundle_window_index=7,
            bundle_submitter_node_id="mrbi.rqx",
            candidate_node_id="candidate-a",
            verifier_node_id="verifier-a",
        )
        duplicate = _reward_attestation_bundle_transaction(
            epoch_index=80,
            bundle_window_index=7,
            bundle_submitter_node_id="mrbi.rqx",
            candidate_node_id="candidate-b",
            verifier_node_id="verifier-b",
        )
        service.mempool.repository.add(first, fee=0, added_at=1)

        try:
            service.mempool._enforce_reward_attestation_bundle_mempool_policy(duplicate)
        except ValidationError as exc:
            assert "epoch, window, and submitter" in str(exc)
        else:
            raise AssertionError(
                "Expected duplicate pending reward attestation bundle identity to be rejected."
            )


def test_mempool_rejects_duplicate_pending_reward_attestation() -> None:
    with TemporaryDirectory() as tempdir:
        service = _make_service(Path(tempdir) / "chipcoin.sqlite3")
        first = _reward_attestation_bundle_transaction(
            epoch_index=80,
            bundle_window_index=7,
            bundle_submitter_node_id="submitter-a",
            candidate_node_id="candidate-a",
            verifier_node_id="verifier-a",
        )
        duplicate_attestation = _reward_attestation_bundle_transaction(
            epoch_index=80,
            bundle_window_index=8,
            check_window_index=7,
            bundle_submitter_node_id="submitter-b",
            candidate_node_id="candidate-a",
            verifier_node_id="verifier-a",
        )
        service.mempool.repository.add(first, fee=0, added_at=1)

        try:
            service.mempool._enforce_reward_attestation_bundle_mempool_policy(duplicate_attestation)
        except ValidationError as exc:
            assert "already contains this reward attestation" in str(exc)
        else:
            raise AssertionError("Expected duplicate pending reward attestation to be rejected.")


def test_mempool_limits_pending_reward_attestation_bundle_burst() -> None:
    with TemporaryDirectory() as tempdir:
        service = _make_service(Path(tempdir) / "chipcoin.sqlite3")
        service.mempool.policy = MempoolPolicy(max_reward_attestation_bundle_transactions=2)
        for index in range(service.mempool.policy.max_reward_attestation_bundle_transactions):
            transaction = _reward_attestation_bundle_transaction(
                epoch_index=80,
                bundle_window_index=index,
                bundle_submitter_node_id=f"submitter-{index}",
                candidate_node_id=f"candidate-{index}",
                verifier_node_id=f"verifier-{index}",
            )
            service.mempool.repository.add(transaction, fee=0, added_at=index)

        rejected = _reward_attestation_bundle_transaction(
            epoch_index=80,
            bundle_window_index=9,
            bundle_submitter_node_id="submitter-over-limit",
            candidate_node_id="candidate-over-limit",
            verifier_node_id="verifier-over-limit",
        )
        try:
            service.mempool._enforce_reward_attestation_bundle_mempool_policy(rejected)
        except ValidationError as exc:
            assert "limit exceeded" in str(exc)
        else:
            raise AssertionError("Expected reward attestation bundle burst to be capped.")


def test_node_service_rejects_duplicate_pending_reward_node_registration() -> None:
    with TemporaryDirectory() as tempdir:
        service = _make_service(Path(tempdir) / "chipcoin.sqlite3")
        registration_fee = int(service.reward_node_fee_schedule()["register_fee_chipbits"])
        first = TransactionSigner(wallet_key(0)).build_register_reward_node_transaction(
            node_id="node-farm-1",
            payout_address=wallet_key(0).address,
            node_public_key_hex=wallet_key(0).public_key.hex(),
            declared_host="42.115.140.51",
            declared_port=28444,
            registration_fee_chipbits=registration_fee,
            network=service.network,
        )
        duplicate = TransactionSigner(wallet_key(1)).build_register_reward_node_transaction(
            node_id="node-farm-1",
            payout_address=wallet_key(1).address,
            node_public_key_hex=wallet_key(1).public_key.hex(),
            declared_host="42.115.140.51",
            declared_port=28444,
            registration_fee_chipbits=registration_fee,
            network=service.network,
        )

        service.receive_transaction(first)
        try:
            service.receive_transaction(duplicate)
        except ValidationError as exc:
            assert "node_id" in str(exc)
        else:
            raise AssertionError("Expected duplicate reward-node registration to be rejected.")

        assert service.list_mempool_transactions() == [first]


def test_node_service_limits_pending_reward_node_registration_burst() -> None:
    with TemporaryDirectory() as tempdir:
        service = _make_service(Path(tempdir) / "chipcoin.sqlite3")
        service.mempool.policy = MempoolPolicy(max_register_reward_node_transactions=3)
        registration_fee = int(service.reward_node_fee_schedule()["register_fee_chipbits"])
        limit = service.mempool.policy.max_register_reward_node_transactions
        for index in range(limit):
            wallet = wallet_key_from_private_key(parse_private_key_hex(f"{index + 10:064x}"))
            transaction = TransactionSigner(wallet).build_register_reward_node_transaction(
                node_id=f"node-farm-{index}",
                payout_address=wallet.address,
                node_public_key_hex=wallet.public_key.hex(),
                declared_host="42.115.140.51",
                declared_port=28080 + index,
                registration_fee_chipbits=registration_fee,
                network=service.network,
            )
            service.receive_transaction(transaction)

        rejected_wallet = wallet_key_from_private_key(parse_private_key_hex(f"{limit + 10:064x}"))
        rejected = TransactionSigner(rejected_wallet).build_register_reward_node_transaction(
            node_id=f"node-farm-{limit}",
            payout_address=rejected_wallet.address,
            node_public_key_hex=rejected_wallet.public_key.hex(),
            declared_host="42.115.140.51",
            declared_port=28080 + limit,
            registration_fee_chipbits=registration_fee,
            network=service.network,
        )
        try:
            service.receive_transaction(rejected)
        except ValidationError as exc:
            assert "limit exceeded" in str(exc)
        else:
            raise AssertionError("Expected reward-node registration burst to be capped.")

        assert len(service.list_mempool_transactions()) == limit


def test_node_service_prunes_invalid_special_node_transaction_before_template() -> None:
    with TemporaryDirectory() as tempdir:
        service = _make_service(Path(tempdir) / "chipcoin.sqlite3")
        registration_fee = int(service.reward_node_fee_schedule()["register_fee_chipbits"])
        first = TransactionSigner(wallet_key(0)).build_register_reward_node_transaction(
            node_id="node-farm-1",
            payout_address=wallet_key(0).address,
            node_public_key_hex=wallet_key(0).public_key.hex(),
            declared_host="42.115.140.51",
            declared_port=28444,
            registration_fee_chipbits=registration_fee,
            network=service.network,
        )
        duplicate = TransactionSigner(wallet_key(1)).build_register_reward_node_transaction(
            node_id="node-farm-1",
            payout_address=wallet_key(1).address,
            node_public_key_hex=wallet_key(1).public_key.hex(),
            declared_host="42.115.140.51",
            declared_port=28444,
            registration_fee_chipbits=registration_fee,
            network=service.network,
        )
        service.mempool.repository.add(first, fee=0, added_at=1)
        service.mempool.repository.add(duplicate, fee=0, added_at=2)

        template = service.build_candidate_block("CHCminer")
        template_txids = {transaction.txid() for transaction in template.block.transactions}

        assert first.txid() in template_txids
        assert duplicate.txid() not in template_txids
        assert service.mempool.repository.get(first.txid()) is not None
        assert service.mempool.repository.get(duplicate.txid()) is None


def test_node_service_prunes_excess_special_node_transactions_before_template() -> None:
    with TemporaryDirectory() as tempdir:
        service = _make_service(Path(tempdir) / "chipcoin.sqlite3")
        service.mempool.policy = MempoolPolicy(max_register_reward_node_transactions=3)
        registration_fee = int(service.reward_node_fee_schedule()["register_fee_chipbits"])
        transactions = []
        for index in range(5):
            wallet = wallet_key_from_private_key(parse_private_key_hex(f"{index + 20:064x}"))
            transaction = TransactionSigner(wallet).build_register_reward_node_transaction(
                node_id=f"node-farm-{index}",
                payout_address=wallet.address,
                node_public_key_hex=wallet.public_key.hex(),
                declared_host="42.115.140.51",
                declared_port=28100 + index,
                registration_fee_chipbits=registration_fee,
                network=service.network,
            )
            transactions.append(transaction)
            service.mempool.repository.add(transaction, fee=0, added_at=index)

        template = service.build_candidate_block("CHCminer")
        template_txids = {transaction.txid() for transaction in template.block.transactions}

        assert {transaction.txid() for transaction in transactions[:3]}.issubset(template_txids)
        assert all(transaction.txid() not in template_txids for transaction in transactions[3:])
        assert [transaction.txid() for transaction in service.list_mempool_transactions()] == [
            transaction.txid()
            for transaction in transactions[:3]
        ]


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


def test_mempool_rejects_pre_validation_policy_failures_before_context_build() -> None:
    with TemporaryDirectory() as tempdir:
        service = _make_service(Path(tempdir) / "chipcoin.sqlite3")
        service.mempool.policy = MempoolPolicy(max_transaction_outputs=1)
        transaction = Transaction(
            version=1,
            inputs=(TxInput(previous_output=OutPoint(txid="89" * 32, index=0)),),
            outputs=(
                TxOutput(value=1, recipient=wallet_key(0).address),
                TxOutput(value=1, recipient=wallet_key(1).address),
            ),
            metadata={"kind": "payment"},
        )

        def fail_context_build(_view):
            raise AssertionError("pre-validation policy failure must not build validation context")

        service.mempool.validation_context_factory = fail_context_build

        try:
            service.receive_transaction(transaction)
        except ValidationError as exc:
            assert "output-count policy" in str(exc)
            return
        raise AssertionError("Expected pre-validation mempool policy rejection.")


def test_node_service_rejects_oversized_raw_transaction_before_deserialize() -> None:
    with TemporaryDirectory() as tempdir:
        service = _make_service(Path(tempdir) / "chipcoin.sqlite3")
        service.mempool.policy = MempoolPolicy(max_transaction_size_bytes=4)

        try:
            service.decode_raw_transaction("00" * 5)
        except ValueError as exc:
            assert "mempool size policy" in str(exc)
            return
        raise AssertionError("Expected oversized raw transaction to be rejected before deserialize.")


def test_mempool_reconcile_revalidates_entries_without_repeated_repository_scans() -> None:
    with TemporaryDirectory() as tempdir:
        service = _make_service(Path(tempdir) / "chipcoin.sqlite3")
        transactions = []
        for index in range(3):
            outpoint = OutPoint(txid=f"{index + 1:064x}", index=0)
            put_wallet_utxo(service, outpoint, value=100, owner=wallet_key(0))
            transaction = signed_payment(outpoint, value=100, sender=wallet_key(0), fee=10)
            service.receive_transaction(transaction)
            transactions.append(transaction)

        repository = service.mempool.repository
        original_list_all = repository.list_all
        call_count = 0

        def counted_list_all():
            nonlocal call_count
            call_count += 1
            return original_list_all()

        repository.list_all = counted_list_all  # type: ignore[method-assign]

        service.mempool.reconcile()

        assert call_count <= 3
        assert [transaction.txid() for transaction in service.list_mempool_transactions()] == [
            transaction.txid() for transaction in transactions
        ]


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


def test_mempool_rejects_chcq_output_before_activation() -> None:
    with TemporaryDirectory() as tempdir:
        service = NodeService.open_sqlite(Path(tempdir) / "chipcoin.sqlite3", network="testnet")
        owner = wallet_key(0)
        funding_outpoint = OutPoint(txid="9a" * 32, index=0)
        put_wallet_utxo(service, funding_outpoint, value=100, owner=owner)
        pq_recipient = public_key_to_pq_address(b"\x77" * 1312, scheme_id=SIG_SCHEME_ML_DSA_44)
        unsigned = Transaction(
            version=1,
            inputs=(TxInput(previous_output=funding_outpoint),),
            outputs=(TxOutput(value=90, recipient=pq_recipient),),
            metadata={"kind": "payment"},
        )
        digest = transaction_signature_digest(
            unsigned,
            0,
            previous_output=TxOutput(value=100, recipient=owner.address),
            network=service.network,
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
            assert "CHCQ outputs are not active" in str(exc)
            return
        raise AssertionError("Expected pre-activation CHCQ output to be rejected by mempool validation.")


def test_mempool_uses_next_height_and_candidate_at_pq_activation_boundary() -> None:
    with TemporaryDirectory() as tempdir:
        service = NodeService.open_sqlite(Path(tempdir) / "chipcoin.sqlite3", network="testnet")
        synthetic_tip = BlockHeader(
            version=1,
            previous_block_hash="00" * 32,
            merkle_root="00" * 32,
            timestamp=1_700_000_000,
            bits=service.params.genesis_bits,
            nonce=0,
        )
        service.headers.put(
            synthetic_tip,
            height=PQ_SUPPORT_TESTNET_ACTIVATION_HEIGHT - 1,
            cumulative_work=1,
            is_main_chain=True,
        )
        service.headers.set_tip(synthetic_tip.block_hash(), PQ_SUPPORT_TESTNET_ACTIVATION_HEIGHT - 1)

        owner = wallet_key(0)
        funding_outpoint = OutPoint(txid="9c" * 32, index=0)
        put_wallet_utxo(service, funding_outpoint, value=100, owner=owner)
        pq_recipient = public_key_to_pq_address(b"\x78" * 1312, scheme_id=SIG_SCHEME_ML_DSA_44)
        tx = signed_payment(
            funding_outpoint,
            value=100,
            sender=owner,
            recipient=pq_recipient,
            amount=90,
            fee=10,
        )

        accepted = service.receive_transaction(tx)
        template = service.build_candidate_block(wallet_key(2).address)

        assert accepted.transaction.txid() == tx.txid()
        assert template.height == PQ_SUPPORT_TESTNET_ACTIVATION_HEIGHT
        assert any(candidate.txid() == tx.txid() for candidate in template.block.transactions)


def test_mempool_accepts_and_mines_chcq_spend_after_activation(monkeypatch) -> None:
    monkeypatch.setattr("chipcoin.consensus.validation.pq_support_is_active", lambda *, network, height: True)
    with TemporaryDirectory() as tempdir:
        service = NodeService.open_sqlite(Path(tempdir) / "chipcoin.sqlite3", network="testnet")
        pq_owner = wallet_key_from_mldsa44_seed(bytes(range(32)))
        recipient = wallet_key(1).address
        funding_outpoint = OutPoint(txid="9b" * 32, index=0)
        put_wallet_utxo(service, funding_outpoint, value=1_234_567_890, owner=pq_owner)
        built = TransactionSigner(pq_owner).build_signed_transaction(
            spend_candidates=spend_candidates_for_wallet(funding_outpoint, value=1_234_567_890, owner=pq_owner),
            recipient=recipient,
            amount_chipbits=1_000_000_000,
            fee_chipbits=1_000,
            metadata={"kind": "payment", "purpose": "pq-node-e2e"},
            network=service.network,
        )

        accepted = service.receive_transaction(built.transaction)
        candidate = service.build_candidate_block(wallet_key(2).address)
        mined_block = _mine_block(candidate.block)
        total_fees = service.apply_block(mined_block)

        assert accepted.transaction.txid() == built.transaction.txid()
        assert accepted.fee == 1_000
        assert total_fees == 1_000
        assert service.list_mempool_transactions() == []
        assert service.find_transaction(built.transaction.txid())["location"] == "chain"
        assert service.chainstate.get(funding_outpoint) is None


def test_mempool_accepts_chcq_output_after_activation(monkeypatch) -> None:
    monkeypatch.setattr("chipcoin.consensus.validation.pq_support_is_active", lambda *, network, height: True)
    with TemporaryDirectory() as tempdir:
        service = NodeService.open_sqlite(Path(tempdir) / "chipcoin.sqlite3", network="testnet")
        owner = wallet_key(0)
        funding_outpoint = OutPoint(txid="9c" * 32, index=0)
        put_wallet_utxo(service, funding_outpoint, value=100, owner=owner)
        pq_recipient = wallet_key_from_mldsa44_seed(bytes(range(32))).address
        transaction = signed_payment(
            funding_outpoint,
            value=100,
            sender=owner,
            recipient=pq_recipient,
            amount=90,
            fee=10,
        )

        accepted = service.receive_transaction(transaction)

        assert accepted.transaction.txid() == transaction.txid()
        assert accepted.fee == 10
        assert service.list_mempool_transactions() == [transaction]


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


def test_reward_attestation_backlog_report_groups_pending_bundles() -> None:
    with TemporaryDirectory() as tempdir:
        params = replace(MAINNET_PARAMS, coinbase_maturity=0, max_attestation_bundles_per_block=4)
        service = _make_service_with_params(Path(tempdir) / "chipcoin.sqlite3", params)
        bundles = [
            _reward_attestation_bundle_transaction(
                epoch_index=5,
                bundle_window_index=1,
                bundle_submitter_node_id="submitter-a",
                candidate_node_id="candidate-a",
                verifier_node_id="verifier-a",
                extra_attestations=(("candidate-b", "verifier-a"),),
            ),
            _reward_attestation_bundle_transaction(
                epoch_index=5,
                bundle_window_index=1,
                bundle_submitter_node_id="submitter-b",
                candidate_node_id="candidate-a",
                verifier_node_id="verifier-b",
            ),
            _reward_attestation_bundle_transaction(
                epoch_index=5,
                bundle_window_index=2,
                bundle_submitter_node_id="submitter-a",
                candidate_node_id="candidate-c",
                verifier_node_id="verifier-a",
            ),
            _reward_attestation_bundle_transaction(
                epoch_index=6,
                bundle_window_index=1,
                bundle_submitter_node_id="submitter-c",
                candidate_node_id="candidate-d",
                verifier_node_id="verifier-c",
            ),
            _reward_attestation_bundle_transaction(
                epoch_index=6,
                bundle_window_index=2,
                bundle_submitter_node_id="submitter-d",
                candidate_node_id="candidate-e",
                verifier_node_id="verifier-d",
            ),
        ]
        for index, bundle in enumerate(bundles):
            service.mempool.repository.add(bundle, fee=0, added_at=1_700_000_000 + index)

        report = service.reward_attestation_backlog_report()

        assert report["mempool_reward_attestation_bundle_count"] == 5
        assert report["total_attestation_count"] == 6
        assert report["average_attestations_per_bundle"] == 1.2
        assert report["estimated_blocks_to_drain_at_current_cap"] == 2
        assert report["estimated_seconds_to_drain_at_target_spacing"] == 1200
        assert report["aggregation_projection"] == {
            "estimated_bundles_after_epoch_window_aggregation": 4,
            "estimated_bundle_reduction_after_epoch_window_aggregation": 1,
            "estimated_blocks_to_drain_after_epoch_window_aggregation": 1,
            "estimated_seconds_to_drain_after_epoch_window_aggregation": 600,
        }
        assert report["duplicate_bundle_key_count"] == 0
        assert report["duplicate_attestation_identity_count"] == 0
        assert report["by_epoch"] == [
            {"epoch_index": 5, "count": 3},
            {"epoch_index": 6, "count": 2},
        ]
        assert {"epoch_index": 5, "bundle_window_index": 1, "count": 2} in report["by_epoch_window"]
        assert report["by_submitter"][0] == {"bundle_submitter_node_id": "submitter-a", "count": 2}
        assert {"candidate_node_id": "candidate-a", "attestation_count": 2} in report["by_candidate"]
        assert {"verifier_node_id": "verifier-a", "attestation_count": 3} in report["by_verifier"]


def test_mempool_diagnostics_includes_transaction_metadata() -> None:
    with TemporaryDirectory() as tempdir:
        service = _make_service(Path(tempdir) / "chipcoin.sqlite3")
        bundle = _reward_attestation_bundle_transaction()
        service.mempool.repository.add(bundle, fee=0, added_at=1_700_000_000)

        diagnostics = service.mempool_diagnostics()

        assert diagnostics[0]["metadata"]["kind"] == REWARD_ATTESTATION_BUNDLE_KIND
        assert diagnostics[0]["metadata"]["bundle_submitter_node_id"] == "submitter-a"


def test_reward_attestation_aggregate_can_replace_smaller_pending_bundles() -> None:
    with TemporaryDirectory() as tempdir:
        service = _make_service(Path(tempdir) / "chipcoin.sqlite3")
        first = _reward_attestation_bundle_transaction(
            epoch_index=80,
            bundle_window_index=1,
            bundle_submitter_node_id="submitter-a",
            candidate_node_id="candidate-a",
            verifier_node_id="verifier-a",
        )
        second = _reward_attestation_bundle_transaction(
            epoch_index=80,
            bundle_window_index=1,
            bundle_submitter_node_id="submitter-b",
            candidate_node_id="candidate-b",
            verifier_node_id="verifier-b",
        )
        aggregate = _reward_attestation_bundle_transaction(
            epoch_index=80,
            bundle_window_index=1,
            bundle_submitter_node_id="submitter-c",
            candidate_node_id="candidate-a",
            verifier_node_id="verifier-a",
            extra_attestations=(("candidate-b", "verifier-b"),),
        )
        service.mempool.repository.add(first, fee=0, added_at=1_700_000_000)
        service.mempool.repository.add(second, fee=0, added_at=1_700_000_001)

        replacement_txids = service.mempool._reward_attestation_bundle_replacement_txids(aggregate)

        assert replacement_txids == [first.txid(), second.txid()]
        remaining_entries = [
            entry
            for entry in service.mempool.repository.list_all()
            if entry.transaction.txid() not in set(replacement_txids)
        ]
        service.mempool._enforce_reward_attestation_bundle_mempool_policy(
            aggregate,
            entries=remaining_entries,
        )


def test_runtime_aggregates_pending_reward_attestations_for_same_window() -> None:
    with TemporaryDirectory() as tempdir:
        service = _make_service(Path(tempdir) / "chipcoin.sqlite3")
        runtime = NodeRuntime(service=service)
        pending = _reward_attestation_bundle_transaction(
            epoch_index=80,
            bundle_window_index=1,
            bundle_submitter_node_id="submitter-a",
            candidate_node_id="candidate-a",
            verifier_node_id="verifier-a",
        )
        local = _reward_attestation_bundle_transaction(
            epoch_index=80,
            bundle_window_index=1,
            bundle_submitter_node_id="submitter-b",
            candidate_node_id="candidate-b",
            verifier_node_id="verifier-b",
        )
        service.mempool.repository.add(pending, fee=0, added_at=1_700_000_000)

        local_bundle = json.loads(local.metadata["attestations_json"])
        aggregate = runtime._aggregate_pending_reward_attestations(
            epoch_index=80,
            bundle_window_index=1,
            local_attestations=list(parse_reward_attestation_bundle_metadata(local.metadata).attestations),
        )

        assert len(local_bundle) == 1
        assert [(item.candidate_node_id, item.verifier_node_id) for item in aggregate] == [
            ("candidate-a", "verifier-a"),
            ("candidate-b", "verifier-b"),
        ]


def test_block_template_respects_max_attestation_bundles_per_block() -> None:
    with TemporaryDirectory() as tempdir:
        params = replace(MAINNET_PARAMS, coinbase_maturity=0, max_attestation_bundles_per_block=2)
        service = _make_service_with_params(Path(tempdir) / "chipcoin.sqlite3", params)
        for index in range(5):
            bundle = _reward_attestation_bundle_transaction(
                epoch_index=5,
                bundle_window_index=index,
                bundle_submitter_node_id=f"submitter-{index}",
                candidate_node_id=f"candidate-{index}",
                verifier_node_id=f"verifier-{index}",
            )
            service.mempool.repository.add(bundle, fee=0, added_at=1_700_000_000 + index)

        template = service.build_candidate_block(wallet_key(0).address)
        included_bundles = [
            transaction
            for transaction in template.block.transactions
            if transaction.metadata.get("kind") == REWARD_ATTESTATION_BUNDLE_KIND
        ]

        assert len(included_bundles) == 2


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
            network=service.network,
            height=0,
            miner_address=wallet_key(2).address,
            bits=service.params.genesis_bits,
            mempool_entries=[MempoolEntry(transaction=child, fee=10, added_at=1)],
            node_registry_view=service.node_registry.snapshot(),
            confirmed_transaction_ids=set(),
        )

        assert len(template.block.transactions) == 1


def test_block_template_builder_skips_registry_conflicting_special_node_transactions() -> None:
    with TemporaryDirectory() as tempdir:
        service = _make_service(Path(tempdir) / "chipcoin.sqlite3")
        registration_fee = int(service.reward_node_fee_schedule()["register_fee_chipbits"])
        first = TransactionSigner(wallet_key(0)).build_register_reward_node_transaction(
            node_id="node-farm-1",
            payout_address=wallet_key(0).address,
            node_public_key_hex=wallet_key(0).public_key.hex(),
            declared_host="42.115.140.51",
            declared_port=28444,
            registration_fee_chipbits=registration_fee,
            network=service.network,
        )
        duplicate = TransactionSigner(wallet_key(1)).build_register_reward_node_transaction(
            node_id="node-farm-1",
            payout_address=wallet_key(1).address,
            node_public_key_hex=wallet_key(1).public_key.hex(),
            declared_host="42.115.140.51",
            declared_port=28444,
            registration_fee_chipbits=registration_fee,
            network=service.network,
        )

        template = service.mining.build_block_template(
            previous_block_hash="00" * 32,
            network=service.network,
            height=0,
            miner_address=wallet_key(2).address,
            bits=service.params.genesis_bits,
            mempool_entries=[
                MempoolEntry(transaction=first, fee=0, added_at=1),
                MempoolEntry(transaction=duplicate, fee=0, added_at=2),
            ],
            node_registry_view=service.node_registry.snapshot(),
            confirmed_transaction_ids=set(),
        )

        template_txids = {transaction.txid() for transaction in template.block.transactions}
        assert first.txid() in template_txids
        assert duplicate.txid() not in template_txids


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
