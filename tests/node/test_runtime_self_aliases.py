from pathlib import Path
from tempfile import TemporaryDirectory

from chipcoin.node.runtime import NodeRuntime, OutboundPeer
from chipcoin.node.service import NodeService
from tests.node.test_runtime_integration import TEST_PARAMS


def test_runtime_identifies_local_listener_aliases(monkeypatch) -> None:
    with TemporaryDirectory() as tempdir:
        service = NodeService.open_sqlite(
            Path(tempdir) / "node.sqlite3",
            params=TEST_PARAMS,
            time_provider=lambda: 1_700_000_000,
        )
        runtime = NodeRuntime(service=service, listen_host="0.0.0.0", listen_port=18444)

        monkeypatch.setattr(
            "chipcoin.node.runtime.socket.gethostbyname_ex",
            lambda hostname: (hostname, [], ["172.18.0.2"]),
        )

        def fake_getaddrinfo(host: str, port: int, type: int):
            if host in {"node", "172.18.0.2"} and port == 18444:
                return [(None, None, None, None, ("172.18.0.2", port))]
            if host == "172.18.0.3" and port == 18444:
                return [(None, None, None, None, ("172.18.0.3", port))]
            raise OSError("unresolvable")

        monkeypatch.setattr("chipcoin.node.runtime.socket.getaddrinfo", fake_getaddrinfo)

        assert runtime._is_local_listener_alias(OutboundPeer("node", 18444)) is True
        assert runtime._is_local_listener_alias(OutboundPeer("172.18.0.2", 18444)) is True
        assert runtime._is_local_listener_alias(OutboundPeer("172.18.0.3", 18444)) is False
        assert runtime._is_local_listener_alias(OutboundPeer("node", 18445)) is False
