from chipcoin.consensus.economics import (
    miner_subsidy_chipbits,
    node_reward_pool_chipbits,
    total_block_subsidy_chipbits,
    total_subsidy_through_height,
)
from chipcoin.consensus.params import MAINNET_PARAMS


def test_block_subsidy_halves_on_schedule() -> None:
    assert miner_subsidy_chipbits(0, MAINNET_PARAMS) == 50 * 100_000_000
    assert node_reward_pool_chipbits(0, MAINNET_PARAMS) == 5 * 100_000_000
    assert total_block_subsidy_chipbits(0, MAINNET_PARAMS) == 55 * 100_000_000
    assert miner_subsidy_chipbits(MAINNET_PARAMS.halving_interval, MAINNET_PARAMS) == 25 * 100_000_000


def test_total_subsidy_never_exceeds_max_money() -> None:
    total = total_subsidy_through_height(1_000_000, MAINNET_PARAMS)

    assert total <= MAINNET_PARAMS.max_money_chipbits
