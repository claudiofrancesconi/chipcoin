from chipcoin.consensus.economics import (
    miner_subsidy_chipbits,
    node_reward_pool_chipbits,
    subsidy_split_chipbits,
    terminal_correction_chipbits,
    terminal_correction_height,
    total_block_subsidy_chipbits,
    total_subsidy_through_height,
)
from chipcoin.consensus.params import MAINNET_PARAMS


def test_block_subsidy_halves_on_schedule() -> None:
    assert miner_subsidy_chipbits(0, MAINNET_PARAMS) == 20 * 100_000_000
    assert node_reward_pool_chipbits(0, MAINNET_PARAMS) == 2 * 100_000_000
    assert total_block_subsidy_chipbits(0, MAINNET_PARAMS) == 22 * 100_000_000
    assert miner_subsidy_chipbits(MAINNET_PARAMS.halving_interval, MAINNET_PARAMS) == 10 * 100_000_000


def test_total_subsidy_tracks_option_a_milestones() -> None:
    assert total_subsidy_through_height(249_999, MAINNET_PARAMS) == 550_000_000_000_000
    assert total_subsidy_through_height(499_999, MAINNET_PARAMS) == 825_000_000_000_000
    assert total_subsidy_through_height(749_999, MAINNET_PARAMS) == 962_500_000_000_000
    assert total_subsidy_through_height(999_999, MAINNET_PARAMS) == 1_031_250_000_000_000
    assert total_subsidy_through_height(1_499_999, MAINNET_PARAMS) == 1_082_812_500_000_000


def test_total_subsidy_never_exceeds_max_money() -> None:
    total = total_subsidy_through_height(10_000_000, MAINNET_PARAMS)

    assert total <= MAINNET_PARAMS.max_money_chipbits
    assert total == MAINNET_PARAMS.max_money_chipbits


def test_terminal_correction_is_explicit_and_exact() -> None:
    correction_height = terminal_correction_height(MAINNET_PARAMS)
    correction_chipbits = terminal_correction_chipbits(MAINNET_PARAMS)

    assert correction_height == 7_750_000
    assert correction_chipbits == 6_250_000
    assert subsidy_split_chipbits(correction_height - 1, MAINNET_PARAMS) == (1, 0)
    assert subsidy_split_chipbits(correction_height, MAINNET_PARAMS) == (6_250_000, 0)
    assert subsidy_split_chipbits(correction_height + 1, MAINNET_PARAMS) == (0, 0)
    assert total_subsidy_through_height(correction_height - 1, MAINNET_PARAMS) == (
        MAINNET_PARAMS.max_money_chipbits - correction_chipbits
    )
    assert total_subsidy_through_height(correction_height, MAINNET_PARAMS) == MAINNET_PARAMS.max_money_chipbits
