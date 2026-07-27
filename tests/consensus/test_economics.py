from chipcoin.consensus.economics import (
    REWARD_NODE_MIN_REGISTER_FEE_CHIPBITS,
    REWARD_NODE_MIN_RENEW_FEE_CHIPBITS,
    is_epoch_reward_height,
    miner_subsidy_chipbits,
    node_reward_pool_chipbits,
    reward_fee_node_count,
    reward_registered_node_count,
    renew_reward_node_fee_chipbits,
    register_reward_node_fee_chipbits,
    subsidy_split_chipbits,
    total_block_subsidy_chipbits,
    total_subsidy_through_height,
)
from chipcoin.consensus.nodes import InMemoryNodeRegistryView, NodeRecord
from chipcoin.consensus.params import MAINNET_PARAMS, TESTNET_PARAMS


EXACT_CAP_HEIGHT = 643_297


def _legacy_subsidy_totals_through_height(height: int, params) -> tuple[int, int]:
    if height < 0:
        return 0, 0

    miner_total = 0
    node_total = 0
    for current_height in range(height + 1):
        halvings = current_height // params.halving_interval
        miner_subsidy = max(params.initial_miner_subsidy_chipbits >> halvings, 0)
        node_reward = 0
        if (current_height + 1) % params.epoch_length_blocks == 0:
            node_reward = max(params.initial_node_epoch_reward_chipbits >> halvings, 0)
        if miner_subsidy <= 0 and node_reward <= 0:
            break
        remaining_supply = max(0, params.max_money_chipbits - miner_total - node_total)
        miner_total += min(miner_subsidy, remaining_supply)
        remaining_supply = max(0, params.max_money_chipbits - miner_total - node_total)
        node_total += min(node_reward, remaining_supply)
    return miner_total, node_total


def test_initial_subsidy_values_match_locked_baseline() -> None:
    assert miner_subsidy_chipbits(0, MAINNET_PARAMS) == 50 * 100_000_000
    assert node_reward_pool_chipbits(0, MAINNET_PARAMS) == 0
    assert node_reward_pool_chipbits(99, MAINNET_PARAMS) == 50 * 100_000_000
    assert total_block_subsidy_chipbits(0, MAINNET_PARAMS) == 50 * 100_000_000
    assert total_block_subsidy_chipbits(99, MAINNET_PARAMS) == 100 * 100_000_000


def test_node_reward_is_only_minted_on_epoch_closing_blocks() -> None:
    assert is_epoch_reward_height(98, MAINNET_PARAMS) is False
    assert is_epoch_reward_height(99, MAINNET_PARAMS) is True
    assert is_epoch_reward_height(100, MAINNET_PARAMS) is False

    assert node_reward_pool_chipbits(98, MAINNET_PARAMS) == 0
    assert node_reward_pool_chipbits(99, MAINNET_PARAMS) == 50 * 100_000_000
    assert node_reward_pool_chipbits(100, MAINNET_PARAMS) == 0


def test_halving_boundary_applies_to_miner_and_node_epoch_reward() -> None:
    boundary = MAINNET_PARAMS.halving_interval

    assert miner_subsidy_chipbits(boundary - 1, MAINNET_PARAMS) == 50 * 100_000_000
    assert miner_subsidy_chipbits(boundary, MAINNET_PARAMS) == 25 * 100_000_000

    assert is_epoch_reward_height(boundary - 1, MAINNET_PARAMS) is True
    assert node_reward_pool_chipbits(boundary - 1, MAINNET_PARAMS) == 50 * 100_000_000
    assert node_reward_pool_chipbits(boundary, MAINNET_PARAMS) == 0
    assert node_reward_pool_chipbits(boundary + 99, MAINNET_PARAMS) == 25 * 100_000_000


def test_subsidy_split_matches_epoch_reward_shape() -> None:
    assert subsidy_split_chipbits(0, MAINNET_PARAMS) == (50 * 100_000_000, 0)
    assert subsidy_split_chipbits(99, MAINNET_PARAMS) == (50 * 100_000_000, 50 * 100_000_000)
    assert subsidy_split_chipbits(100, MAINNET_PARAMS) == (50 * 100_000_000, 0)


def test_total_issuance_progression_matches_new_schedule() -> None:
    assert total_subsidy_through_height(-1, MAINNET_PARAMS) == 0
    assert total_subsidy_through_height(0, MAINNET_PARAMS) == 50 * 100_000_000
    assert total_subsidy_through_height(98, MAINNET_PARAMS) == 99 * 50 * 100_000_000
    assert total_subsidy_through_height(99, MAINNET_PARAMS) == 100 * 50 * 100_000_000 + 50 * 100_000_000
    assert total_subsidy_through_height(199, MAINNET_PARAMS) == 200 * 50 * 100_000_000 + 2 * 50 * 100_000_000


def test_total_issuance_matches_legacy_loop_at_boundaries() -> None:
    checkpoints = [
        -1,
        0,
        98,
        99,
        100,
        MAINNET_PARAMS.halving_interval - 1,
        MAINNET_PARAMS.halving_interval,
        MAINNET_PARAMS.halving_interval + 99,
        EXACT_CAP_HEIGHT - 1,
        EXACT_CAP_HEIGHT,
        EXACT_CAP_HEIGHT + 1,
    ]

    for height in checkpoints:
        miner_total, node_total = _legacy_subsidy_totals_through_height(height, MAINNET_PARAMS)
        assert total_subsidy_through_height(height, MAINNET_PARAMS) == miner_total + node_total


