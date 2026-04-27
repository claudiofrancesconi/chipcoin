import asyncio
import json
import socket
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib import request

import pytest

from chipcoin.config import MAINNET_CONFIG
from chipcoin.consensus.models import OutPoint
from chipcoin.consensus.params import DEVNET_PARAMS, MAINNET_PARAMS
from chipcoin.consensus.pow import verify_proof_of_work
from chipcoin.consensus.serialization import serialize_transaction
from chipcoin.miner.config import MinerWorkerConfig
from chipcoin.miner.worker import MinerWorker
from chipcoin.node.messages import EmptyPayload, MessageEnvelope
from chipcoin.node.p2p.codec import encode_message
from chipcoin.node.runtime import NodeRuntime, OutboundPeer
from chipcoin.node.service import NodeService
from tests.helpers import signed_payment, wallet_key


TEST_PARAMS = replace(MAINNET_PARAMS, coinbase_maturity=0)


def _local_socket_available() -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
    except OSError:
        return False
    return True


pytestmark = pytest.mark.skipif(
    not _local_socket_available(),
    reason="local TCP binds are unavailable in this environment",
)


def _http_json(method: str, url: str, body: dict[str, object] | None = None) -> dict[str, object]:
    encoded = None if body is None else json.dumps(body, sort_keys=True).encode("utf-8")
    headers = {"Accept": "application/json"}
    if encoded is not None:
        headers["Content-Type"] = "application/json"
    req = request.Request(url, method=method, data=encoded, headers=headers)
    with request.urlopen(req, timeout=10.0) as response:
        raw = response.read()
    return {} if not raw else json.loads(raw.decode("utf-8"))


def test_two_runtimes_complete_handshake_and_sync_blockchain() -> None:
    async def scenario() -> None:
        with TemporaryDirectory() as tempdir:
            source_service = _make_service(Path(tempdir) / "source.sqlite3", start_time=1_700_000_000)
            target_service = _make_service(Path(tempdir) / "target.sqlite3", start_time=1_700_001_000)
            source_runtime = NodeRuntime(service=source_service, listen_host="127.0.0.1", listen_port=0, ping_interval=0.2)
            await source_runtime.start()
            mined_block = _mine_block(source_service.build_candidate_block("CHCminer-a").block)
            source_service.apply_block(mined_block)

            target_runtime = NodeRuntime(
                service=target_service,
                listen_host="127.0.0.1",
                listen_port=0,
                outbound_peers=[OutboundPeer("127.0.0.1", source_runtime.bound_port)],
                connect_interval=0.1,
                ping_interval=0.2,
            )
            await target_runtime.start()
            try:
                await _wait_until(lambda: source_runtime.connected_peer_count() == 1 and target_runtime.connected_peer_count() == 1)
                await _wait_until(lambda: target_service.chain_tip() is not None and target_service.chain_tip().block_hash == mined_block.block_hash())
            finally:
                await target_runtime.stop()
                await source_runtime.stop()

    asyncio.run(scenario())


def test_runtime_relays_transaction_between_two_nodes() -> None:
    async def scenario() -> None:
        with TemporaryDirectory() as tempdir:
            source_service = _make_service(Path(tempdir) / "source.sqlite3", start_time=1_700_000_000)
            target_service = _make_service(Path(tempdir) / "target.sqlite3", start_time=1_700_001_000)
            source_runtime = NodeRuntime(service=source_service, listen_host="127.0.0.1", listen_port=0, ping_interval=0.2)
            await source_runtime.start()
            funding_block = _mine_block(source_service.build_candidate_block(wallet_key(0).address).block)
            source_service.apply_block(funding_block)

            target_runtime = NodeRuntime(
                service=target_service,
                listen_host="127.0.0.1",
                listen_port=0,
                outbound_peers=[OutboundPeer("127.0.0.1", source_runtime.bound_port)],
                connect_interval=0.1,
                ping_interval=0.2,
            )
            await target_runtime.start()
            try:
                await _wait_until(lambda: target_service.chain_tip() is not None and target_service.chain_tip().block_hash == funding_block.block_hash())

                coinbase_txid = funding_block.transactions[0].txid()
                transaction = signed_payment(
                    OutPoint(txid=coinbase_txid, index=0),
                    value=int(funding_block.transactions[0].outputs[0].value),
                    sender=wallet_key(0),
                    fee=10,
                )

                await source_runtime.submit_transaction(transaction)
                await _wait_until(lambda: target_service.find_transaction(transaction.txid()) is not None)
                result = target_service.find_transaction(transaction.txid())
                assert result is not None
                assert result["location"] == "mempool"
            finally:
                await target_runtime.stop()
                await source_runtime.stop()

    asyncio.run(scenario())


