from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import asyncio

from chipcoin.consensus.params import DEVNET_PARAMS, MAINNET_PARAMS
from chipcoin.consensus.models import Block, OutPoint, Transaction, TxInput, TxOutput
from chipcoin.consensus.pow import verify_proof_of_work
from chipcoin.consensus.serialization import serialize_transaction
from chipcoin.consensus.validation import ValidationContext, validate_block, ValidationError, transaction_signature_digest
from chipcoin.consensus.utxo import InMemoryUtxoView
from chipcoin.crypto.signatures import sign_digest
from chipcoin.node.mempool import MempoolPolicy
from chipcoin.node.mining import transaction_weight_units
from chipcoin.node.peers import PeerInfo, PeerManager
from chipcoin.node.messages import AddrMessage, MessageEnvelope, PeerAddress
from chipcoin.node.runtime import NodeRuntime, OutboundPeer, SessionHandle
from chipcoin.node.service import NodeService
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
        service.add_peer("127.0.0.1", 18444)
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


def test_runtime_ignores_announced_alias_of_known_peer(monkeypatch) -> None:
    async def scenario() -> None:
        with TemporaryDirectory() as tempdir:
            service = _make_service(Path(tempdir) / "chipcoin.sqlite3")
            service.add_peer("173.212.193.13", 18444)
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


def test_runtime_dedupes_unidentified_outbound_aliases(monkeypatch) -> None:
    with TemporaryDirectory() as tempdir:
        service = _make_service(Path(tempdir) / "chipcoin.sqlite3")
        service.add_peer("173.212.193.13", 18444)
        service.add_peer("tiltmediaconsulting.com", 18444)
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
        service.add_peer("173.212.193.13", 18444)
        service.add_peer("tiltmediaconsulting.com", 18444)
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
        service.add_peer("node-a", 18444)

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
        assert int(template.block.transactions[0].outputs[0].value) == 55 * 100_000_000 + 10


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
