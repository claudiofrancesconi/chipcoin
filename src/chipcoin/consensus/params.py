"""Consensus parameters and economic constants."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ConsensusParams:
    """Static parameters that define network consensus behavior."""

    coinbase_maturity: int
    halving_interval: int
    initial_miner_subsidy_chipbits: int
    node_reward_ratio_numerator: int
    node_reward_ratio_denominator: int
    max_money_chipbits: int
    target_block_time_seconds: int
    difficulty_adjustment_window: int
    genesis_bits: int
    max_block_weight: int
    max_block_sigops: int
    epoch_length_blocks: int
    max_rewarded_nodes_per_block: int
    register_node_fee_chipbits: int
    renew_node_fee_chipbits: int


MAINNET_PARAMS = ConsensusParams(
    coinbase_maturity=100,
    halving_interval=250_000,
    initial_miner_subsidy_chipbits=20 * 100_000_000,
    node_reward_ratio_numerator=1,
    node_reward_ratio_denominator=10,
    max_money_chipbits=11_000_000 * 100_000_000,
    target_block_time_seconds=120,
    difficulty_adjustment_window=1000,
    genesis_bits=0x207FFFFF,
    max_block_weight=4_000_000,
    max_block_sigops=80_000,
    epoch_length_blocks=1000,
    max_rewarded_nodes_per_block=10,
    register_node_fee_chipbits=0,
    renew_node_fee_chipbits=0,
)


DEVNET_PARAMS = ConsensusParams(
    coinbase_maturity=10,
    halving_interval=250_000,
    initial_miner_subsidy_chipbits=20 * 100_000_000,
    node_reward_ratio_numerator=1,
    node_reward_ratio_denominator=10,
    max_money_chipbits=11_000_000 * 100_000_000,
    target_block_time_seconds=30,
    difficulty_adjustment_window=200,
    genesis_bits=0x1F0FFFFF,
    max_block_weight=4_000_000,
    max_block_sigops=80_000,
    epoch_length_blocks=1000,
    max_rewarded_nodes_per_block=10,
    register_node_fee_chipbits=0,
    renew_node_fee_chipbits=0,
)