def test_runtime_http_submit_tx_relays_between_two_nodes() -> None:
    async def scenario() -> None:
        _require_local_socket_support()
        with TemporaryDirectory() as tempdir:
            source_service = _make_service(Path(tempdir) / "source.sqlite3", start_time=1_700_000_000)
            target_service = _make_service(Path(tempdir) / "target.sqlite3", start_time=1_700_001_000)
            http_port = _free_port()
            source_runtime = NodeRuntime(
                service=source_service,
                listen_host="127.0.0.1",
                listen_port=0,
                http_host="127.0.0.1",
                http_port=http_port,
                ping_interval=0.2,
            )
            await source_runtime.start()
            funding_block = _mine_block(source_service.build_candidate_block(wallet_key(0).address).block)
            source_service.apply_block(funding_block)

            target_runtime = NodeRuntime(
                service=target_service,
                listen_host="127.0.0.1",
                listen_port=0,
                outbound_peers=[OutboundPeer("127.0.0.1", source_runtime.bound_port)],
                connect_interval=0.1,
                ping_interval=0.2,
            )
            await target_runtime.start()
            try:
                await _wait_until(lambda: target_service.chain_tip() is not None and target_service.chain_tip().block_hash == funding_block.block_hash())

                coinbase_txid = funding_block.transactions[0].txid()
                transaction = signed_payment(
                    OutPoint(txid=coinbase_txid, index=0),
                    value=int(funding_block.transactions[0].outputs[0].value),
                    sender=wallet_key(0),
                    fee=10,
                )

                result = await asyncio.to_thread(
                    _http_json,
                    "POST",
                    f"http://127.0.0.1:{source_runtime.http_bound_port}/v1/tx/submit",
                    {"raw_hex": serialize_transaction(transaction).hex()},
                )
                assert result["accepted"] is True
                await _wait_until(lambda: target_service.find_transaction(transaction.txid()) is not None)
                propagated = target_service.find_transaction(transaction.txid())
                assert propagated is not None
                assert propagated["location"] == "mempool"
            finally:
                await target_runtime.stop()
                await source_runtime.stop()

    asyncio.run(scenario())


def test_runtime_relays_new_block_between_two_connected_nodes() -> None:
    async def scenario() -> None:
        with TemporaryDirectory() as tempdir:
            source_service = _make_service(Path(tempdir) / "source.sqlite3", start_time=1_700_000_000)
            target_service = _make_service(Path(tempdir) / "target.sqlite3", start_time=1_700_001_000)
            source_runtime = NodeRuntime(service=source_service, listen_host="127.0.0.1", listen_port=0, ping_interval=0.2)
            await source_runtime.start()
            genesis_successor = _mine_block(source_service.build_candidate_block("CHCminer-a").block)
            source_service.apply_block(genesis_successor)

            target_runtime = NodeRuntime(
                service=target_service,
                listen_host="127.0.0.1",
                listen_port=0,
                outbound_peers=[OutboundPeer("127.0.0.1", source_runtime.bound_port)],
                connect_interval=0.1,
                ping_interval=0.2,
            )
            await target_runtime.start()
            try:
                await _wait_until(lambda: target_service.chain_tip() is not None and target_service.chain_tip().block_hash == genesis_successor.block_hash())

                next_block = _mine_block(source_service.build_candidate_block("CHCminer-b").block)
                await source_runtime.announce_block(next_block)

                await _wait_until(lambda: target_service.chain_tip() is not None and target_service.chain_tip().block_hash == next_block.block_hash())
            finally:
                await target_runtime.stop()
                await source_runtime.stop()

    asyncio.run(scenario())


def test_runtime_accepts_http_mined_block_and_relays_it_to_connected_peers() -> None:
    async def scenario() -> None:
        _require_local_socket_support()
        with TemporaryDirectory() as tempdir:
            node_a_service = _make_service(Path(tempdir) / "node-a.sqlite3", start_time=1_700_000_000)
            node_b_service = _make_service(Path(tempdir) / "node-b.sqlite3", start_time=1_700_001_000)
            node_a_runtime = NodeRuntime(
                service=node_a_service,
                listen_host="127.0.0.1",
                listen_port=0,
                http_host="127.0.0.1",
                http_port=_free_port(),
                ping_interval=0.2,
            )
            await node_a_runtime.start()
            node_b_runtime = NodeRuntime(
                service=node_b_service,
                listen_host="127.0.0.1",
                listen_port=0,
                outbound_peers=[OutboundPeer("127.0.0.1", node_a_runtime.bound_port)],
                connect_interval=0.1,
                ping_interval=0.2,
            )
            await node_b_runtime.start()
            try:
                await _wait_until(lambda: node_a_runtime.connected_peer_count() == 1 and node_b_runtime.connected_peer_count() == 1)
                worker = MinerWorker(
                    MinerWorkerConfig(
                        network="mainnet",
                        payout_address=wallet_key(1).address,
                        node_urls=(f"http://127.0.0.1:{node_a_runtime.http_bound_port}",),
                        miner_id="worker-a",
                        nonce_batch_size=2_000_000,
                        mining_min_interval_seconds=5.0,
                        run_seconds=0.3,
                    )
                )

                result = await asyncio.to_thread(worker.run)

                assert result["accepted_blocks"] >= 1
                await _wait_until(
                    lambda: node_a_service.chain_tip() is not None
                    and node_b_service.chain_tip() is not None
                    and node_a_service.chain_tip().block_hash == node_b_service.chain_tip().block_hash,
                    timeout=10.0,
                )
            finally:
                await node_b_runtime.stop()
                await node_a_runtime.stop()

    asyncio.run(scenario())


