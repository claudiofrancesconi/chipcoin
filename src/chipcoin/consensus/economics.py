"""Monetary policy and reward schedule helpers."""

from __future__ import annotations

from .params import ConsensusParams


CHCBITS_PER_CHC = 100_000_000


def _regular_miner_subsidy_chipbits(height: int, params: ConsensusParams) -> int:
    """Return the ordinary halving-based miner subsidy in chipbits."""

    if height < 0:
        raise ValueError("Block height cannot be negative.")

    halvings = height // params.halving_interval
    subsidy_chipbits = params.initial_miner_subsidy_chipbits >> halvings
    return max(subsidy_chipbits, 0)


def _regular_node_reward_pool_chipbits(height: int, params: ConsensusParams) -> int:
    """Return the ordinary halving-based node reward pool in chipbits."""

    subsidy_chipbits = _regular_miner_subsidy_chipbits(height, params)
    return (subsidy_chipbits * params.node_reward_ratio_numerator) // params.node_reward_ratio_denominator


def terminal_correction_height(params: ConsensusParams) -> int:
    """Return the one block height that mints the explicit terminal correction."""

    return params.halving_interval * params.initial_miner_subsidy_chipbits.bit_length()


def terminal_correction_chipbits(params: ConsensusParams) -> int:
    """Return the explicit final-tail correction needed to land exactly on max supply."""

    total_regular_subsidy = 0
    height = 0
    while True:
        miner_subsidy = _regular_miner_subsidy_chipbits(height, params)
        if miner_subsidy <= 0:
            break
        node_pool = _regular_node_reward_pool_chipbits(height, params)
        total_regular_subsidy += (miner_subsidy + node_pool) * params.halving_interval
        height += params.halving_interval
    return max(0, params.max_money_chipbits - total_regular_subsidy)


def subsidy_split_chipbits(height: int, params: ConsensusParams) -> tuple[int, int]:
    """Return the exact miner/node subsidy split for one block height."""

    miner_subsidy = _regular_miner_subsidy_chipbits(height, params)
    if miner_subsidy > 0:
        return miner_subsidy, _regular_node_reward_pool_chipbits(height, params)
    if height == terminal_correction_height(params):
        return terminal_correction_chipbits(params), 0
    return 0, 0


def miner_subsidy_chipbits(height: int, params: ConsensusParams) -> int:
    """Return the miner base subsidy in chipbits for a given height."""

    return subsidy_split_chipbits(height, params)[0]


def node_reward_pool_chipbits(height: int, params: ConsensusParams) -> int:
    """Return the node reward pool in chipbits for a given height."""

    return subsidy_split_chipbits(height, params)[1]


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
    current_height = 0
    while current_height <= height:
        miner_subsidy = _regular_miner_subsidy_chipbits(current_height, params)
        if miner_subsidy <= 0:
            break
        node_pool = _regular_node_reward_pool_chipbits(current_height, params)
        epoch_end = min(height, current_height + params.halving_interval - 1)
        block_count = epoch_end - current_height + 1
        total += (miner_subsidy + node_pool) * block_count
        current_height += params.halving_interval

    correction_height = terminal_correction_height(params)
    if height >= correction_height:
        total += terminal_correction_chipbits(params)
    return total
