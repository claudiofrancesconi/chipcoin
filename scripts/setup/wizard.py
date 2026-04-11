#!/usr/bin/env python3
"""Phase 1 setup wizard for the public Chipcoin runtime."""

from __future__ import annotations

import getpass
import hashlib
import ipaddress
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import NamedTuple
from urllib import error, request

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from chipcoin.interfaces.cli import _load_snapshot_trusted_keys  # noqa: E402
from chipcoin.crypto.keys import parse_private_key_hex  # noqa: E402
from chipcoin.node.service import NodeService  # noqa: E402
from chipcoin.wallet.signer import generate_wallet_key, wallet_key_from_private_key  # noqa: E402


ENV_PATH = REPO_ROOT / ".env"
ENV_EXAMPLE_PATH = REPO_ROOT / "config" / "env" / ".env.example"
RUNTIME_ROOT = Path("/var/lib/chipcoin")
NODE_DATA_PATH = str(RUNTIME_ROOT / "data" / "node-devnet.sqlite3")
NODE_SNAPSHOT_PATH = str(RUNTIME_ROOT / "data" / "node-devnet.snapshot")
WALLET_PATH = str(RUNTIME_ROOT / "wallets" / "chipcoin-wallet.json")
PUBLIC_DEVNET_NODE_ENDPOINT = "https://api.chipcoinprotocol.com"
PUBLIC_DEVNET_BOOTSTRAP_PEER = "chipcoinprotocol.com:18444"
PUBLIC_DEVNET_BOOTSTRAP_URL = "http://chipcoinprotocol.com:28080"
PUBLIC_DEVNET_EXPLORER_URL = "https://explorer.chipcoinprotocol.com"
PUBLIC_DEVNET_SNAPSHOT_MANIFEST_URL = "https://chipcoinprotocol.com/downloads/snapshots/devnet/latest.manifest.json"
SNAPSHOT_STALE_WARNING_SECONDS = 6 * 60 * 60
SNAPSHOT_LARGE_DELTA_WARNING_SECONDS = 24 * 60 * 60
COMPOSE_FILE_PATH = REPO_ROOT / "docker-compose.yml"
DEFAULTS = {
    "CHIPCOIN_NETWORK": "devnet",
    "COMPOSE_PROJECT_NAME": "chipcoin",
    "CHIPCOIN_RUNTIME_DIR": str(RUNTIME_ROOT),
    "DEFAULT_NODE_ENDPOINT": PUBLIC_DEVNET_NODE_ENDPOINT,
    "DEFAULT_BOOTSTRAP_PEER": PUBLIC_DEVNET_BOOTSTRAP_PEER,
    "DEFAULT_EXPLORER_URL": PUBLIC_DEVNET_EXPLORER_URL,
    "NODE_DATA_PATH": NODE_DATA_PATH,
    "NODE_BOOTSTRAP_MODE": "auto",
    "NODE_SNAPSHOT_MANIFEST_URLS": PUBLIC_DEVNET_SNAPSHOT_MANIFEST_URL,
    "NODE_SNAPSHOT_FILE": NODE_SNAPSHOT_PATH,
    "NODE_SNAPSHOT_TRUST_MODE": "warn",
    "NODE_SNAPSHOT_TRUSTED_KEYS_FILE": "",
    "NODE_SNAPSHOT_SELECTED_URL": "",
    "NODE_SNAPSHOT_SELECTED_HEIGHT": "",
    "NODE_SNAPSHOT_SELECTED_HASH": "",
    "NODE_LOG_LEVEL": "INFO",
    "NODE_P2P_BIND_PORT": "18444",
    "NODE_HTTP_BIND_PORT": "8081",
    "CHIPCOIN_HTTP_ALLOWED_ORIGINS": "",
    "MINER_LOG_LEVEL": "INFO",
    "MINER_WALLET_FILE": WALLET_PATH,
    "MINING_MIN_INTERVAL_SECONDS": "1.0",
    "MINING_NODE_URLS": "http://node:8081",
    "MINING_MINER_ID": "",
    "MINING_POLLING_INTERVAL_SECONDS": "2.0",
    "MINING_REQUEST_TIMEOUT_SECONDS": "10.0",
    "MINING_NONCE_BATCH_SIZE": "250000",
    "MINING_TEMPLATE_REFRESH_SKEW_SECONDS": "1",
    "BROWSER_WALLET_DEFAULT_NODE_ENDPOINT": PUBLIC_DEVNET_NODE_ENDPOINT,
    "NODE_DIRECT_PEERS": "",
    "NODE_DIRECT_PEER": "",
    "NODE_BOOTSTRAP_URL": PUBLIC_DEVNET_BOOTSTRAP_URL,
    "NODE_PUBLIC_HOST": "",
    "NODE_PUBLIC_P2P_PORT": "18444",
    "DIRECT_PEERS": "",
    "DIRECT_PEER": "",
    "BOOTSTRAP_URL": "",
    "BOOTSTRAP_PEER_LIMIT": "4",
    "BOOTSTRAP_ANNOUNCE_ENABLED": "false",
    "BOOTSTRAP_REFRESH_INTERVAL_SECONDS": "60",
    "INITIAL_SYNC_CONSERVATIVE_DEFAULTS": "true",
}