def test_runtime_http_miner_end_to_end_over_real_http_and_p2p() -> None:
    async def scenario() -> None:
        _require_local_socket_support()
        with TemporaryDirectory() as tempdir:
            node_a_service = _make_service(Path(tempdir) / "node-a.sqlite3", start_time=1_700_000_000)
            node_b_service = _make_service(Path(tempdir) / "node-b.sqlite3", start_time=1_700_001_000)
            http_port = _free_port()
            node_a_runtime = NodeRuntime(
                service=node_a_service,
                listen_host="127.0.0.1",
                listen_port=0,
                http_host="127.0.0.1",
                http_port=http_port,
                ping_interval=0.2,
            )
            await node_a_runtime.start()
            node_b_runtime = NodeRuntime(
                service=node_b_service,
                listen_host="127.0.0.1",
                listen_port=0,
                outbound_peers=[OutboundPeer("127.0.0.1", node_a_runtime.bound_port)],
                connect_interval=0.1,
                ping_interval=0.2,
            )
            await node_b_runtime.start()
            try:
                await _wait_until(lambda: node_a_runtime.connected_peer_count() == 1 and node_b_runtime.connected_peer_count() == 1)
                worker = MinerWorker(
                    MinerWorkerConfig(
                        network="mainnet",
                        payout_address=wallet_key(1).address,
                        node_urls=(f"http://127.0.0.1:{node_a_runtime.http_bound_port}",),
                        miner_id="worker-http",
                        nonce_batch_size=2_000_000,
                        mining_min_interval_seconds=5.0,
                        run_seconds=0.3,
                    )
                )
                result = await asyncio.to_thread(worker.run)
                assert result["accepted_blocks"] >= 1
                await _wait_until(
                    lambda: node_a_service.chain_tip() is not None
                    and node_b_service.chain_tip() is not None
                    and node_a_service.chain_tip().block_hash == node_b_service.chain_tip().block_hash
                )
                status = await asyncio.to_thread(
                    _http_json,
                    "GET",
                    f"http://127.0.0.1:{node_a_runtime.http_bound_port}/mining/status",
                )
                assert status["best_tip_hash"] == node_a_service.chain_tip().block_hash
            finally:
                await node_b_runtime.stop()
                await node_a_runtime.stop()

    asyncio.run(scenario())


def test_runtime_deduplicates_duplicate_connections_between_same_two_nodes() -> None:
    async def scenario() -> None:
        with TemporaryDirectory() as tempdir:
            left_service = _make_service(Path(tempdir) / "left.sqlite3", start_time=1_700_000_000)
            right_service = _make_service(Path(tempdir) / "right.sqlite3", start_time=1_700_001_000)
            left_runtime = NodeRuntime(
                service=left_service,
                listen_host="127.0.0.1",
                listen_port=0,
                connect_interval=0.1,
                ping_interval=0.2,
            )
            right_runtime = NodeRuntime(
                service=right_service,
                listen_host="127.0.0.1",
                listen_port=0,
                connect_interval=0.1,
                ping_interval=0.2,
            )
            await left_runtime.start()
            await right_runtime.start()
            left_runtime._outbound_targets[("127.0.0.1", right_runtime.bound_port)] = OutboundPeer("127.0.0.1", right_runtime.bound_port)
            right_runtime._outbound_targets[("127.0.0.1", left_runtime.bound_port)] = OutboundPeer("127.0.0.1", left_runtime.bound_port)

            try:
                await _wait_until(lambda: left_runtime.connected_peer_count() == 1 and right_runtime.connected_peer_count() == 1)
                await _wait_until(
                    lambda: any(
                        peer.protocol_error_class == "duplicate_connection"
                        for peer in left_service.list_peers() + right_service.list_peers()
                    )
                )
            finally:
                await right_runtime.stop()
                await left_runtime.stop()

    asyncio.run(scenario())


