"""Consensus parameters and economic constants."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ConsensusParams:
    """Static parameters that define network consensus behavior."""

    coinbase_maturity: int
    halving_interval: int
    initial_miner_subsidy_chipbits: int
    initial_node_epoch_reward_chipbits: int
    max_money_chipbits: int
    target_block_time_seconds: int
    difficulty_adjustment_window: int
    genesis_bits: int
    max_block_weight: int
    max_block_sigops: int
    epoch_length_blocks: int
    register_node_fee_chipbits: int
    renew_node_fee_chipbits: int
    node_reward_activation_height: int
    reward_node_warmup_epochs: int
    reward_check_windows_per_epoch: int
    reward_target_checks_per_epoch: int
    reward_min_passed_checks_per_epoch: int
    reward_verifier_committee_size: int
    reward_verifier_quorum: int
    reward_final_confirmation_window_blocks: int
    reward_sync_lag_tolerance_blocks: int
    max_rewarded_nodes_per_epoch: int
    max_attestation_bundles_per_block: int
    max_attestations_per_bundle: int
    max_attestations_per_verifier_per_window: int
    target_block_time_activation_height: int = 0
    legacy_target_block_time_seconds: int | None = None
    pq_support_activation_height: int | None = None
    reward_node_fee_recent_driver_activation_height: int = 0


def target_block_time_seconds_for_height(height: int, params: ConsensusParams) -> int:
    """Return the block-time target that applies to one candidate height."""

    if (
        params.legacy_target_block_time_seconds is not None
        and height < params.target_block_time_activation_height
    ):
        return params.legacy_target_block_time_seconds
    return params.target_block_time_seconds


MAINNET_PARAMS = ConsensusParams(
    coinbase_maturity=100,
    halving_interval=111_000,
    initial_miner_subsidy_chipbits=50 * 100_000_000,
    initial_node_epoch_reward_chipbits=50 * 100_000_000,
    max_money_chipbits=11_000_000 * 100_000_000,
    target_block_time_seconds=600,
    difficulty_adjustment_window=1000,
    genesis_bits=0x207FFFFF,
    max_block_weight=4_000_000,
    max_block_sigops=80_000,
    epoch_length_blocks=100,
    register_node_fee_chipbits=100_000_000,
    renew_node_fee_chipbits=10_000_000,
    node_reward_activation_height=1_000,
    reward_node_warmup_epochs=2,
    reward_check_windows_per_epoch=10,
    reward_target_checks_per_epoch=3,
    reward_min_passed_checks_per_epoch=2,
    reward_verifier_committee_size=3,
    reward_verifier_quorum=2,
    reward_final_confirmation_window_blocks=10,
    reward_sync_lag_tolerance_blocks=5,
    max_rewarded_nodes_per_epoch=25,
    max_attestation_bundles_per_block=4,
    max_attestations_per_bundle=24,
    max_attestations_per_verifier_per_window=1,
)


DEVNET_PARAMS = ConsensusParams(
    coinbase_maturity=10,
    halving_interval=111_000,
    initial_miner_subsidy_chipbits=50 * 100_000_000,
    initial_node_epoch_reward_chipbits=50 * 100_000_000,
    max_money_chipbits=11_000_000 * 100_000_000,
    target_block_time_seconds=300,
    difficulty_adjustment_window=200,
    genesis_bits=0x1F0FFFFF,
    max_block_weight=4_000_000,
    max_block_sigops=80_000,
    epoch_length_blocks=100,
    register_node_fee_chipbits=100_000_000,
    renew_node_fee_chipbits=10_000_000,
    node_reward_activation_height=300,
    reward_node_warmup_epochs=2,
    reward_check_windows_per_epoch=10,
    reward_target_checks_per_epoch=3,
    reward_min_passed_checks_per_epoch=2,
    reward_verifier_committee_size=3,
    reward_verifier_quorum=2,
    reward_final_confirmation_window_blocks=10,
    reward_sync_lag_tolerance_blocks=5,
    max_rewarded_nodes_per_epoch=10,
    max_attestation_bundles_per_block=4,
    max_attestations_per_bundle=24,
    max_attestations_per_verifier_per_window=1,
)


TESTNET_PARAMS = ConsensusParams(
    coinbase_maturity=20,
    halving_interval=111_000,
    initial_miner_subsidy_chipbits=50 * 100_000_000,
    initial_node_epoch_reward_chipbits=50 * 100_000_000,
    max_money_chipbits=11_000_000 * 100_000_000,
    target_block_time_seconds=600,
    difficulty_adjustment_window=500,
    genesis_bits=0x1F07FFFF,
    max_block_weight=4_000_000,
    max_block_sigops=80_000,
    epoch_length_blocks=100,
    register_node_fee_chipbits=100_000_000,
    renew_node_fee_chipbits=10_000_000,
    node_reward_activation_height=600,
    reward_node_warmup_epochs=2,
    reward_check_windows_per_epoch=10,
    reward_target_checks_per_epoch=3,
    reward_min_passed_checks_per_epoch=2,
    reward_verifier_committee_size=3,
    reward_verifier_quorum=2,
    reward_final_confirmation_window_blocks=10,
    reward_sync_lag_tolerance_blocks=5,
    max_rewarded_nodes_per_epoch=15,
    max_attestation_bundles_per_block=4,
    max_attestations_per_bundle=24,
    max_attestations_per_verifier_per_window=1,
    target_block_time_activation_height=4_500,
    legacy_target_block_time_seconds=300,
    reward_node_fee_recent_driver_activation_height=12_000,
)