class SnapshotManifestEntry(NamedTuple):
    """One downloadable snapshot advertised by a manifest."""

    manifest_url: str
    network: str
    snapshot_url: str
    format_version: int
    snapshot_height: int
    snapshot_block_hash: str
    created_at: int
    checksum_sha256: str
    signer_pubkeys: tuple[str, ...] = ()


def _snapshot_metadata_path(node_data_path: Path) -> Path:
    """Return the audit metadata file stored next to the node DB."""

    return node_data_path.with_suffix(node_data_path.suffix + ".snapshot.meta.json")


def main() -> int:
    print("Chipcoin Phase 1 Setup Wizard")
    _check_prerequisites()
    setup_mode = _ask_choice(
        "Select setup mode",
        {
            "quick": "Quick start (use public devnet defaults)",
            "custom": "Custom configuration",
            "local": "Local/self-hosted",
        },
        "quick",
    )
    role = _ask_choice("What do you want to run?", {"node": "Node", "miner": "Miner", "both": "Both"}, "both")
    network = _ask_choice("Which network do you want to use?", {"devnet": "Devnet"}, "devnet")
    runtime_mode = _ask_choice("How should services run?", {"foreground": "Foreground", "background": "Background"}, "foreground")
    _print_public_reachability_note()
    bootstrap_notes: list[str] = []

    wallet_path: Path | None = None
    wallet_address: str | None = None
    wallet_private_key_hex: str | None = None
    if role == "node":
        print("Node-only Phase 1 runtime does not consume a wallet yet. Skipping wallet setup.")
    else:
        wallet_mode = _ask_choice(
            "How should the wallet be handled?",
            {"generate": "Generate new wallet", "import": "Import existing private key"},
            "generate",
        )
        wallet_path = Path(WALLET_PATH)
        _prepare_wallet_path(wallet_path)
        wallet_address, wallet_private_key_hex = _handle_wallet(wallet_mode, wallet_path)

    env_values = dict(DEFAULTS)
    env_values["CHIPCOIN_NETWORK"] = network
    _apply_setup_mode(env_values, setup_mode, role)
    if role in {"node", "both"}:
        _configure_node_discovery(env_values, setup_mode=setup_mode)
        _configure_node_bootstrap(env_values, setup_mode=setup_mode)
    _preflight_validate(env_values, role=role)
    _prepare_runtime_files(env_values, role=role)
    if role in {"node", "both"}:
        bootstrap_notes = _prepare_node_bootstrap(env_values, network=network)

    _write_env(env_values)
    _print_success(
        role,
        network,
        runtime_mode,
        wallet_path,
        wallet_address,
        wallet_private_key_hex,
        setup_mode,
        env_values,
        bootstrap_notes=bootstrap_notes,
    )
    return 0


def _check_prerequisites() -> None:
    if shutil.which("docker") is None:
        _die("Docker is not installed or not available in PATH.")
    try:
        subprocess.run(["docker", "compose", "version"], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.CalledProcessError):
        _die("Docker Compose is not available.")
    if not ENV_EXAMPLE_PATH.exists():
        _die(f"Missing environment template: {ENV_EXAMPLE_PATH}")


def _ask_choice(prompt: str, options: dict[str, str], default: str) -> str:
    keys = list(options)
    while True:
        print(prompt)
        for key in keys:
            suffix = " (default)" if key == default else ""
            print(f"  - {key}: {options[key]}{suffix}")
        answer = input("> ").strip().lower()
        if not answer:
            return default
        if answer in options:
            return answer
        print("Invalid selection. Please choose one of the listed options.")


def _parse_peer_list(value: str) -> list[str]:
    peers: list[str] = []
    for candidate in re.split(r"[\s,]+", value.strip()):
        if not candidate:
            continue
        host, sep, port = candidate.rpartition(":")
        if not sep or not host or not port.isdigit():
            raise ValueError("Expected host:port entries separated by commas or spaces.")
        if candidate not in peers:
            peers.append(candidate)
    return peers


def _ask_direct_peers(prompt: str, default: str) -> str:
    while True:
        answer = input(f"{prompt} [{default}]: ").strip()
        if not answer:
            return default
        try:
            peers = _parse_peer_list(answer)
        except ValueError as exc:
            print(f"Invalid peer format. {exc}")
            continue
        if peers:
            return ",".join(peers)
        print("Enter at least one host:port entry or leave the field empty.")


def _ask_http_url(prompt: str, default: str) -> str:
    while True:
        answer = input(f"{prompt} [{default}]: ").strip()
        if not answer:
            return default
        if answer.startswith(("http://", "https://")):
            return answer
        print("Invalid URL. Expected http://host or https://host.")


def _ask_optional_http_urls(prompt: str, default: str) -> str:
    while True:
        answer = input(f"{prompt} [{default}]: ").strip()
        if not answer:
            return default
        try:
            urls = _parse_http_urls(answer)
        except ValueError as exc:
            print(f"Invalid URL list. {exc}")
            continue
        if urls:
            return ",".join(urls)
        print("Enter at least one http:// or https:// URL or leave the field empty.")


def _ask_host(prompt: str, default: str) -> str:
    while True:
        suffix = f" [{default}]" if default else ""
        answer = input(f"{prompt}{suffix}: ").strip()
        if not answer:
            if default:
                return default
            print("Host must not be empty.")
            continue
        if _looks_public_host(answer):
            return answer
        print("Invalid public host. Use a real public DNS name or public IP address, not localhost or a private address.")