def test_runtime_sync_continues_when_headers_arrive_in_multiple_batches() -> None:
    async def scenario() -> None:
        with TemporaryDirectory() as tempdir:
            source_service = _make_service(Path(tempdir) / "source.sqlite3", start_time=1_700_000_000)
            target_service = _make_service(Path(tempdir) / "target.sqlite3", start_time=1_700_001_000)
            source_runtime = NodeRuntime(service=source_service, listen_host="127.0.0.1", listen_port=0, ping_interval=0.2)
            original_handle_getheaders = source_service.handle_getheaders
            source_service.handle_getheaders = lambda request, limit=2000: original_handle_getheaders(request, limit=2)
            await source_runtime.start()
            latest_block = None
            for _ in range(5):
                latest_block = _mine_block(source_service.build_candidate_block("CHCminer-a").block)
                source_service.apply_block(latest_block)
            assert latest_block is not None

            target_runtime = NodeRuntime(
                service=target_service,
                listen_host="127.0.0.1",
                listen_port=0,
                outbound_peers=[OutboundPeer("127.0.0.1", source_runtime.bound_port)],
                connect_interval=0.1,
                ping_interval=0.2,
            )
            target_runtime.sync_manager.max_headers = 2
            await target_runtime.start()
            try:
                await _wait_until(lambda: target_service.chain_tip() is not None and target_service.chain_tip().block_hash == latest_block.block_hash())
            finally:
                await target_runtime.stop()
                await source_runtime.stop()

    asyncio.run(scenario())


def test_runtime_chunks_getdata_requests_to_inventory_limit() -> None:
    async def scenario() -> None:
        with TemporaryDirectory() as tempdir:
            source_service = _make_service(Path(tempdir) / "source.sqlite3", start_time=1_700_000_000)
            target_service = _make_service(Path(tempdir) / "target.sqlite3", start_time=1_700_001_000)
            source_runtime = NodeRuntime(service=source_service, listen_host="127.0.0.1", listen_port=0, ping_interval=0.2)
            await source_runtime.start()
            latest_block = None
            for _ in range(5):
                latest_block = _mine_block(source_service.build_candidate_block("CHCminer-a").block)
                source_service.apply_block(latest_block)
            assert latest_block is not None

            target_runtime = NodeRuntime(
                service=target_service,
                listen_host="127.0.0.1",
                listen_port=0,
                outbound_peers=[OutboundPeer("127.0.0.1", source_runtime.bound_port)],
                connect_interval=0.1,
                ping_interval=0.2,
                max_inventory_items=2,
            )
            await target_runtime.start()
            try:
                await _wait_until(lambda: target_service.chain_tip() is not None and target_service.chain_tip().block_hash == latest_block.block_hash())
            finally:
                await target_runtime.stop()
                await source_runtime.stop()

    asyncio.run(scenario())


def test_runtime_sync_downloads_blocks_from_multiple_peers() -> None:
    async def scenario() -> None:
        with TemporaryDirectory() as tempdir:
            source_service = _make_service(Path(tempdir) / "source.sqlite3", start_time=1_700_000_000)
            latest_block = None
            for _ in range(6):
                latest_block = _mine_block(source_service.build_candidate_block("CHCminer-a").block)
                source_service.apply_block(latest_block)
            assert latest_block is not None

            source_runtime_a = NodeRuntime(service=source_service, listen_host="127.0.0.1", listen_port=0, ping_interval=0.2)
            source_runtime_b = NodeRuntime(service=source_service, listen_host="127.0.0.1", listen_port=0, ping_interval=0.2)
            await source_runtime_a.start()
            await source_runtime_b.start()

            target_service = _make_service(Path(tempdir) / "target.sqlite3", start_time=1_700_001_000)
            target_runtime = NodeRuntime(
                service=target_service,
                listen_host="127.0.0.1",
                listen_port=0,
                outbound_peers=[
                    OutboundPeer("127.0.0.1", source_runtime_a.bound_port),
                    OutboundPeer("127.0.0.1", source_runtime_b.bound_port),
                ],
                connect_interval=0.1,
                ping_interval=0.2,
                headers_sync_parallel_peers=2,
                block_download_window_size=4,
                block_max_inflight_per_peer=2,
            )
            await target_runtime.start()
            try:
                await _wait_until(lambda: target_service.chain_tip() is not None and target_service.chain_tip().block_hash == latest_block.block_hash())
                sync_status = target_service.status()["sync"]
                assert sync_status["mode"] == "synced"
                assert sync_status["best_header_hash"] == latest_block.block_hash()
                assert len(sync_status["block_peers"]) == 2
            finally:
                await target_runtime.stop()
                await source_runtime_b.stop()
                await source_runtime_a.stop()

    asyncio.run(scenario())


def test_runtime_applies_backoff_and_score_after_handshake_failures() -> None:
    async def bad_peer(reader, writer):
        writer.close()
        await writer.wait_closed()

    async def scenario() -> None:
        with TemporaryDirectory() as tempdir:
            service = _make_service(Path(tempdir) / "node.sqlite3", start_time=1_700_000_000)
            server = await asyncio.start_server(bad_peer, "127.0.0.1", 0)
            port = int(server.sockets[0].getsockname()[1])
            runtime = NodeRuntime(
                service=service,
                listen_host="127.0.0.1",
                listen_port=0,
                outbound_peers=[OutboundPeer("127.0.0.1", port)],
                connect_interval=0.05,
                ping_interval=0.2,
            )
            await runtime.start()
            try:
                await _wait_until(
                    lambda: any(
                        peer.host == "127.0.0.1"
                        and peer.port == port
                        and peer.reconnect_attempts is not None
                        and peer.reconnect_attempts >= 1
                        and peer.score is not None
                        and peer.score < 0
                        and peer.backoff_until is not None
                        and peer.backoff_until > 0
                        and peer.protocol_error_class == "connection_closed"
                        for peer in service.list_peers()
                    )
                )
            finally:
                await runtime.stop()
                server.close()
                await server.wait_closed()

    asyncio.run(scenario())


