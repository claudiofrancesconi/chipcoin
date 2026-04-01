import asyncio
import socket
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

from chipcoin.config import MAINNET_CONFIG
from chipcoin.consensus.models import OutPoint
from chipcoin.consensus.params import DEVNET_PARAMS, MAINNET_PARAMS
from chipcoin.consensus.pow import verify_proof_of_work
from chipcoin.node.messages import EmptyPayload, MessageEnvelope
from chipcoin.node.p2p.codec import encode_message
from chipcoin.node.runtime import NodeRuntime, OutboundPeer
from chipcoin.node.service import NodeService
from tests.helpers import signed_payment, wallet_key


TEST_PARAMS = replace(MAINNET_PARAMS, coinbase_maturity=0)


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


def test_runtime_miner_produces_blocks_end_to_end() -> None:
    async def scenario() -> None:
        with TemporaryDirectory() as tempdir:
            service = _make_service(Path(tempdir) / "miner.sqlite3", start_time=1_700_000_000)
            runtime = NodeRuntime(
                service=service,
                listen_host="127.0.0.1",
                listen_port=0,
                ping_interval=0.2,
                miner_address="CHCminer-runtime",
                mining_nonce_batch_size=25_000,
            )
            await runtime.start()
            try:
                await _wait_until(lambda: service.chain_tip() is not None)
                assert service.chain_tip() is not None
                assert service.chain_tip().height >= 0
            finally:
                await runtime.stop()

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


def test_runtime_miner_refreshes_template_when_valid_mempool_tx_arrives() -> None:
    async def scenario() -> None:
        with TemporaryDirectory() as tempdir:
            service = _make_service(Path(tempdir) / "miner.sqlite3", start_time=1_700_000_000)
            initial_block = _mine_block(service.build_candidate_block(wallet_key(0).address).block)
            service.apply_block(initial_block)
            runtime = NodeRuntime(
                service=service,
                listen_host="127.0.0.1",
                listen_port=0,
                ping_interval=0.2,
                miner_address=wallet_key(0).address,
                mining_nonce_batch_size=25_000,
            )
            await runtime.start()
            try:
                funding_outpoint = OutPoint(txid=initial_block.transactions[0].txid(), index=0)
                transaction = signed_payment(
                    funding_outpoint,
                    value=int(initial_block.transactions[0].outputs[0].value),
                    sender=wallet_key(0),
                    fee=10,
                )
                await runtime.submit_transaction(transaction)
                await _wait_until(
                    lambda: (service.find_transaction(transaction.txid()) or {}).get("location") == "chain"
                )
                result = service.find_transaction(transaction.txid())
                assert result is not None
                assert result["location"] == "chain"
            finally:
                await runtime.stop()

    asyncio.run(scenario())


def test_valid_transaction_propagates_between_two_nodes_and_gets_mined() -> None:
    async def scenario() -> None:
        with TemporaryDirectory() as tempdir:
            source_service = _make_service(Path(tempdir) / "source.sqlite3", start_time=1_700_000_000)
            miner_service = _make_service(Path(tempdir) / "miner.sqlite3", start_time=1_700_001_000)

            source_runtime = NodeRuntime(service=source_service, listen_host="127.0.0.1", listen_port=0, ping_interval=0.2)
            await source_runtime.start()
            funding_block = _mine_block(source_service.build_candidate_block(wallet_key(0).address).block)
            source_service.apply_block(funding_block)

            miner_runtime = NodeRuntime(
                service=miner_service,
                listen_host="127.0.0.1",
                listen_port=0,
                outbound_peers=[OutboundPeer("127.0.0.1", source_runtime.bound_port)],
                connect_interval=0.1,
                ping_interval=0.2,
            )
            await miner_runtime.start()
            try:
                await _wait_until(
                    lambda: miner_service.chain_tip() is not None
                    and miner_service.chain_tip().block_hash == funding_block.block_hash()
                )
                await miner_runtime.stop()

                miner_runtime = NodeRuntime(
                    service=miner_service,
                    listen_host="127.0.0.1",
                    listen_port=0,
                    outbound_peers=[OutboundPeer("127.0.0.1", source_runtime.bound_port)],
                    connect_interval=0.1,
                    ping_interval=0.2,
                    miner_address=wallet_key(1).address,
                    mining_nonce_batch_size=25_000,
                )
                await miner_runtime.start()

                transaction = signed_payment(
                    OutPoint(txid=funding_block.transactions[0].txid(), index=0),
                    value=int(funding_block.transactions[0].outputs[0].value),
                    sender=wallet_key(0),
                    fee=10,
                )

                await source_runtime.submit_transaction(transaction)
                await _wait_until(
                    lambda: (miner_service.find_transaction(transaction.txid()) or {}).get("location") in {"mempool", "chain"}
                )
                await _wait_until(lambda: (source_service.find_transaction(transaction.txid()) or {}).get("location") == "chain")
                await _wait_until(lambda: (miner_service.find_transaction(transaction.txid()) or {}).get("location") == "chain")

                source_result = source_service.find_transaction(transaction.txid())
                miner_result = miner_service.find_transaction(transaction.txid())
                assert source_result is not None
                assert miner_result is not None
                assert source_result["location"] == "chain"
                assert miner_result["location"] == "chain"
            finally:
                await miner_runtime.stop()
                await source_runtime.stop()

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


def test_miner_restart_reconnects_and_resumes_mining() -> None:
    async def scenario() -> None:
        with TemporaryDirectory() as tempdir:
            source_service = _make_service(Path(tempdir) / "source.sqlite3", start_time=1_700_000_000)
            port = _free_port()
            source_runtime = NodeRuntime(
                service=source_service,
                listen_host="127.0.0.1",
                listen_port=port,
                ping_interval=0.2,
                read_timeout=1.0,
                handshake_timeout=1.0,
            )
            await source_runtime.start()
            try:
                miner_db = Path(tempdir) / "miner.sqlite3"
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
                    miner_address=wallet_key(1).address,
                    mining_nonce_batch_size=25_000,
                )
                await miner_runtime.start()
                try:
                    await _wait_until(lambda: source_service.chain_tip() is not None, timeout=10.0)
                    first_tip = source_service.chain_tip()
                    assert first_tip is not None
                finally:
                    await miner_runtime.stop()

                restarted_miner_service = NodeService.open_sqlite(
                    miner_db,
                    params=TEST_PARAMS,
                    time_provider=lambda: 1_700_002_000,
                )
                restarted_miner_runtime = NodeRuntime(
                    service=restarted_miner_service,
                    listen_host="127.0.0.1",
                    listen_port=0,
                    outbound_peers=[OutboundPeer("127.0.0.1", port)],
                    connect_interval=0.1,
                    ping_interval=0.2,
                    read_timeout=1.0,
                    handshake_timeout=1.0,
                    miner_address=wallet_key(1).address,
                    mining_nonce_batch_size=25_000,
                )
                await restarted_miner_runtime.start()
                try:
                    await _wait_until(
                        lambda: source_service.chain_tip() is not None and source_service.chain_tip().height > first_tip.height,
                        timeout=15.0,
                    )
                    await _wait_until(
                        lambda: any(peer.handshake_complete for peer in source_service.list_peers())
                        and any(peer.handshake_complete for peer in restarted_miner_service.list_peers()),
                        timeout=10.0,
                    )
                finally:
                    await restarted_miner_runtime.stop()
            finally:
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
