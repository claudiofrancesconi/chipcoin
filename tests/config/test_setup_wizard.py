from __future__ import annotations

import importlib.util
from pathlib import Path
from tempfile import TemporaryDirectory

from chipcoin.node.service import NodeService


REPO_ROOT = Path(__file__).resolve().parents[2]
WIZARD_PATH = REPO_ROOT / "scripts" / "setup" / "wizard.py"


def load_wizard_module():
    spec = importlib.util.spec_from_file_location("chipcoin_setup_wizard", WIZARD_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _make_snapshot(service: NodeService, snapshot_path: Path) -> dict[str, object]:
    block = service.build_candidate_block("CHCtest-bootstrap").block
    mined = None
    from dataclasses import replace
    from chipcoin.consensus.pow import verify_proof_of_work

    for nonce in range(2_000_000):
        candidate = replace(block.header, nonce=nonce)
        if verify_proof_of_work(candidate):
            mined = replace(block, header=candidate)
            break
    assert mined is not None
    service.apply_block(mined)
    return service.export_snapshot_file(snapshot_path, format_version=2)


def test_quick_mode_same_host_defaults_node_remote_miner_local_http() -> None:
    wizard = load_wizard_module()
    env_values = dict(wizard.DEFAULTS)

    wizard._apply_setup_mode(env_values, "quick", "both")

    assert env_values["NODE_DIRECT_PEERS"] == ""
    assert env_values["NODE_BOOTSTRAP_URL"] == "http://chipcoinprotocol.com:28080"
    assert env_values["MINING_NODE_URLS"] == "http://node:8081"
    assert env_values["DIRECT_PEERS"] == ""
    assert env_values["BOOTSTRAP_URL"] == ""


def test_quick_mode_miner_only_defaults_to_remote_peer() -> None:
    wizard = load_wizard_module()
    env_values = dict(wizard.DEFAULTS)

    wizard._apply_setup_mode(env_values, "quick", "miner")

    assert env_values["NODE_DIRECT_PEERS"] == ""
    assert env_values["NODE_BOOTSTRAP_URL"] == "http://chipcoinprotocol.com:28080"
    assert env_values["MINING_NODE_URLS"] == "https://api.chipcoinprotocol.com"


def test_local_mode_same_host_keeps_node_isolated_and_miner_local_http() -> None:
    wizard = load_wizard_module()
    env_values = dict(wizard.DEFAULTS)

    wizard._apply_setup_mode(env_values, "local", "both")

    assert env_values["NODE_DIRECT_PEERS"] == ""
    assert env_values["NODE_BOOTSTRAP_URL"] == ""
    assert env_values["MINING_NODE_URLS"] == "http://node:8081"
    assert env_values["DIRECT_PEERS"] == ""


def test_env_examples_expose_service_specific_discovery_defaults() -> None:
    for env_path in [REPO_ROOT / ".env.example", REPO_ROOT / "config" / "env" / ".env.example"]:
        content = env_path.read_text(encoding="utf-8")
        assert "NODE_DIRECT_PEERS=" in content
        assert "NODE_BOOTSTRAP_URL=" in content
        assert "BOOTSTRAP_ANNOUNCE_ENABLED=" in content
        assert "NODE_PUBLIC_HOST=" in content
        assert "NODE_PUBLIC_P2P_PORT=" in content
        assert "MINING_NODE_URLS=" in content


def test_configure_node_discovery_defaults_to_bootstrap_and_public_standard_port(monkeypatch) -> None:
    wizard = load_wizard_module()
    env_values = dict(wizard.DEFAULTS)
    answers = iter([
        "",  # bootstrap seed default
        "",  # default bootstrap URL
        "yes",
        "node.example.com",
        "",  # default public port from NODE_P2P_BIND_PORT
    ])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))

    wizard._configure_node_discovery(env_values, setup_mode="quick")

    assert env_values["NODE_DIRECT_PEERS"] == ""
    assert env_values["NODE_BOOTSTRAP_URL"] == "http://chipcoinprotocol.com:28080"
    assert env_values["BOOTSTRAP_ANNOUNCE_ENABLED"] == "true"
    assert env_values["NODE_PUBLIC_HOST"] == "node.example.com"
    assert env_values["NODE_PUBLIC_P2P_PORT"] == env_values["NODE_P2P_BIND_PORT"] == "18444"