def test_runtime_classifies_handshake_timeout() -> None:
    async def silent_peer(reader, writer):
        await asyncio.sleep(1.0)
        writer.close()
        await writer.wait_closed()

    async def scenario() -> None:
        with TemporaryDirectory() as tempdir:
            service = _make_service(Path(tempdir) / "node.sqlite3", start_time=1_700_000_000)
            server = await asyncio.start_server(silent_peer, "127.0.0.1", 0)
            port = int(server.sockets[0].getsockname()[1])
            runtime = NodeRuntime(
                service=service,
                listen_host="127.0.0.1",
                listen_port=0,
                outbound_peers=[OutboundPeer("127.0.0.1", port)],
                connect_interval=0.05,
                ping_interval=0.2,
                handshake_timeout=0.1,
            )
            await runtime.start()
            try:
                await _wait_until(
                    lambda: any(
                        peer.host == "127.0.0.1"
                        and peer.port == port
                        and peer.protocol_error_class == "handshake_failed"
                        for peer in service.list_peers()
                    )
                )
            finally:
                await runtime.stop()
                server.close()
                await server.wait_closed()

    asyncio.run(scenario())


def test_runtime_penalizes_malformed_peer_messages() -> None:
    async def malformed_peer(reader, writer):
        frame = bytearray(encode_message(MessageEnvelope(command="verack", payload=EmptyPayload()), magic=MAINNET_CONFIG.magic))
        frame[20:24] = b"bad!"
        writer.write(bytes(frame))
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    async def scenario() -> None:
        with TemporaryDirectory() as tempdir:
            service = _make_service(Path(tempdir) / "node.sqlite3", start_time=1_700_000_000)
            server = await asyncio.start_server(malformed_peer, "127.0.0.1", 0)
            port = int(server.sockets[0].getsockname()[1])
            runtime = NodeRuntime(
                service=service,
                listen_host="127.0.0.1",
                listen_port=0,
                outbound_peers=[OutboundPeer("127.0.0.1", port)],
                connect_interval=0.05,
                ping_interval=0.2,
            )
            await runtime.start()
            try:
                await _wait_until(
                    lambda: any(
                        peer.host == "127.0.0.1"
                        and peer.port == port
                        and peer.last_error is not None
                        and peer.score is not None
                        and peer.score < 0
                        and peer.protocol_error_class == "checksum_error"
                        for peer in service.list_peers()
                    )
                )
            finally:
                await runtime.stop()
                server.close()
                await server.wait_closed()

    asyncio.run(scenario())


def test_runtimes_on_different_network_magics_fail_fast() -> None:
    async def scenario() -> None:
        with TemporaryDirectory() as tempdir:
            main_service = _make_service(Path(tempdir) / "main.sqlite3", start_time=1_700_000_000)
            dev_service = NodeService.open_sqlite(
                Path(tempdir) / "dev.sqlite3",
                network="devnet",
                params=DEVNET_PARAMS,
                time_provider=lambda: 1_700_001_000,
            )
            main_runtime = NodeRuntime(service=main_service, listen_host="127.0.0.1", listen_port=0, ping_interval=0.2)
            await main_runtime.start()
            dev_runtime = NodeRuntime(
                service=dev_service,
                listen_host="127.0.0.1",
                listen_port=0,
                outbound_peers=[OutboundPeer("127.0.0.1", main_runtime.bound_port)],
                connect_interval=0.1,
                ping_interval=0.2,
            )
            await dev_runtime.start()
            try:
                await _wait_until(
                    lambda: (
                        any(
                            peer.protocol_error_class == "wrong_network_magic"
                            for peer in main_service.list_peers()
                        )
                        or any(
                            peer.host == "127.0.0.1"
                            and peer.port == main_runtime.bound_port
                            and (
                                peer.protocol_error_class == "wrong_network_magic"
                                or peer.protocol_error_class == "connection_closed"
                            )
                            for peer in dev_service.list_peers()
                        )
                    )
                )
                assert main_runtime.connected_peer_count() == 0
                assert dev_runtime.connected_peer_count() == 0
            finally:
                await dev_runtime.stop()
                await main_runtime.stop()

    asyncio.run(scenario())