def _ask_port(prompt: str, default: str) -> str:
    while True:
        answer = input(f"{prompt} [{default}]: ").strip()
        if not answer:
            return default
        if answer.isdigit() and 1 <= int(answer) <= 65535:
            return answer
        print("Invalid port. Expected an integer between 1 and 65535.")


def _looks_public_host(host: str) -> bool:
    candidate = host.strip()
    if not candidate or any(character.isspace() for character in candidate):
        return False
    lowered = candidate.lower()
    if lowered == "localhost" or lowered.endswith(".local"):
        return False
    try:
        address = ipaddress.ip_address(candidate)
    except ValueError:
        return True
    return address.is_global


def _ask_optional_peer(prompt: str, default: str) -> str:
    while True:
        answer = input(f"{prompt} [{default}]: ").strip()
        if not answer:
            return default
        host, sep, port = answer.rpartition(":")
        if sep and host and port.isdigit():
            return answer
        print("Invalid peer format. Expected host:port.")


def _parse_http_urls(value: str) -> list[str]:
    urls: list[str] = []
    for candidate in re.split(r"[\s,]+", value.strip()):
        if not candidate:
            continue
        if not candidate.startswith(("http://", "https://")):
            raise ValueError("expected http:// or https:// URLs separated by commas or spaces")
        normalized = candidate.rstrip("/")
        if normalized not in urls:
            urls.append(normalized)
    return urls


def _should_offer_runtime_sudo(path: Path) -> bool:
    return path.is_absolute() and Path("/var/lib") in path.parents