def test_configure_node_discovery_requires_explicit_public_host(monkeypatch) -> None:
    wizard = load_wizard_module()
    env_values = dict(wizard.DEFAULTS)
    monkeypatch.setattr(wizard, "_detect_public_ip", lambda: "")
    answers = iter([
        "",   # bootstrap seed default
        "",   # default bootstrap URL
        "yes",
        "tilt.example.net",
        "",   # accept standard public port default
    ])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))

    wizard._configure_node_discovery(env_values, setup_mode="quick")

    assert env_values["BOOTSTRAP_ANNOUNCE_ENABLED"] == "true"
    assert env_values["NODE_PUBLIC_HOST"] == "tilt.example.net"
    assert env_values["NODE_PUBLIC_P2P_PORT"] == "18444"


def test_configure_node_discovery_uses_detected_public_ip_as_default(monkeypatch) -> None:
    wizard = load_wizard_module()
    env_values = dict(wizard.DEFAULTS)
    monkeypatch.setattr(wizard, "_detect_public_ip", lambda: "198.51.100.10")
    answers = iter([
        "",   # bootstrap seed default
        "",   # default bootstrap URL
        "yes",
        "",   # accept detected public IP
        "",   # accept standard public port default
    ])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))

    wizard._configure_node_discovery(env_values, setup_mode="quick")

    assert env_values["BOOTSTRAP_ANNOUNCE_ENABLED"] == "true"
    assert env_values["NODE_PUBLIC_HOST"] == "198.51.100.10"
    assert env_values["NODE_PUBLIC_P2P_PORT"] == "18444"


def test_configure_node_discovery_manual_mode_disables_bootstrap_url(monkeypatch) -> None:
    wizard = load_wizard_module()
    env_values = dict(wizard.DEFAULTS)
    answers = iter([
        "manual",
        "",
        "no",
    ])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))

    wizard._configure_node_discovery(env_values, setup_mode="quick")

    assert env_values["NODE_DIRECT_PEERS"] == "chipcoinprotocol.com:18444"
    assert env_values["NODE_BOOTSTRAP_URL"] == ""
    assert env_values["BOOTSTRAP_ANNOUNCE_ENABLED"] == "false"


def test_prepare_runtime_files_skips_node_db_for_miner_only() -> None:
    wizard = load_wizard_module()
    with TemporaryDirectory() as tempdir:
        node_data_path = Path(tempdir) / "node-devnet.sqlite3"
        env_values = dict(wizard.DEFAULTS)
        env_values["NODE_DATA_PATH"] = str(node_data_path)

        wizard._prepare_runtime_files(env_values, role="miner")

        assert node_data_path.exists() is False


def test_prepare_runtime_files_creates_node_db_for_node_role() -> None:
    wizard = load_wizard_module()
    with TemporaryDirectory() as tempdir:
        node_data_path = Path(tempdir) / "node-devnet.sqlite3"
        env_values = dict(wizard.DEFAULTS)
        env_values["NODE_DATA_PATH"] = str(node_data_path)

        wizard._prepare_runtime_files(env_values, role="node")

        assert node_data_path.is_file()


def test_parse_snapshot_manifest_and_select_latest_compatible_snapshot() -> None:
    wizard = load_wizard_module()
    manifest = {
        "snapshots": [
            {
                "network": "devnet",
                "snapshot_url": "https://snapshots.example/devnet-100.snapshot",
                "format_version": 2,
                "snapshot_height": 100,
                "snapshot_block_hash": "aa" * 32,
                "created_at": 1000,
                "checksum_sha256": "11" * 32,
            },
            {
                "network": "devnet",
                "snapshot_url": "https://snapshots.example/devnet-120.snapshot",
                "format_version": 2,
                "snapshot_height": 120,
                "snapshot_block_hash": "bb" * 32,
                "created_at": 2000,
                "checksum_sha256": "22" * 32,
            },
            {
                "network": "mainnet",
                "snapshot_url": "https://snapshots.example/mainnet-500.snapshot",
                "format_version": 2,
                "snapshot_height": 500,
                "snapshot_block_hash": "cc" * 32,
                "created_at": 3000,
                "checksum_sha256": "33" * 32,
            },
        ]
    }

    entries = wizard._parse_snapshot_manifest(manifest, manifest_url="https://manifest.example")
    assert len(entries) == 3

    wizard._fetch_json_from_url = lambda url: manifest
    selected = wizard._select_latest_compatible_snapshot(["https://manifest.example"], network="devnet")
    assert selected.snapshot_height == 120
    assert selected.snapshot_url == "https://snapshots.example/devnet-120.snapshot"