def test_devnet_runtime_rejects_testnet_peer_before_handshake_or_sync() -> None:
    async def scenario() -> None:
        with TemporaryDirectory() as tempdir:
            dev_service = NodeService.open_sqlite(
                Path(tempdir) / "dev.sqlite3",
                network="devnet",
                time_provider=lambda: 1_700_000_000,
            )
            test_service = NodeService.open_sqlite(
                Path(tempdir) / "test.sqlite3",
                network="testnet",
                time_provider=lambda: 1_700_001_000,
            )
            dev_runtime = NodeRuntime(service=dev_service, listen_host="127.0.0.1", listen_port=0, ping_interval=0.2)
            await dev_runtime.start()
            test_runtime = NodeRuntime(
                service=test_service,
                listen_host="127.0.0.1",
                listen_port=0,
                outbound_peers=[OutboundPeer("127.0.0.1", dev_runtime.bound_port)],
                connect_interval=0.1,
                ping_interval=0.2,
                handshake_timeout=0.5,
            )
            await test_runtime.start()
            try:
                await _wait_until(
                    lambda: dev_service.peer_summary()["error_class_counts"].get("wrong_network_magic", 0) >= 1
                )
                assert dev_runtime.connected_peer_count() == 0
                assert test_runtime.connected_peer_count() == 0
                assert not any(peer.handshake_complete is True for peer in dev_service.list_peers())
                assert not any(peer.handshake_complete is True for peer in test_service.list_peers())
                assert dev_service.status()["sync"]["header_peers"] == []
                assert dev_service.status()["sync"]["block_peers"] == []
                assert test_service.status()["sync"]["header_peers"] == []
                assert test_service.status()["sync"]["block_peers"] == []
            finally:
                await test_runtime.stop()
                await dev_runtime.stop()

    asyncio.run(scenario())


def test_testnet_runtime_rejects_devnet_peer_before_handshake_or_sync() -> None:
    async def scenario() -> None:
        with TemporaryDirectory() as tempdir:
            test_service = NodeService.open_sqlite(
                Path(tempdir) / "test.sqlite3",
                network="testnet",
                time_provider=lambda: 1_700_000_000,
            )
            dev_service = NodeService.open_sqlite(
                Path(tempdir) / "dev.sqlite3",
                network="devnet",
                time_provider=lambda: 1_700_001_000,
            )
            test_runtime = NodeRuntime(service=test_service, listen_host="127.0.0.1", listen_port=0, ping_interval=0.2)
            await test_runtime.start()
            dev_runtime = NodeRuntime(
                service=dev_service,
                listen_host="127.0.0.1",
                listen_port=0,
                outbound_peers=[OutboundPeer("127.0.0.1", test_runtime.bound_port)],
                connect_interval=0.1,
                ping_interval=0.2,
                handshake_timeout=0.5,
            )
            await dev_runtime.start()
            try:
                await _wait_until(
                    lambda: test_service.peer_summary()["error_class_counts"].get("wrong_network_magic", 0) >= 1
                )
                assert test_runtime.connected_peer_count() == 0
                assert dev_runtime.connected_peer_count() == 0
                assert not any(peer.handshake_complete is True for peer in test_service.list_peers())
                assert not any(peer.handshake_complete is True for peer in dev_service.list_peers())
                assert test_service.status()["sync"]["header_peers"] == []
                assert test_service.status()["sync"]["block_peers"] == []
                assert dev_service.status()["sync"]["header_peers"] == []
                assert dev_service.status()["sync"]["block_peers"] == []
            finally:
                await dev_runtime.stop()
                await test_runtime.stop()

    asyncio.run(scenario())


def test_runtime_relays_transaction_inserted_via_shared_node_database() -> None:
    async def scenario() -> None:
        with TemporaryDirectory() as tempdir:
            source_db = Path(tempdir) / "source.sqlite3"
            source_service = _make_service(source_db, start_time=1_700_000_000)
            target_service = _make_service(Path(tempdir) / "target.sqlite3", start_time=1_700_001_000)
            source_runtime = NodeRuntime(service=source_service, listen_host="127.0.0.1", listen_port=0, ping_interval=0.2)
            await source_runtime.start()
            funding_block = _mine_block(source_service.build_candidate_block(wallet_key(0).address).block)
            source_service.apply_block(funding_block)

            target_runtime = NodeRuntime(
                service=target_service,
                listen_host="127.0.0.1",
                listen_port=0,
                outbound_peers=[OutboundPeer("127.0.0.1", source_runtime.bound_port)],
                connect_interval=0.1,
                ping_interval=0.2,
            )
            await target_runtime.start()
            try:
                await _wait_until(
                    lambda: target_service.chain_tip() is not None
                    and target_service.chain_tip().block_hash == funding_block.block_hash()
                )

                http_service = NodeService.open_sqlite(source_db, params=TEST_PARAMS, time_provider=lambda: 1_700_002_000)
                transaction = signed_payment(
                    OutPoint(txid=funding_block.transactions[0].txid(), index=0),
                    value=int(funding_block.transactions[0].outputs[0].value),
                    sender=wallet_key(0),
                    fee=10,
                )
                http_service.receive_transaction(transaction)

                await _wait_until(lambda: (target_service.find_transaction(transaction.txid()) or {}).get("location") == "mempool")
            finally:
                await target_runtime.stop()
                await source_runtime.stop()

    asyncio.run(scenario())


