from chipcoin.consensus.economics import node_reward_pool_chipbits
from chipcoin.consensus.nodes import InMemoryNodeRegistryView, NodeRecord, select_rewarded_nodes
from chipcoin.consensus.params import MAINNET_PARAMS
from chipcoin.node.mining import MiningCoordinator
from tests.helpers import wallet_key


def _registry_with_active_nodes(count: int) -> InMemoryNodeRegistryView:
    records = []
    for index in range(count):
        records.append(
            NodeRecord(
                node_id=f"node-{index}",
                payout_address=wallet_key(index % 3).address,
                owner_pubkey=(index + 1).to_bytes(33, "big"),
                registered_height=0,
                last_renewed_height=0,
            )
        )
    return InMemoryNodeRegistryView.from_records(records)


def test_winner_selection_with_zero_active_nodes() -> None:
    winners = select_rewarded_nodes(
        _registry_with_active_nodes(0),
        height=1,
        previous_block_hash="00" * 32,
        node_reward_pool_chipbits=node_reward_pool_chipbits(0, MAINNET_PARAMS),
        params=MAINNET_PARAMS,
    )

    assert winners == []


def test_winner_selection_with_one_active_node() -> None:
    winners = select_rewarded_nodes(
        _registry_with_active_nodes(1),
        height=1,
        previous_block_hash="11" * 32,
        node_reward_pool_chipbits=node_reward_pool_chipbits(0, MAINNET_PARAMS),
        params=MAINNET_PARAMS,
    )

    assert len(winners) == 1
    assert winners[0].reward_chipbits == 500_000_000


def test_winner_selection_with_three_active_nodes() -> None:
    winners = select_rewarded_nodes(
        _registry_with_active_nodes(3),
        height=1,
        previous_block_hash="22" * 32,
        node_reward_pool_chipbits=node_reward_pool_chipbits(0, MAINNET_PARAMS),
        params=MAINNET_PARAMS,
    )

    assert len(winners) == 3
    assert {winner.reward_chipbits for winner in winners} == {166_666_666}


def test_winner_selection_with_ten_active_nodes() -> None:
    winners = select_rewarded_nodes(
        _registry_with_active_nodes(10),
        height=1,
        previous_block_hash="33" * 32,
        node_reward_pool_chipbits=node_reward_pool_chipbits(0, MAINNET_PARAMS),
        params=MAINNET_PARAMS,
    )

    assert len(winners) == 10
    assert {winner.reward_chipbits for winner in winners} == {50_000_000}


def test_winner_selection_with_more_than_ten_active_nodes() -> None:
    winners = select_rewarded_nodes(
        _registry_with_active_nodes(13),
        height=1,
        previous_block_hash="44" * 32,
        node_reward_pool_chipbits=node_reward_pool_chipbits(0, MAINNET_PARAMS),
        params=MAINNET_PARAMS,
    )

    assert len(winners) == 10


def test_mining_coinbase_assembly_with_three_active_nodes() -> None:
    registry = _registry_with_active_nodes(3)
    coordinator = MiningCoordinator(params=MAINNET_PARAMS, time_provider=lambda: 1_700_000_000)

    template = coordinator.build_block_template(
        previous_block_hash="55" * 32,
        height=1,
        miner_address=wallet_key(0).address,
        bits=MAINNET_PARAMS.genesis_bits,
        mempool_entries=[],
        node_registry_view=registry,
    )

    coinbase = template.block.transactions[0]
    assert len(coinbase.outputs) == 4
    assert int(coinbase.outputs[0].value) == 5_000_000_002
    assert {int(output.value) for output in coinbase.outputs[1:]} == {166_666_666}