def test_select_latest_compatible_snapshot_tries_manifest_urls_in_order() -> None:
    wizard = load_wizard_module()
    manifest_a = {"snapshots": [{"network": "mainnet", "snapshot_url": "https://a", "format_version": 2, "snapshot_height": 1, "snapshot_block_hash": "aa" * 32, "created_at": 1, "checksum_sha256": "11" * 32}]}
    manifest_b = {"snapshots": [{"network": "devnet", "snapshot_url": "https://b", "format_version": 2, "snapshot_height": 50, "snapshot_block_hash": "bb" * 32, "created_at": 2, "checksum_sha256": "22" * 32}]}

    def _fetch(url: str):
        if url == "https://manifest-a.example":
            return manifest_a
        if url == "https://manifest-b.example":
            return manifest_b
        raise AssertionError(url)

    wizard._fetch_json_from_url = _fetch
    selected = wizard._select_latest_compatible_snapshot(
        ["https://manifest-a.example", "https://manifest-b.example"],
        network="devnet",
    )
    assert selected.manifest_url == "https://manifest-b.example"
    assert selected.snapshot_url == "https://b"


def test_prepare_node_bootstrap_auto_imports_snapshot() -> None:
    wizard = load_wizard_module()
    with TemporaryDirectory() as tempdir:
        temp_root = Path(tempdir)
        node_data_path = temp_root / "node.sqlite3"
        snapshot_path = temp_root / "snapshot.snapshot"
        source_service = NodeService.open_sqlite(temp_root / "source.sqlite3", network="devnet")
        metadata = _make_snapshot(source_service, snapshot_path)

        env_values = dict(wizard.DEFAULTS)
        env_values["NODE_DATA_PATH"] = str(node_data_path)
        env_values["NODE_BOOTSTRAP_MODE"] = "auto"
        env_values["NODE_SNAPSHOT_MANIFEST_URLS"] = "https://manifest.example"
        env_values["NODE_SNAPSHOT_FILE"] = str(temp_root / "downloaded.snapshot")
        env_values["NODE_SNAPSHOT_TRUST_MODE"] = "off"

        wizard._prepare_runtime_files(env_values, role="node")
        wizard._fetch_json_from_url = lambda url: {
            "snapshots": [
                {
                    "network": "devnet",
                    "snapshot_url": "https://snapshots.example/devnet.snapshot",
                    "format_version": 2,
                    "snapshot_height": metadata["snapshot_height"],
                    "snapshot_block_hash": metadata["snapshot_block_hash"],
                    "created_at": metadata["created_at"],
                    "checksum_sha256": wizard._file_sha256(snapshot_path),
                }
            ]
        }
        wizard._download_snapshot_file = lambda url, destination: destination.write_bytes(snapshot_path.read_bytes())

        notes = wizard._prepare_node_bootstrap(env_values, network="devnet")

        assert env_values["NODE_BOOTSTRAP_MODE"] == "snapshot"
        assert "Snapshot selected:" in " ".join(notes)
        imported = NodeService.open_sqlite(node_data_path, network="devnet")
        assert imported.chain_tip() is not None
        assert imported.chain_tip().height == metadata["snapshot_height"]
        metadata_path = wizard._snapshot_metadata_path(node_data_path)
        assert metadata_path.is_file()
        snapshot_metadata = __import__("json").loads(metadata_path.read_text(encoding="utf-8"))
        assert snapshot_metadata["bootstrap_mode"] == "snapshot"
        assert snapshot_metadata["snapshot_height"] == metadata["snapshot_height"]


def test_prepare_node_bootstrap_auto_falls_back_when_download_fails() -> None:
    wizard = load_wizard_module()
    with TemporaryDirectory() as tempdir:
        temp_root = Path(tempdir)
        node_data_path = temp_root / "node.sqlite3"
        env_values = dict(wizard.DEFAULTS)
        env_values["NODE_DATA_PATH"] = str(node_data_path)
        env_values["NODE_BOOTSTRAP_MODE"] = "auto"
        env_values["NODE_SNAPSHOT_MANIFEST_URLS"] = "https://manifest.example"
        env_values["NODE_SNAPSHOT_FILE"] = str(temp_root / "downloaded.snapshot")
        wizard._prepare_runtime_files(env_values, role="node")
        wizard._fetch_json_from_url = lambda url: {
            "snapshots": [
                {
                    "network": "devnet",
                    "snapshot_url": "https://snapshots.example/devnet.snapshot",
                    "format_version": 2,
                    "snapshot_height": 10,
                    "snapshot_block_hash": "aa" * 32,
                    "created_at": 1000,
                    "checksum_sha256": "11" * 32,
                }
            ]
        }

        def _fail_download(url: str, destination: Path) -> None:
            raise RuntimeError("download failed")

        wizard._download_snapshot_file = _fail_download
        notes = wizard._prepare_node_bootstrap(env_values, network="devnet")

        assert env_values["NODE_BOOTSTRAP_MODE"] == "full"
        assert "Falling back to full sync" in " ".join(notes)