def test_node_restart_preserves_chain_and_miner_reconnects() -> None:
    async def scenario() -> None:
        with TemporaryDirectory() as tempdir:
            source_db = Path(tempdir) / "source.sqlite3"
            miner_db = Path(tempdir) / "miner.sqlite3"
            port = _free_port()
            source_service = _make_service(source_db, start_time=1_700_000_000)
            initial_block = _mine_block(source_service.build_candidate_block(wallet_key(0).address).block)
            source_service.apply_block(initial_block)
            initial_height = source_service.chain_tip().height

            source_runtime = NodeRuntime(
                service=source_service,
                listen_host="127.0.0.1",
                listen_port=port,
                ping_interval=0.2,
                read_timeout=1.0,
                handshake_timeout=1.0,
            )
            miner_service = _make_service(miner_db, start_time=1_700_001_000)
            miner_runtime = NodeRuntime(
                service=miner_service,
                listen_host="127.0.0.1",
                listen_port=0,
                outbound_peers=[OutboundPeer("127.0.0.1", port)],
                connect_interval=0.1,
                ping_interval=0.2,
                read_timeout=1.0,
                handshake_timeout=1.0,
            )
            await source_runtime.start()
            await miner_runtime.start()
            try:
                await _wait_until(
                    lambda: miner_service.chain_tip() is not None
                    and miner_service.chain_tip().height >= initial_height
                )
                await _wait_until(
                    lambda: any(peer.handshake_complete for peer in source_service.list_peers())
                        and any(peer.handshake_complete for peer in miner_service.list_peers()),
                    timeout=10.0,
                )
                pre_restart_tip = source_service.chain_tip()
                assert pre_restart_tip is not None

                await source_runtime.stop()

                restarted_service = NodeService.open_sqlite(
                    source_db,
                    params=TEST_PARAMS,
                    time_provider=lambda: 1_700_002_000,
                )
                assert restarted_service.chain_tip() is not None
                assert restarted_service.chain_tip().block_hash == pre_restart_tip.block_hash

                restarted_runtime = NodeRuntime(
                    service=restarted_service,
                    listen_host="127.0.0.1",
                    listen_port=port,
                    ping_interval=0.2,
                    read_timeout=1.0,
                    handshake_timeout=1.0,
                )
                await restarted_runtime.start()
                try:
                    await _wait_until(
                        lambda: any(peer.handshake_complete for peer in restarted_service.list_peers())
                        and any(peer.handshake_complete for peer in miner_service.list_peers()),
                        timeout=10.0,
                    )
                finally:
                    await restarted_runtime.stop()
            finally:
                await miner_runtime.stop()

    asyncio.run(scenario())


def test_template_miner_restart_resumes_without_chain_sync() -> None:
    async def scenario() -> None:
        _require_local_socket_support()
        with TemporaryDirectory() as tempdir:
            node_service = _make_service(Path(tempdir) / "node.sqlite3", start_time=1_700_000_000)
            node_runtime = NodeRuntime(
                service=node_service,
                listen_host="127.0.0.1",
                listen_port=0,
                http_host="127.0.0.1",
                http_port=_free_port(),
                ping_interval=0.2,
            )
            await node_runtime.start()
            try:
                first_worker = MinerWorker(
                    MinerWorkerConfig(
                        network="mainnet",
                        payout_address=wallet_key(1).address,
                        node_urls=(f"http://127.0.0.1:{node_runtime.http_bound_port}",),
                        miner_id="worker-a",
                        nonce_batch_size=2_000_000,
                        mining_min_interval_seconds=5.0,
                        run_seconds=0.3,
                    )
                )
                first_result = await asyncio.to_thread(first_worker.run)
                assert first_result["accepted_blocks"] >= 1
                first_tip = node_service.chain_tip()
                assert first_tip is not None

                restarted_worker = MinerWorker(
                    MinerWorkerConfig(
                        network="mainnet",
                        payout_address=wallet_key(1).address,
                        node_urls=(f"http://127.0.0.1:{node_runtime.http_bound_port}",),
                        miner_id="worker-a",
                        nonce_batch_size=2_000_000,
                        mining_min_interval_seconds=5.0,
                        run_seconds=0.3,
                    )
                )
                second_result = await asyncio.to_thread(restarted_worker.run)
                assert second_result["accepted_blocks"] >= 1
                assert node_service.chain_tip() is not None
                assert node_service.chain_tip().height > first_tip.height
            finally:
                await node_runtime.stop()

    asyncio.run(scenario())