def _ensure_runtime_parent(path: Path, label: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        return
    except PermissionError as exc:
        if not _should_offer_runtime_sudo(path):
            _die(
                f"Cannot create {label.lower()} directory {path.parent}. "
                f"Original error: {exc}"
            )
        _prepare_runtime_with_sudo(path.parent, label, exc)


def _prepare_runtime_with_sudo(target_parent: Path, label: str, original_error: PermissionError) -> None:
    runtime_root = RUNTIME_ROOT
    print()
    print(
        f"{label} needs a writable runtime directory under {runtime_root}, "
        f"but the current user cannot create {target_parent}."
    )
    answer = input("Prepare the runtime directory with sudo now? [Y/n]: ").strip().lower()
    if answer in {"", "y", "yes"}:
        user = getpass.getuser()
        group = _primary_group_name()
        mkdir_args = [
            "sudo",
            "mkdir",
            "-p",
            str(runtime_root / "data"),
            str(runtime_root / "wallets"),
            str(runtime_root / "logs"),
        ]
        chown_args = ["sudo", "chown", "-R", f"{user}:{group}", str(runtime_root)]
        try:
            subprocess.run(mkdir_args, check=True)
            subprocess.run(chown_args, check=True)
            target_parent.mkdir(parents=True, exist_ok=True)
            return
        except (OSError, subprocess.CalledProcessError) as exc:
            _die(
                f"Failed to prepare runtime directory with sudo. "
                f"Tried: {' '.join(mkdir_args)} && {' '.join(chown_args)}. "
                f"Original error: {original_error}. Sudo step error: {exc}"
            )
    _die(
        f"Cannot continue without a writable runtime directory for {label.lower()}. "
        f"Prepare it manually with "
        f"'sudo mkdir -p {runtime_root}/data {runtime_root}/wallets {runtime_root}/logs "
        f"&& sudo chown -R $USER:$USER {runtime_root}'. Original error: {original_error}"
    )


def _primary_group_name() -> str:
    try:
        import grp

        return grp.getgrgid(os.getgid()).gr_name
    except Exception:  # noqa: BLE001
        return getpass.getuser()


def _apply_setup_mode(env_values: dict[str, str], setup_mode: str, role: str) -> None:
    miner_node_default = PUBLIC_DEVNET_NODE_ENDPOINT if role == "miner" else "http://node:8081"

    if setup_mode == "quick":
        env_values["DEFAULT_NODE_ENDPOINT"] = PUBLIC_DEVNET_NODE_ENDPOINT
        env_values["DEFAULT_BOOTSTRAP_PEER"] = PUBLIC_DEVNET_BOOTSTRAP_PEER
        env_values["DEFAULT_EXPLORER_URL"] = PUBLIC_DEVNET_EXPLORER_URL
        env_values["BROWSER_WALLET_DEFAULT_NODE_ENDPOINT"] = PUBLIC_DEVNET_NODE_ENDPOINT
        env_values["NODE_DIRECT_PEERS"] = ""
        env_values["NODE_DIRECT_PEER"] = ""
        env_values["NODE_BOOTSTRAP_URL"] = PUBLIC_DEVNET_BOOTSTRAP_URL
        env_values["MINING_NODE_URLS"] = miner_node_default
        env_values["DIRECT_PEERS"] = ""
        env_values["DIRECT_PEER"] = ""
        env_values["BOOTSTRAP_URL"] = ""
        return

    if setup_mode == "custom":
        node_endpoint = _ask_http_url("Enter node endpoint", PUBLIC_DEVNET_NODE_ENDPOINT)
        explorer_url = _ask_http_url("Enter explorer URL", PUBLIC_DEVNET_EXPLORER_URL)
        mining_node_urls = _ask_http_url(
            "Enter miner node endpoint",
            node_endpoint if role == "miner" else "http://node:8081",
        )
        env_values["DEFAULT_NODE_ENDPOINT"] = node_endpoint
        env_values["DEFAULT_BOOTSTRAP_PEER"] = PUBLIC_DEVNET_BOOTSTRAP_PEER
        env_values["DEFAULT_EXPLORER_URL"] = explorer_url
        env_values["BROWSER_WALLET_DEFAULT_NODE_ENDPOINT"] = node_endpoint
        env_values["NODE_DIRECT_PEERS"] = ""
        env_values["NODE_DIRECT_PEER"] = ""
        env_values["NODE_BOOTSTRAP_URL"] = PUBLIC_DEVNET_BOOTSTRAP_URL
        env_values["MINING_NODE_URLS"] = mining_node_urls
        env_values["DIRECT_PEERS"] = ""
        env_values["DIRECT_PEER"] = ""
        env_values["BOOTSTRAP_URL"] = ""
        return

    env_values["DEFAULT_NODE_ENDPOINT"] = "http://127.0.0.1:8081"
    env_values["DEFAULT_BOOTSTRAP_PEER"] = ""
    env_values["DEFAULT_EXPLORER_URL"] = ""
    env_values["BROWSER_WALLET_DEFAULT_NODE_ENDPOINT"] = "http://127.0.0.1:8081"
    env_values["NODE_DIRECT_PEERS"] = ""
    env_values["NODE_DIRECT_PEER"] = ""
    env_values["NODE_BOOTSTRAP_URL"] = ""
    env_values["MINING_NODE_URLS"] = "http://127.0.0.1:8081" if role == "miner" else "http://node:8081"
    env_values["DIRECT_PEERS"] = ""
    env_values["DIRECT_PEER"] = ""
    env_values["BOOTSTRAP_URL"] = ""


def _configure_node_discovery(env_values: dict[str, str], *, setup_mode: str) -> None:
    """Prompt for node peer discovery and optional bootstrap announce settings."""

    default_discovery = "isolated" if setup_mode == "local" else "bootstrap"
    discovery_mode = _ask_choice(
        "How should the node find its first peers?",
        {
            "bootstrap": "Bootstrap seed service",
            "manual": "Manual startup peer list",
            "isolated": "Start isolated",
        },
        default_discovery,
    )

    env_values["NODE_DIRECT_PEERS"] = ""
    env_values["NODE_DIRECT_PEER"] = ""
    env_values["NODE_BOOTSTRAP_URL"] = ""

    if discovery_mode == "bootstrap":
        env_values["NODE_BOOTSTRAP_URL"] = _ask_http_url(
            "Enter bootstrap seed URL",
            PUBLIC_DEVNET_BOOTSTRAP_URL,
        )
    elif discovery_mode == "manual":
        env_values["NODE_DIRECT_PEERS"] = _ask_direct_peers(
            "Enter startup peer(s)",
            PUBLIC_DEVNET_BOOTSTRAP_PEER,
        )

    publicly_reachable = _ask_choice(
        "Will this node accept inbound public P2P connections?",
        {
            "no": "No",
            "yes": "Yes, announce this node to the bootstrap seed",
        },
        "no" if setup_mode == "local" else "no",
    )

    env_values["BOOTSTRAP_ANNOUNCE_ENABLED"] = "false"
    env_values["NODE_PUBLIC_HOST"] = ""
    env_values["NODE_PUBLIC_P2P_PORT"] = env_values.get("NODE_P2P_BIND_PORT", "18444")

    if publicly_reachable == "yes":
        env_values["BOOTSTRAP_ANNOUNCE_ENABLED"] = "true"
        env_values["NODE_PUBLIC_HOST"] = _ask_host(
            "Enter the public P2P host for this node",
            env_values.get("NODE_PUBLIC_HOST", ""),
        )
        env_values["NODE_PUBLIC_P2P_PORT"] = _ask_port(
            "Enter the public P2P port for this node",
            env_values.get("NODE_P2P_BIND_PORT", "18444"),
        )


def _configure_node_bootstrap(env_values: dict[str, str], *, setup_mode: str) -> None:
    """Prompt for node bootstrap settings."""

    default_mode = "full" if setup_mode == "local" else "auto"
    bootstrap_mode = _ask_choice(
        "How should the node bootstrap?",
        {
            "full": "Full sync from genesis",
            "snapshot": "Snapshot bootstrap only",
            "auto": "Prefer snapshot, fall back to full sync",
        },
        default_mode,
    )
    env_values["NODE_BOOTSTRAP_MODE"] = bootstrap_mode
    if bootstrap_mode == "full":
        env_values["NODE_SNAPSHOT_MANIFEST_URLS"] = ""
        env_values["NODE_SNAPSHOT_TRUST_MODE"] = "off"
        env_values["NODE_SNAPSHOT_TRUSTED_KEYS_FILE"] = ""
        return

    default_manifest_urls = (
        PUBLIC_DEVNET_SNAPSHOT_MANIFEST_URL
        if setup_mode in {"quick", "custom"}
        else env_values.get("NODE_SNAPSHOT_MANIFEST_URLS", "")
    )
    env_values["NODE_SNAPSHOT_MANIFEST_URLS"] = _ask_optional_http_urls(
        "Enter snapshot manifest URL(s)",
        default_manifest_urls,
    )
    env_values["NODE_SNAPSHOT_TRUST_MODE"] = _ask_choice(
        "How should snapshot trust be handled?",
        {
            "off": "Do not verify signatures",
            "warn": "Warn on weak/untrusted snapshots but continue",
            "enforce": "Require a valid trusted snapshot signature",
        },
        "warn",
    )
    trusted_keys_file = input(
        f"Enter trusted snapshot signer keys file [{env_values['NODE_SNAPSHOT_TRUSTED_KEYS_FILE']}]: "
    ).strip()
    if trusted_keys_file:
        env_values["NODE_SNAPSHOT_TRUSTED_KEYS_FILE"] = trusted_keys_file


def _print_public_reachability_note() -> None:
    print()
    print("Public reachability note:")
    print("  - outbound-only nodes can still connect and sync")
    print("  - publicly reachable nodes are strongly preferred for network health")
    print("  - when possible, open and forward TCP 18444 for the node P2P listener")
    print("  - for clean installs, prefer multiple startup peers when available")
    print()


def _prepare_wallet_path(wallet_path: Path) -> None:
    _ensure_runtime_parent(wallet_path, "Wallet")
    if wallet_path.exists():
        overwrite = input(f"Wallet file already exists at {wallet_path}. Overwrite it? [y/N]: ").strip().lower()
        if overwrite not in {"y", "yes"}:
            _die("Setup aborted because the wallet file would be overwritten.")


def _handle_wallet(wallet_mode: str, wallet_path: Path) -> tuple[str, str]:
    if wallet_mode == "generate":
        wallet_key = generate_wallet_key()
    else:
        private_key_hex = getpass.getpass("Enter private key hex: ").strip()
        if not private_key_hex:
            _die("Private key must not be empty.")
        try:
            wallet_key = wallet_key_from_private_key(parse_private_key_hex(private_key_hex))
        except Exception as exc:  # noqa: BLE001
            _die(f"Invalid private key: {exc}")

    payload = {
        "private_key_hex": wallet_key.private_key.hex(),
        "public_key_hex": wallet_key.public_key.hex(),
        "address": wallet_key.address,
        "compressed": wallet_key.compressed,
    }
    wallet_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    try:
        os.chmod(wallet_path, 0o600)
    except OSError:
        pass
    return wallet_key.address, wallet_key.private_key.hex()


def _prepare_sqlite_file(path: Path, label: str) -> None:
    _ensure_runtime_parent(path, label)
    if path.exists() and path.is_dir():
        _die(f"{label} path points to a directory, but a writable SQLite file is required: {path}")
    try:
        path.touch(exist_ok=True)
    except PermissionError as exc:
        _die(
            f"{label} file is not writable: {path}. "
            "The runtime directory may need to be prepared or re-owned first. "
            f"Original error: {exc}"
        )
    if not path.is_file():
        _die(f"{label} path is not a regular file: {path}")
    if not os.access(path, os.W_OK):
        _die(f"{label} file is not writable: {path}")


def _reset_sqlite_file(path: Path, label: str) -> None:
    _ensure_runtime_parent(path, label)
    if path.exists():
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
    path.touch()


def _write_snapshot_metadata_file(node_data_path: Path, metadata: dict[str, object]) -> None:
    """Persist a small audit/debug record next to the node DB."""

    metadata_path = _snapshot_metadata_path(node_data_path)
    metadata_path.write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _remove_snapshot_metadata_file(node_data_path: Path) -> None:
    """Remove the snapshot audit/debug record when bootstrap is reset."""

    metadata_path = _snapshot_metadata_path(node_data_path)
    if metadata_path.exists() and metadata_path.is_file():
        metadata_path.unlink()


def _prepare_runtime_files(env_values: dict[str, str], *, role: str) -> None:
    if role in {"node", "both"}:
        _prepare_sqlite_file(Path(env_values["NODE_DATA_PATH"]), "Node data")


def _preflight_validate(env_values: dict[str, str], *, role: str) -> None:
    """Validate the generated install contract before writing .env."""

    if not COMPOSE_FILE_PATH.exists():
        _die(f"Expected Docker Compose file is missing: {COMPOSE_FILE_PATH}")
    if role not in {"node", "miner", "both"}:
        _die(f"Unsupported role selection: {role}")
    if role in {"node", "both"}:
        node_data_path = Path(env_values["NODE_DATA_PATH"])
        _ensure_runtime_parent(node_data_path, "Node data")
        announce_enabled = env_values.get("BOOTSTRAP_ANNOUNCE_ENABLED", "false")
        if announce_enabled not in {"true", "false", "1", "0"}:
            _die("BOOTSTRAP_ANNOUNCE_ENABLED must be true or false.")
        if announce_enabled in {"true", "1"}:
            bootstrap_url = env_values.get("NODE_BOOTSTRAP_URL", "").strip() or env_values.get("BOOTSTRAP_URL", "").strip()
            if not bootstrap_url:
                _die("Bootstrap announce requires NODE_BOOTSTRAP_URL or BOOTSTRAP_URL.")
            if not env_values.get("NODE_PUBLIC_HOST", "").strip():
                _die("Bootstrap announce requires NODE_PUBLIC_HOST.")
            if not _looks_public_host(env_values["NODE_PUBLIC_HOST"]):
                _die(f"NODE_PUBLIC_HOST is not a public host: {env_values['NODE_PUBLIC_HOST']}")
            public_port = env_values.get("NODE_PUBLIC_P2P_PORT", "").strip()
            if not public_port.isdigit() or not (1 <= int(public_port) <= 65535):
                _die(f"NODE_PUBLIC_P2P_PORT must be a valid TCP port: {public_port}")
        bootstrap_mode = env_values.get("NODE_BOOTSTRAP_MODE", "full")
        if bootstrap_mode not in {"full", "snapshot", "auto"}:
            _die(f"Unsupported node bootstrap mode: {bootstrap_mode}")
        if bootstrap_mode in {"snapshot", "auto"}:
            try:
                _parse_http_urls(env_values.get("NODE_SNAPSHOT_MANIFEST_URLS", ""))
            except ValueError as exc:
                _die(f"Snapshot manifest URL list is invalid: {exc}")
            snapshot_path = Path(env_values["NODE_SNAPSHOT_FILE"])
            _ensure_runtime_parent(snapshot_path, "Snapshot cache")
            trusted_keys_file = env_values.get("NODE_SNAPSHOT_TRUSTED_KEYS_FILE", "").strip()
            trust_mode = env_values.get("NODE_SNAPSHOT_TRUST_MODE", "off")
            if trust_mode == "enforce" and not trusted_keys_file:
                _die("Snapshot trust mode 'enforce' requires NODE_SNAPSHOT_TRUSTED_KEYS_FILE.")
            if trusted_keys_file and not Path(trusted_keys_file).exists():
                _die(f"Snapshot trusted keys file does not exist: {trusted_keys_file}")
    if role in {"miner", "both"} and not env_values.get("MINING_NODE_URLS", "").strip():
        _die("Miner mode requires at least one mining node endpoint.")


def _prepare_node_bootstrap(env_values: dict[str, str], *, network: str) -> list[str]:
    """Prepare full-sync or snapshot bootstrap assets for the node."""

    notes: list[str] = []
    bootstrap_mode = env_values.get("NODE_BOOTSTRAP_MODE", "full")
    if bootstrap_mode == "full":
        notes.append("Node bootstrap mode: full sync from genesis.")
        return notes

    manifest_urls_raw = env_values.get("NODE_SNAPSHOT_MANIFEST_URLS", "")
    try:
        manifest_urls = _parse_http_urls(manifest_urls_raw)
    except ValueError as exc:
        if bootstrap_mode == "auto":
            env_values["NODE_BOOTSTRAP_MODE"] = "full"
            notes.append(f"Snapshot bootstrap skipped: {exc}. Falling back to full sync.")
            return notes
        _die(f"Snapshot bootstrap configuration is invalid: {exc}")

    if not manifest_urls:
        if bootstrap_mode == "auto":
            env_values["NODE_BOOTSTRAP_MODE"] = "full"
            notes.append("Snapshot bootstrap skipped: no manifest URLs configured. Falling back to full sync.")
            return notes
        _die("Snapshot bootstrap requires at least one manifest URL.")

    try:
        trusted_keys = _load_snapshot_trusted_keys(
            [],
            [env_values["NODE_SNAPSHOT_TRUSTED_KEYS_FILE"]] if env_values.get("NODE_SNAPSHOT_TRUSTED_KEYS_FILE") else [],
        )
        manifest_entry = _select_latest_compatible_snapshot(manifest_urls, network=network)
        notes.append(f"Manifest source used: {manifest_entry.manifest_url}")
        notes.extend(_snapshot_entry_warnings(manifest_entry))
        snapshot_path = Path(env_values["NODE_SNAPSHOT_FILE"])
        _ensure_runtime_parent(snapshot_path, "Snapshot cache")
        _download_snapshot_file(manifest_entry.snapshot_url, snapshot_path)
        if _file_sha256(snapshot_path) != manifest_entry.checksum_sha256:
            raise ValueError("downloaded snapshot file checksum does not match the selected manifest entry")
        node_data_path = Path(env_values["NODE_DATA_PATH"])
        _reset_sqlite_file(node_data_path, "Node data")
        service = NodeService.open_sqlite(node_data_path, network=network)
        metadata = service.import_snapshot_file(
            snapshot_path,
            reset_existing=True,
            trust_mode=env_values.get("NODE_SNAPSHOT_TRUST_MODE", "off"),
            trusted_keys=trusted_keys,
        )
        warnings = metadata.get("warnings", [])
        if isinstance(warnings, list):
            for warning in warnings:
                notes.append(
                    f"WARNING {warning}; snapshot import continued only because trust mode is "
                    f"{env_values.get('NODE_SNAPSHOT_TRUST_MODE', 'off')}."
                )
        _write_snapshot_metadata_file(
            node_data_path,
            {
                "bootstrap_mode": "snapshot",
                "manifest_url": manifest_entry.manifest_url,
                "snapshot_url": manifest_entry.snapshot_url,
                "snapshot_height": manifest_entry.snapshot_height,
                "snapshot_block_hash": manifest_entry.snapshot_block_hash,
                "snapshot_file": str(snapshot_path),
                "node_data_path": str(node_data_path),
                "snapshot_trust_mode": env_values.get("NODE_SNAPSHOT_TRUST_MODE", "off"),
                "accepted_snapshot_signer_pubkeys": metadata.get("accepted_signer_pubkeys", []),
                "warnings": metadata.get("warnings", []),
            },
        )
        env_values["NODE_BOOTSTRAP_MODE"] = "snapshot"
        env_values["NODE_SNAPSHOT_SELECTED_URL"] = manifest_entry.snapshot_url
        env_values["NODE_SNAPSHOT_SELECTED_HEIGHT"] = str(manifest_entry.snapshot_height)
        env_values["NODE_SNAPSHOT_SELECTED_HASH"] = manifest_entry.snapshot_block_hash
        notes.append(
            f"Snapshot selected: {manifest_entry.snapshot_url} "
            f"(height {manifest_entry.snapshot_height} hash {manifest_entry.snapshot_block_hash})."
        )
        notes.append(f"Node database prepared at {env_values['NODE_DATA_PATH']}.")
        return notes
    except Exception as exc:  # noqa: BLE001
        _reset_sqlite_file(Path(env_values["NODE_DATA_PATH"]), "Node data")
        _remove_snapshot_metadata_file(Path(env_values["NODE_DATA_PATH"]))
        snapshot_file = Path(env_values["NODE_SNAPSHOT_FILE"])
        if snapshot_file.exists() and snapshot_file.is_file():
            snapshot_file.unlink()
        if bootstrap_mode == "auto":
            env_values["NODE_BOOTSTRAP_MODE"] = "full"
            env_values["NODE_SNAPSHOT_SELECTED_URL"] = ""
            env_values["NODE_SNAPSHOT_SELECTED_HEIGHT"] = ""
            env_values["NODE_SNAPSHOT_SELECTED_HASH"] = ""
            notes.append(f"Snapshot bootstrap failed: {exc}. Falling back to full sync.")
            return notes
        _die(f"Snapshot bootstrap failed: {exc}")


def _snapshot_entry_warnings(entry: SnapshotManifestEntry) -> list[str]:
    warnings: list[str] = []
    age_seconds = max(0, int(time.time()) - entry.created_at)
    if age_seconds >= SNAPSHOT_STALE_WARNING_SECONDS:
        warnings.append(
            f"WARNING selected snapshot is {age_seconds} seconds old but still valid."
        )
    if age_seconds >= SNAPSHOT_LARGE_DELTA_WARNING_SECONDS:
        warnings.append(
            "WARNING selected snapshot is old enough that the node may need a large post-anchor delta sync."
        )
    return warnings


def _fetch_json_from_url(url: str) -> object:
    with request.urlopen(url, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


def _download_snapshot_file(url: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with request.urlopen(url, timeout=60) as response:
        destination.write_bytes(response.read())


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _parse_snapshot_manifest(payload: object, *, manifest_url: str) -> list[SnapshotManifestEntry]:
    if isinstance(payload, dict):
        raw_entries = payload.get("snapshots", [])
    elif isinstance(payload, list):
        raw_entries = payload
    else:
        raise ValueError(f"Unsupported snapshot manifest format from {manifest_url}")
    if not isinstance(raw_entries, list):
        raise ValueError(f"Snapshot manifest entries must be a list: {manifest_url}")

    entries: list[SnapshotManifestEntry] = []
    for raw_entry in raw_entries:
        if not isinstance(raw_entry, dict):
            raise ValueError(f"Snapshot manifest entry must be an object: {manifest_url}")
        raw_signers = raw_entry.get("signer_pubkeys", [])
        if isinstance(raw_signers, list):
            signer_pubkeys = tuple(str(value) for value in raw_signers if str(value))
        else:
            signer_pubkeys = ()
        checksum = raw_entry.get("checksum_sha256", raw_entry.get("checksum"))
        entries.append(
            SnapshotManifestEntry(
                manifest_url=manifest_url,
                network=str(raw_entry["network"]),
                snapshot_url=str(raw_entry["snapshot_url"]),
                format_version=int(raw_entry["format_version"]),
                snapshot_height=int(raw_entry["snapshot_height"]),
                snapshot_block_hash=str(raw_entry["snapshot_block_hash"]),
                created_at=int(raw_entry["created_at"]),
                checksum_sha256=str(checksum),
                signer_pubkeys=signer_pubkeys,
            )
        )
    return entries


def _select_latest_compatible_snapshot(manifest_urls: list[str], *, network: str) -> SnapshotManifestEntry:
    errors: list[str] = []
    for manifest_url in manifest_urls:
        try:
            payload = _fetch_json_from_url(manifest_url)
            entries = _parse_snapshot_manifest(payload, manifest_url=manifest_url)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{manifest_url}: {exc}")
            continue
        compatible = [
            entry
            for entry in entries
            if entry.network == network and entry.format_version in {1, 2}
        ]
        if compatible:
            compatible.sort(key=lambda entry: (entry.snapshot_height, entry.created_at, entry.format_version), reverse=True)
            return compatible[0]
        errors.append(f"{manifest_url}: no compatible snapshot entries were found")
    details = "; ".join(errors) if errors else "no compatible snapshot entries were found"
    raise ValueError(f"snapshot source unavailable or incompatible: {details}")


def _write_env(values: dict[str, str]) -> None:
    if ENV_PATH.exists():
        overwrite = input(f"{ENV_PATH} already exists. Overwrite it? [y/N]: ").strip().lower()
        if overwrite not in {"y", "yes"}:
            _die("Setup aborted because .env would be overwritten.")
    lines = [f"{key}={values[key]}" for key in DEFAULTS]
    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _print_success(
    role: str,
    network: str,
    runtime_mode: str,
    wallet_path: Path | None,
    wallet_address: str | None,
    wallet_private_key_hex: str | None,
    setup_mode: str,
    env_values: dict[str, str],
    bootstrap_notes: list[str],
) -> None:
    command_suffix = {"node": "node", "miner": "miner", "both": "node miner"}[role]
    compose_up_background = f"docker compose up -d {command_suffix}".strip()

    print()
    print("Setup completed successfully.")
    print(f"Role: {role}")
    print(f"Network: {network}")
    if wallet_path is not None and wallet_address is not None:
        print(f"Wallet file: {wallet_path}")
        print(f"Wallet address: {wallet_address}")
        if wallet_private_key_hex is not None:
            print(f"Wallet private key: {wallet_private_key_hex}")
    else:
        print("Wallet: not required for node-only Phase 1 runtime")
        print("Note: node wallet support is reserved for future real node reward participation flows.")
    print(f"Runtime directory: {env_values['CHIPCOIN_RUNTIME_DIR']}")
    if role in {"node", "both"}:
        print(f"Node database: {env_values['NODE_DATA_PATH']}")
        print(f"Snapshot metadata file: {_snapshot_metadata_path(Path(env_values['NODE_DATA_PATH']))}")
        if env_values["NODE_BOOTSTRAP_URL"]:
            print(f"Node bootstrap seed URL: {env_values['NODE_BOOTSTRAP_URL']}")
        else:
            print("Node bootstrap seed URL: none")
        print(f"Bootstrap announce enabled: {env_values.get('BOOTSTRAP_ANNOUNCE_ENABLED', 'false')}")
        if env_values.get("BOOTSTRAP_ANNOUNCE_ENABLED") == "true":
            print(f"Announced public host: {env_values['NODE_PUBLIC_HOST']}")
            print(f"Announced public P2P port: {env_values['NODE_PUBLIC_P2P_PORT']}")
    print(f"Setup mode: {setup_mode}")
    print(f"Default node endpoint: {env_values['DEFAULT_NODE_ENDPOINT']}")
    if role in {"node", "both"}:
        print(f"Node bootstrap mode: {env_values['NODE_BOOTSTRAP_MODE']}")
        if env_values["NODE_BOOTSTRAP_MODE"] == "snapshot":
            print(f"Snapshot manifest URL(s): {env_values['NODE_SNAPSHOT_MANIFEST_URLS']}")
            print(f"Snapshot cache file: {env_values['NODE_SNAPSHOT_FILE']}")
            print(f"Snapshot trust mode: {env_values['NODE_SNAPSHOT_TRUST_MODE']}")
            if env_values["NODE_SNAPSHOT_SELECTED_URL"]:
                print(f"Selected snapshot URL: {env_values['NODE_SNAPSHOT_SELECTED_URL']}")
                print(f"Selected snapshot height: {env_values['NODE_SNAPSHOT_SELECTED_HEIGHT']}")
                print(f"Selected snapshot hash: {env_values['NODE_SNAPSHOT_SELECTED_HASH']}")
    if role in {"miner", "both"}:
        print(f"Miner node endpoint(s): {env_values['MINING_NODE_URLS']}")
    if env_values["DEFAULT_BOOTSTRAP_PEER"]:
        print(f"Default bootstrap peer: {env_values['DEFAULT_BOOTSTRAP_PEER']}")
    else:
        print("Default bootstrap peer: none")
    if env_values["DIRECT_PEERS"]:
        print(f"Startup peers: {env_values['DIRECT_PEERS']}")
    elif env_values["DIRECT_PEER"]:
        print(f"Startup peer (legacy): {env_values['DIRECT_PEER']}")
    else:
        print("Startup peers: none")
    if env_values["DEFAULT_EXPLORER_URL"]:
        print(f"Default explorer URL: {env_values['DEFAULT_EXPLORER_URL']}")
    else:
        print("Default explorer URL: none")
    if bootstrap_notes:
        print()
        print("Bootstrap notes:")
        for note in bootstrap_notes:
            print(f"  - {note}")
    print()
    print("Next commands:")
    print(f"  {compose_up_background}")
    if role in {"node", "both"}:
        print("  docker compose logs -f node")
    if role in {"miner", "both"}:
        print("  docker compose logs -f miner")
    print("  docker compose ps")
    print("  docker compose down")
    print()
    print("What to inspect:")
    if role in {"node", "both"} and env_values["NODE_BOOTSTRAP_MODE"] == "full":
        print("  - On first start, the node should log bootstrap_mode=full and begin syncing from genesis.")
    elif role in {"node", "both"}:
        print("  - On first start, the node should log bootstrap_mode=snapshot and start near the snapshot anchor height.")
        print("  - If the remote tip is higher than the anchor, only the post-anchor delta should sync.")


def _die(message: str) -> None:
    print(f"ERROR {message}", file=sys.stderr)
    raise SystemExit(1)


if __name__ == "__main__":
    raise SystemExit(main())