def test_prepare_node_bootstrap_warns_when_snapshot_is_stale_but_valid() -> None:
    wizard = load_wizard_module()
    with TemporaryDirectory() as tempdir:
        temp_root = Path(tempdir)
        node_data_path = temp_root / "node.sqlite3"
        snapshot_path = temp_root / "snapshot.snapshot"
        source_service = NodeService.open_sqlite(temp_root / "source.sqlite3", network="devnet")
        metadata = _make_snapshot(source_service, snapshot_path)

        env_values = dict(wizard.DEFAULTS)
        env_values["NODE_DATA_PATH"] = str(node_data_path)
        env_values["NODE_BOOTSTRAP_MODE"] = "snapshot"
        env_values["NODE_SNAPSHOT_MANIFEST_URLS"] = "https://manifest.example"
        env_values["NODE_SNAPSHOT_FILE"] = str(temp_root / "downloaded.snapshot")
        env_values["NODE_SNAPSHOT_TRUST_MODE"] = "off"

        wizard._prepare_runtime_files(env_values, role="node")
        wizard._fetch_json_from_url = lambda url: {
            "snapshots": [
                {
                    "network": "devnet",
                    "snapshot_url": "https://snapshots.example/devnet.snapshot",
                    "format_version": 2,
                    "snapshot_height": metadata["snapshot_height"],
                    "snapshot_block_hash": metadata["snapshot_block_hash"],
                    "created_at": 1,
                    "checksum_sha256": wizard._file_sha256(snapshot_path),
                }
            ]
        }
        wizard._download_snapshot_file = lambda url, destination: destination.write_bytes(snapshot_path.read_bytes())
        wizard.time.time = lambda: wizard.SNAPSHOT_LARGE_DELTA_WARNING_SECONDS + 5

        notes = wizard._prepare_node_bootstrap(env_values, network="devnet")

        notes_text = " ".join(notes)
        assert "still valid" in notes_text
        assert "large post-anchor delta sync" in notes_text
        imported = NodeService.open_sqlite(node_data_path, network="devnet")
        assert imported.chain_tip() is not None


def test_preflight_rejects_invalid_trusted_keys_path() -> None:
    wizard = load_wizard_module()
    with TemporaryDirectory() as tempdir:
        temp_root = Path(tempdir)
        env_values = dict(wizard.DEFAULTS)
        env_values["NODE_DATA_PATH"] = str(temp_root / "node.sqlite3")
        env_values["NODE_BOOTSTRAP_MODE"] = "snapshot"
        env_values["NODE_SNAPSHOT_MANIFEST_URLS"] = "https://manifest.example"
        env_values["NODE_SNAPSHOT_FILE"] = str(temp_root / "node.snapshot")
        env_values["NODE_SNAPSHOT_TRUST_MODE"] = "enforce"
        env_values["NODE_SNAPSHOT_TRUSTED_KEYS_FILE"] = str(temp_root / "missing-keys.txt")

        try:
            wizard._preflight_validate(env_values, role="node")
        except SystemExit as exc:
            assert exc.code == 1
        else:
            raise AssertionError("Expected preflight validation to fail for a missing trusted keys file.")


def test_preflight_rejects_invalid_manifest_url_syntax() -> None:
    wizard = load_wizard_module()
    with TemporaryDirectory() as tempdir:
        temp_root = Path(tempdir)
        env_values = dict(wizard.DEFAULTS)
        env_values["NODE_DATA_PATH"] = str(temp_root / "node.sqlite3")
        env_values["NODE_BOOTSTRAP_MODE"] = "snapshot"
        env_values["NODE_SNAPSHOT_MANIFEST_URLS"] = "not-a-url"
        env_values["NODE_SNAPSHOT_FILE"] = str(temp_root / "node.snapshot")
        env_values["NODE_SNAPSHOT_TRUST_MODE"] = "off"

        try:
            wizard._preflight_validate(env_values, role="node")
        except SystemExit as exc:
            assert exc.code == 1
        else:
            raise AssertionError("Expected preflight validation to fail for an invalid manifest URL.")


