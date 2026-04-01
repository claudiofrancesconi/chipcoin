import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory

from chipcoin.node.runtime import NodeRuntime, OutboundPeer
from chipcoin.node.service import NodeService
from tests.node.test_runtime_integration import TEST_PARAMS, _wait_until


def test_runtime_stops_redialing_after_duplicate_connection_is_resolved() -> None:
    async def scenario() -> None:
        with TemporaryDirectory() as tempdir:
            left_service = NodeService.open_sqlite(
                Path(tempdir) / "left.sqlite3",
                params=TEST_PARAMS,
                time_provider=lambda: 1_700_000_000,
            )
            right_service = NodeService.open_sqlite(
                Path(tempdir) / "right.sqlite3",
                params=TEST_PARAMS,
                time_provider=lambda: 1_700_001_000,
            )
            left_runtime = NodeRuntime(
                service=left_service,
                listen_host="127.0.0.1",
                listen_port=0,
                connect_interval=0.05,
                ping_interval=0.2,
            )
            right_runtime = NodeRuntime(
                service=right_service,
                listen_host="127.0.0.1",
                listen_port=0,
                connect_interval=0.05,
                ping_interval=0.2,
            )
            await left_runtime.start()
            await right_runtime.start()
            left_runtime._outbound_targets[("127.0.0.1", right_runtime.bound_port)] = OutboundPeer(
                "127.0.0.1",
                right_runtime.bound_port,
            )
            right_runtime._outbound_targets[("127.0.0.1", left_runtime.bound_port)] = OutboundPeer(
                "127.0.0.1",
                left_runtime.bound_port,
            )
            try:
                await _wait_until(lambda: left_runtime.connected_peer_count() == 1 and right_runtime.connected_peer_count() == 1)
                await _wait_until(
                    lambda: any(
                        peer.protocol_error_class == "duplicate_connection"
                        for peer in left_service.list_peers() + right_service.list_peers()
                    )
                )

                def duplicate_disconnect_total() -> int:
                    return sum(
                        0 if peer.disconnect_count is None else peer.disconnect_count
                        for peer in left_service.list_peers() + right_service.list_peers()
                        if peer.protocol_error_class == "duplicate_connection"
                    )

                baseline = duplicate_disconnect_total()
                await asyncio.sleep(0.5)
                assert left_runtime.connected_peer_count() == 1
                assert right_runtime.connected_peer_count() == 1
                assert duplicate_disconnect_total() == baseline
            finally:
                await right_runtime.stop()
                await left_runtime.stop()

    asyncio.run(scenario())