def test_first_era_total_matches_reference_number() -> None:
    assert total_subsidy_through_height(110_999, MAINNET_PARAMS) == 560_550_000_000_000


def test_cap_clamp_hits_exact_max_supply() -> None:
    total = total_subsidy_through_height(EXACT_CAP_HEIGHT, MAINNET_PARAMS)

    assert total == MAINNET_PARAMS.max_money_chipbits
    assert total == 11_000_000 * 100_000_000


def test_zero_issuance_after_cap_is_reached() -> None:
    max_supply = MAINNET_PARAMS.max_money_chipbits

    assert subsidy_split_chipbits(EXACT_CAP_HEIGHT + 1, MAINNET_PARAMS) == (0, 0)
    assert total_subsidy_through_height(EXACT_CAP_HEIGHT + 10_000, MAINNET_PARAMS) == max_supply


def test_cap_clamp_applies_to_the_exact_crossing_event() -> None:
    max_supply = MAINNET_PARAMS.max_money_chipbits
    minted_before = total_subsidy_through_height(EXACT_CAP_HEIGHT - 1, MAINNET_PARAMS)
    miner_subsidy, node_reward = subsidy_split_chipbits(EXACT_CAP_HEIGHT, MAINNET_PARAMS)

    assert minted_before < max_supply
    assert minted_before + miner_subsidy + node_reward == max_supply
    assert miner_subsidy >= 0
    assert node_reward >= 0


def test_reward_node_fee_schedule_starts_at_maximum_and_hits_minimum_at_target() -> None:
    assert register_reward_node_fee_chipbits(registered_reward_node_count=0, params=MAINNET_PARAMS) == MAINNET_PARAMS.register_node_fee_chipbits
    assert renew_reward_node_fee_chipbits(registered_reward_node_count=0, params=MAINNET_PARAMS) == MAINNET_PARAMS.renew_node_fee_chipbits
    assert register_reward_node_fee_chipbits(registered_reward_node_count=20_000, params=MAINNET_PARAMS) == REWARD_NODE_MIN_REGISTER_FEE_CHIPBITS
    assert renew_reward_node_fee_chipbits(registered_reward_node_count=20_000, params=MAINNET_PARAMS) == REWARD_NODE_MIN_RENEW_FEE_CHIPBITS


def test_reward_node_fee_schedule_decreases_monotonically_with_registry_growth() -> None:
    checkpoints = [1, 2, 10, 100, 1_000, 10_000, 20_000]
    register_fees = [register_reward_node_fee_chipbits(registered_reward_node_count=count, params=MAINNET_PARAMS) for count in checkpoints]
    renew_fees = [renew_reward_node_fee_chipbits(registered_reward_node_count=count, params=MAINNET_PARAMS) for count in checkpoints]

    assert register_fees == sorted(register_fees, reverse=True)
    assert renew_fees == sorted(renew_fees, reverse=True)


def _reward_record(node_id: str, *, registered_height: int, last_renewed_height: int) -> NodeRecord:
    return NodeRecord(
        node_id=node_id,
        payout_address="CHCCBRoVJkKrHGnwcMr3hxk4N4fbZeMDehU7W",
        owner_pubkey=node_id.encode().ljust(33, b"\0")[:33],
        registered_height=registered_height,
        last_renewed_height=last_renewed_height,
        node_pubkey=node_id.encode().ljust(33, b"\1")[:33],
        declared_host=f"{node_id}.example",
        declared_port=18444,
        reward_registration=True,
    )


def test_reward_fee_node_count_preserves_legacy_count_before_activation() -> None:
    registry = InMemoryNodeRegistryView.from_records(
        [
            _reward_record("active-current", registered_height=9_000, last_renewed_height=11_500),
            _reward_record("active-previous", registered_height=9_000, last_renewed_height=11_400),
            _reward_record("stale-old", registered_height=7_000, last_renewed_height=11_100),
        ]
    )

    assert reward_registered_node_count(registry) == 3
    assert reward_fee_node_count(registry, height=11_999, params=TESTNET_PARAMS) == 3


def test_reward_fee_node_count_excludes_stale_historical_reward_records_after_activation() -> None:
    registry = InMemoryNodeRegistryView.from_records(
        [
            _reward_record("active-current", registered_height=9_000, last_renewed_height=12_000),
            _reward_record("grace-one", registered_height=9_000, last_renewed_height=11_900),
            _reward_record("grace-two", registered_height=9_000, last_renewed_height=11_800),
            _reward_record("stale-old", registered_height=7_000, last_renewed_height=11_700),
        ]
    )

    assert reward_registered_node_count(registry) == 4
    assert reward_fee_node_count(registry, height=12_001, params=TESTNET_PARAMS) == 3


def test_reward_fee_node_count_keeps_two_epoch_grace_at_epoch_boundary() -> None:
    registry = InMemoryNodeRegistryView.from_records(
        [
            _reward_record("previous-one", registered_height=9_000, last_renewed_height=11_900),
            _reward_record("previous-two", registered_height=9_000, last_renewed_height=11_800),
            _reward_record("older-epoch", registered_height=9_000, last_renewed_height=11_700),
        ]
    )

    assert reward_fee_node_count(registry, height=12_000, params=TESTNET_PARAMS) == 2
