"""Shared configuration entrypoints for node and service bootstrap."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .consensus.params import ConsensusParams, DEVNET_PARAMS, MAINNET_PARAMS


@dataclass(frozen=True)
class NetworkConfig:
    """Runtime configuration for a specific Chipcoin network."""

    name: str
    magic: bytes
    default_p2p_port: int
    data_dir: Path
    default_data_file: str
    params: ConsensusParams
    bootstrap_seeds: tuple[str, ...] = ()


DEFAULT_NETWORK = "mainnet"

MAINNET_CONFIG = NetworkConfig(
    name="mainnet",
    magic=bytes.fromhex("F9BEB4D9"),
    default_p2p_port=8333,
    data_dir=Path("."),
    default_data_file="chipcoin.sqlite3",
    params=MAINNET_PARAMS,
)

DEVNET_CONFIG = NetworkConfig(
    name="devnet",
    magic=bytes.fromhex("FAC3B6DA"),
    default_p2p_port=18444,
    data_dir=Path("."),
    default_data_file="chipcoin-devnet.sqlite3",
    params=DEVNET_PARAMS,
)

NETWORK_CONFIGS = {
    MAINNET_CONFIG.name: MAINNET_CONFIG,
    DEVNET_CONFIG.name: DEVNET_CONFIG,
}


def get_network_config(name: str) -> NetworkConfig:
    """Return the runtime config for a named network."""

    try:
        return NETWORK_CONFIGS[name]
    except KeyError as exc:
        raise ValueError(f"Unsupported network: {name}") from exc


def resolve_data_path(raw_path: str | Path, network: str) -> Path:
    """Return a network-aware SQLite path while preserving explicit paths."""

    path = raw_path if isinstance(raw_path, Path) else Path(raw_path)
    config = get_network_config(network)
    if path.name == MAINNET_CONFIG.default_data_file and network != DEFAULT_NETWORK:
        return path.with_name(config.default_data_file)
    return path
