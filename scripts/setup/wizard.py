#!/usr/bin/env python3
"""Phase 1 setup wizard for the public Chipcoin runtime."""

from __future__ import annotations

import getpass
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from chipcoin.crypto.keys import parse_private_key_hex  # noqa: E402
from chipcoin.wallet.signer import generate_wallet_key, wallet_key_from_private_key  # noqa: E402


ENV_PATH = REPO_ROOT / ".env"
ENV_EXAMPLE_PATH = REPO_ROOT / "config" / "env" / ".env.example"
RUNTIME_ROOT = Path.home() / "Chipcoin-runtime"
NODE_DATA_PATH = str(RUNTIME_ROOT / "data" / "node-devnet.sqlite3")
MINER_DATA_PATH = str(RUNTIME_ROOT / "data" / "miner-devnet.sqlite3")
WALLET_PATH = str(RUNTIME_ROOT / "wallets" / "chipcoin-wallet.json")
DEFAULTS = {
    "CHIPCOIN_NETWORK": "devnet",
    "COMPOSE_PROJECT_NAME": "chipcoin",
    "CHIPCOIN_RUNTIME_DIR": str(RUNTIME_ROOT),
    "NODE_DATA_PATH": NODE_DATA_PATH,
    "NODE_LOG_LEVEL": "INFO",
    "NODE_P2P_BIND_PORT": "18444",
    "NODE_HTTP_BIND_PORT": "8081",
    "CHIPCOIN_HTTP_ALLOWED_ORIGINS": "",
    "MINER_DATA_PATH": MINER_DATA_PATH,
    "MINER_LOG_LEVEL": "INFO",
    "MINER_WALLET_FILE": WALLET_PATH,
    "MINER_P2P_BIND_PORT": "18445",
    "MINING_MIN_INTERVAL_SECONDS": "1.0",
    "EXPLORER_PORT": "4173",
    "EXPLORER_DIST_PATH": "./apps/explorer/dist",
    "BROWSER_WALLET_DEFAULT_NODE_ENDPOINT": "http://127.0.0.1:8081",
    "DIRECT_PEER": "",
    "BOOTSTRAP_URL": "",
}


def main() -> int:
    print("Chipcoin Phase 1 Setup Wizard")
    _check_prerequisites()
    role = _ask_choice("What do you want to run?", {"node": "Node", "miner": "Miner", "both": "Both"}, "both")
    network = _ask_choice("Which network do you want to use?", {"devnet": "Devnet"}, "devnet")
    runtime_mode = _ask_choice("How should services run?", {"foreground": "Foreground", "background": "Background"}, "foreground")
    connectivity_mode = _ask_choice(
        "How should peer discovery work?",
        {"direct": "Use direct peer", "bootstrap": "Use bootstrap", "isolated": "Start isolated"},
        "isolated",
    )

    direct_peer = ""
    bootstrap_url = ""
    if connectivity_mode == "direct":
        direct_peer = _ask_direct_peer()
    elif connectivity_mode == "bootstrap":
        bootstrap_url = _ask_bootstrap_url()

    wallet_path: Path | None = None
    wallet_address: str | None = None
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
        wallet_address = _handle_wallet(wallet_mode, wallet_path)

    env_values = dict(DEFAULTS)
    env_values["CHIPCOIN_NETWORK"] = network
    env_values["DIRECT_PEER"] = direct_peer
    env_values["BOOTSTRAP_URL"] = bootstrap_url
    _prepare_runtime_files(env_values)

    _write_env(env_values)
    _print_success(role, network, runtime_mode, wallet_path, wallet_address, direct_peer, bootstrap_url)
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


def _ask_direct_peer() -> str:
    while True:
        answer = input("Enter the direct peer as host:port: ").strip()
        host, sep, port = answer.rpartition(":")
        if sep and host and port.isdigit():
            return answer
        print("Invalid peer format. Expected host:port.")


def _ask_bootstrap_url() -> str:
    while True:
        answer = input("Enter the bootstrap URL: ").strip()
        parsed = urlparse(answer)
        if parsed.scheme in {"http", "https"} and parsed.netloc:
            return answer
        print("Invalid bootstrap URL. Expected http://host or https://host.")


def _prepare_wallet_path(wallet_path: Path) -> None:
    wallet_path.parent.mkdir(parents=True, exist_ok=True)
    if wallet_path.exists():
        overwrite = input(f"Wallet file already exists at {wallet_path}. Overwrite it? [y/N]: ").strip().lower()
        if overwrite not in {"y", "yes"}:
            _die("Setup aborted because the wallet file would be overwritten.")


def _handle_wallet(wallet_mode: str, wallet_path: Path) -> str:
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
    return wallet_key.address


def _prepare_runtime_files(env_values: dict[str, str]) -> None:
    for configured_path in (env_values["NODE_DATA_PATH"], env_values["MINER_DATA_PATH"]):
        path = Path(configured_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch(exist_ok=True)


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
    direct_peer: str,
    bootstrap_url: str,
) -> None:
    command_suffix = {
        "node": "node",
        "miner": "miner",
        "both": "node miner",
    }[role]
    compose_up = f"docker compose up {'-d ' if runtime_mode == 'background' else ''}{command_suffix}".replace("  ", " ").strip()

    print()
    print("Setup completed successfully.")
    print(f"Role: {role}")
    print(f"Network: {network}")
    if wallet_path is not None and wallet_address is not None:
        print(f"Wallet file: {wallet_path}")
        print(f"Wallet address: {wallet_address}")
    else:
        print("Wallet: not required for node-only Phase 1 runtime")
        print("Note: node wallet support is reserved for future real node reward participation flows.")
    print(f"Runtime directory: {DEFAULTS['CHIPCOIN_RUNTIME_DIR']}")
    if direct_peer:
        print(f"Discovery: direct peer ({direct_peer})")
    elif bootstrap_url:
        print(f"Discovery: bootstrap ({bootstrap_url})")
    else:
        print("Discovery: isolated")
    print()
    print("Next commands:")
    print(f"  {compose_up}")
    print("  docker compose logs -f")
    print("  docker compose ps")
    print("  docker compose down")


def _die(message: str) -> None:
    print(f"ERROR {message}", file=sys.stderr)
    raise SystemExit(1)


if __name__ == "__main__":
    raise SystemExit(main())
