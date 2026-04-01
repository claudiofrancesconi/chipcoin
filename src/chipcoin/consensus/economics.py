"""Monetary policy and reward schedule helpers."""

from __future__ import annotations

from .params import ConsensusParams


CHCBITS_PER_CHC = 100_000_000


def miner_subsidy_chipbits(height: int, params: ConsensusParams) -> int:
    """Return the miner base subsidy in chipbits for a given height."""

    if height < 0:
        raise ValueError("Block height cannot be negative.")

    halvings = height // params.halving_interval
    subsidy_chipbits = params.initial_miner_subsidy_chipbits >> halvings
    return max(subsidy_chipbits, 0)


def node_reward_pool_chipbits(height: int, params: ConsensusParams) -> int:
    """Return the node reward pool in chipbits for a given height."""

    subsidy_chipbits = miner_subsidy_chipbits(height, params)
    return (subsidy_chipbits * params.node_reward_ratio_numerator) // params.node_reward_ratio_denominator


def total_block_subsidy_chipbits(height: int, params: ConsensusParams) -> int:
    """Return total subsidy minted by one block in chipbits."""

    return miner_subsidy_chipbits(height, params) + node_reward_pool_chipbits(height, params)


def block_subsidy(height: int, params: ConsensusParams) -> int:
    """Backward-compatible alias for total per-block subsidy in chipbits."""

    return total_block_subsidy_chipbits(height, params)


def total_subsidy_through_height(height: int, params: ConsensusParams) -> int:
    """Return the total minted subsidy from height zero through the given height."""

    if height < 0:
        return 0

    total = 0
    for current_height in range(height + 1):
        total += total_block_subsidy_chipbits(current_height, params)
    return min(total, params.max_money_chipbits)