def test_template_miner_fails_over_to_secondary_node_runtime() -> None:
    async def scenario() -> None:
        _require_local_socket_support()
        with TemporaryDirectory() as tempdir:
            secondary_service = _make_service(Path(tempdir) / "node-b.sqlite3", start_time=1_700_001_000)
            secondary_runtime = NodeRuntime(
                service=secondary_service,
                listen_host="127.0.0.1",
                listen_port=0,
                http_host="127.0.0.1",
                http_port=_free_port(),
                ping_interval=0.2,
            )
            await secondary_runtime.start()
            try:
                worker = MinerWorker(
                    MinerWorkerConfig(
                        network="mainnet",
                        payout_address=wallet_key(1).address,
                        node_urls=(
                            "http://127.0.0.1:1",
                            f"http://127.0.0.1:{secondary_runtime.http_bound_port}",
                        ),
                        miner_id="worker-a",
                        nonce_batch_size=2_000_000,
                        mining_min_interval_seconds=5.0,
                        run_seconds=0.3,
                    )
                )

                result = await asyncio.to_thread(worker.run)

                assert result["accepted_blocks"] >= 1
                assert secondary_service.chain_tip() is not None
            finally:
                await secondary_runtime.stop()

    asyncio.run(scenario())


def test_runtime_bootstraps_from_snapshot_and_syncs_only_post_anchor_delta() -> None:
    async def scenario() -> None:
        with TemporaryDirectory() as tempdir:
            source_service = _make_service(Path(tempdir) / "source.sqlite3", start_time=1_700_000_000)
            for _ in range(4):
                source_service.apply_block(_mine_block(source_service.build_candidate_block("CHCsource").block))
            snapshot_path = Path(tempdir) / "chain.snapshot.json"
            source_service.export_snapshot_file(snapshot_path)
            for _ in range(2):
                source_service.apply_block(_mine_block(source_service.build_candidate_block("CHCsource").block))

            target_service = _make_service(Path(tempdir) / "target.sqlite3", start_time=1_700_001_000)
            target_service.import_snapshot_file(snapshot_path)

            source_runtime = NodeRuntime(service=source_service, listen_host="127.0.0.1", listen_port=0, ping_interval=0.2)
            await source_runtime.start()
            target_runtime = NodeRuntime(
                service=target_service,
                listen_host="127.0.0.1",
                listen_port=0,
                outbound_peers=[OutboundPeer("127.0.0.1", source_runtime.bound_port)],
                connect_interval=0.1,
                ping_interval=0.2,
            )
            await target_runtime.start()
            try:
                await _wait_until(
                    lambda: target_service.chain_tip() is not None
                    and source_service.chain_tip() is not None
                    and target_service.chain_tip().block_hash == source_service.chain_tip().block_hash,
                    timeout=10.0,
                )
                assert target_service.snapshot_anchor() is not None
                assert target_service.snapshot_anchor().height == 3
            finally:
                await target_runtime.stop()
                await source_runtime.stop()

    asyncio.run(scenario())


def test_post_handshake_idle_read_timeout_does_not_drop_healthy_sessions() -> None:
    async def scenario() -> None:
        with TemporaryDirectory() as tempdir:
            left_service = _make_service(Path(tempdir) / "left.sqlite3", start_time=1_700_000_000)
            right_service = _make_service(Path(tempdir) / "right.sqlite3", start_time=1_700_001_000)
            port = _free_port()
            left_runtime = NodeRuntime(
                service=left_service,
                listen_host="127.0.0.1",
                listen_port=port,
                connect_interval=0.1,
                ping_interval=2.0,
                read_timeout=0.2,
                handshake_timeout=1.0,
            )
            right_runtime = NodeRuntime(
                service=right_service,
                listen_host="127.0.0.1",
                listen_port=0,
                outbound_peers=[OutboundPeer("127.0.0.1", port)],
                connect_interval=0.1,
                ping_interval=2.0,
                read_timeout=0.2,
                handshake_timeout=1.0,
            )
            await left_runtime.start()
            await right_runtime.start()
            try:
                await _wait_until(lambda: left_runtime.connected_peer_count() == 1 and right_runtime.connected_peer_count() == 1)
                await asyncio.sleep(0.75)
                assert left_runtime.connected_peer_count() == 1
                assert right_runtime.connected_peer_count() == 1
            finally:
                await right_runtime.stop()
                await left_runtime.stop()

    asyncio.run(scenario())


def _make_service(database_path: Path, *, start_time: int) -> NodeService:
    timestamps = iter(range(start_time, start_time + 1000))
    return NodeService.open_sqlite(database_path, params=TEST_PARAMS, time_provider=lambda: next(timestamps))


def _mine_block(block):
    for nonce in range(2_000_000):
        header = replace(block.header, nonce=nonce)
        if verify_proof_of_work(header):
            return replace(block, header=header)
    raise AssertionError("Expected to find a valid nonce for the easy target.")


async def _wait_until(predicate, *, timeout: float = 5.0, interval: float = 0.05) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(interval)
    raise AssertionError("Condition was not satisfied before timeout.")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _require_local_socket_support() -> None:
    try:
        _free_port()
    except OSError as exc:
        pytest.skip(f"local TCP sockets unavailable in this environment: {exc}")