def test_preflight_rejects_invalid_public_announce_host() -> None:
    wizard = load_wizard_module()
    with TemporaryDirectory() as tempdir:
        temp_root = Path(tempdir)
        env_values = dict(wizard.DEFAULTS)
        env_values["NODE_DATA_PATH"] = str(temp_root / "node.sqlite3")
        env_values["BOOTSTRAP_ANNOUNCE_ENABLED"] = "true"
        env_values["NODE_BOOTSTRAP_URL"] = "http://bootstrap.example:28080"
        env_values["NODE_PUBLIC_HOST"] = "127.0.0.1"
        env_values["NODE_PUBLIC_P2P_PORT"] = "18444"

        try:
            wizard._preflight_validate(env_values, role="node")
        except SystemExit as exc:
            assert exc.code == 1
        else:
            raise AssertionError("Expected preflight validation to fail for a non-public announce host.")


def test_both_role_snapshot_bootstrap_keeps_local_miner_endpoint() -> None:
    wizard = load_wizard_module()
    with TemporaryDirectory() as tempdir:
        temp_root = Path(tempdir)
        node_data_path = temp_root / "node.sqlite3"
        snapshot_path = temp_root / "snapshot.snapshot"
        source_service = NodeService.open_sqlite(temp_root / "source.sqlite3", network="devnet")
        metadata = _make_snapshot(source_service, snapshot_path)

        env_values = dict(wizard.DEFAULTS)
        wizard._apply_setup_mode(env_values, "quick", "both")
        env_values["NODE_DATA_PATH"] = str(node_data_path)
        env_values["NODE_BOOTSTRAP_MODE"] = "snapshot"
        env_values["NODE_SNAPSHOT_MANIFEST_URLS"] = "https://manifest.example"
        env_values["NODE_SNAPSHOT_FILE"] = str(temp_root / "downloaded.snapshot")
        env_values["NODE_SNAPSHOT_TRUST_MODE"] = "off"

        wizard._preflight_validate(env_values, role="both")
        wizard._prepare_runtime_files(env_values, role="both")
        wizard._fetch_json_from_url = lambda url: {
            "snapshots": [
                {
                    "network": "devnet",
                    "snapshot_url": "https://snapshots.example/devnet.snapshot",
                    "format_version": 2,
                    "snapshot_height": metadata["snapshot_height"],
                    "snapshot_block_hash": metadata["snapshot_block_hash"],
                    "created_at": metadata["created_at"],
                    "checksum_sha256": wizard._file_sha256(snapshot_path),
                }
            ]
        }
        wizard._download_snapshot_file = lambda url, destination: destination.write_bytes(snapshot_path.read_bytes())

        notes = wizard._prepare_node_bootstrap(env_values, network="devnet")

        assert env_values["MINING_NODE_URLS"] == "http://node:8081"
        assert env_values["NODE_BOOTSTRAP_MODE"] == "snapshot"
        assert "Manifest source used:" in " ".join(notes)


def test_prepare_node_bootstrap_auto_cleans_partial_failure_state() -> None:
    wizard = load_wizard_module()
    with TemporaryDirectory() as tempdir:
        temp_root = Path(tempdir)
        node_data_path = temp_root / "node.sqlite3"
        snapshot_target = temp_root / "downloaded.snapshot"
        env_values = dict(wizard.DEFAULTS)
        env_values["NODE_DATA_PATH"] = str(node_data_path)
        env_values["NODE_BOOTSTRAP_MODE"] = "auto"
        env_values["NODE_SNAPSHOT_MANIFEST_URLS"] = "https://manifest.example"
        env_values["NODE_SNAPSHOT_FILE"] = str(snapshot_target)
        env_values["NODE_SNAPSHOT_TRUST_MODE"] = "off"

        wizard._prepare_runtime_files(env_values, role="node")
        node_data_path.write_text("dirty", encoding="utf-8")
        wizard._fetch_json_from_url = lambda url: {
            "snapshots": [
                {
                    "network": "devnet",
                    "snapshot_url": "https://snapshots.example/devnet.snapshot",
                    "format_version": 2,
                    "snapshot_height": 10,
                    "snapshot_block_hash": "aa" * 32,
                    "created_at": 1000,
                    "checksum_sha256": "11" * 32,
                }
            ]
        }

        def _bad_download(url: str, destination: Path) -> None:
            destination.write_text("corrupt", encoding="utf-8")
            raise RuntimeError("download interrupted")

        wizard._download_snapshot_file = _bad_download
        notes = wizard._prepare_node_bootstrap(env_values, network="devnet")

        assert env_values["NODE_BOOTSTRAP_MODE"] == "full"
        assert node_data_path.is_file()
        assert node_data_path.stat().st_size == 0
        assert not snapshot_target.exists()
        assert not wizard._snapshot_metadata_path(node_data_path).exists()
        assert "Falling back to full sync" in " ".join(notes)
