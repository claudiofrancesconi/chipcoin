from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

from chipcoin.consensus.models import Block, BlockHeader, OutPoint
from chipcoin.consensus.params import MAINNET_PARAMS
from chipcoin.consensus.pow import verify_proof_of_work
from chipcoin.node.messages import GetHeadersMessage, HeadersMessage
from chipcoin.node.mempool import MempoolPolicy
from chipcoin.node.service import NodeService
from chipcoin.node.sync import SyncManager
from tests.helpers import signed_payment, wallet_key


def _make_service(database_path: Path, *, start_time: int) -> NodeService:
    timestamps = iter(range(start_time, start_time + 500))
    return NodeService.open_sqlite(database_path, time_provider=lambda: next(timestamps))


def _mine_block(block: Block) -> Block:
    for nonce in range(2_000_000):
        header = replace(block.header, nonce=nonce)
        if verify_proof_of_work(header):
            return replace(block, header=header)
    raise AssertionError("Expected to find a valid nonce for the easy target.")


def _mine_chain(service: NodeService, count: int, miner_address: str) -> list[Block]:
    blocks: list[Block] = []
    for _ in range(count):
        template = service.build_candidate_block(miner_address)
        block = _mine_block(template.block)
        service.apply_block(block)
        blocks.append(block)
    return blocks


def test_getheaders_returns_headers_after_locator() -> None:
    with TemporaryDirectory() as tempdir:
        service = _make_service(Path(tempdir) / "node.sqlite3", start_time=1_700_000_000)
        blocks = _mine_chain(service, 3, "CHCminer-a")

        response = service.handle_getheaders(
            GetHeadersMessage(
                protocol_version=1,
                locator_hashes=(blocks[0].block_hash(),),
                stop_hash="00" * 32,
            )
        )

        assert response == HeadersMessage(headers=(blocks[1].header, blocks[2].header))


def test_sync_manager_downloads_headers_and_blocks_header_first() -> None:
    with TemporaryDirectory() as tempdir:
        source = _make_service(Path(tempdir) / "source.sqlite3", start_time=1_700_000_000)
        target = _make_service(Path(tempdir) / "target.sqlite3", start_time=1_700_001_000)
        blocks = _mine_chain(source, 3, "CHCminer-source")

        result = SyncManager(node=target).synchronize(source)

        assert result.headers_received == 3
        assert result.blocks_fetched == 3
        assert result.activated_tip == blocks[-1].block_hash()
        assert target.chain_tip() is not None
        assert target.chain_tip().block_hash == blocks[-1].block_hash()
        assert target.get_block_by_hash(blocks[-1].block_hash()) == blocks[-1]


def test_sync_manager_reorgs_to_branch_with_more_cumulative_work() -> None:
    with TemporaryDirectory() as tempdir:
        node_a = _make_service(Path(tempdir) / "node-a.sqlite3", start_time=1_700_000_000)
        node_b = _make_service(Path(tempdir) / "node-b.sqlite3", start_time=1_700_001_000)
        node_c = _make_service(Path(tempdir) / "node-c.sqlite3", start_time=1_700_002_000)

        chain_a = _mine_chain(node_a, 2, "CHCminer-a")
        chain_c = _mine_chain(node_c, 3, "CHCminer-c")

        sync_b = SyncManager(node=node_b)
        first = sync_b.synchronize(node_a)
        second = sync_b.synchronize(node_c)

        assert first.activated_tip == chain_a[-1].block_hash()
        assert second.activated_tip == chain_c[-1].block_hash()
        assert second.reorged is True
        assert second.reorg_depth == 2
        assert second.old_tip == chain_a[-1].block_hash()
        assert second.new_tip == chain_c[-1].block_hash()
        assert second.common_ancestor is None
        assert node_b.chain_tip() is not None
        assert node_b.chain_tip().block_hash == chain_c[-1].block_hash()
        latest_coinbase_txid = chain_c[-1].transactions[0].txid()
        latest_coinbase_utxo = node_b.chainstate.get_utxo(OutPoint(txid=latest_coinbase_txid, index=0))
        assert latest_coinbase_utxo is not None
        assert latest_coinbase_utxo.output.recipient == "CHCminer-c"


def test_sync_manager_reports_unknown_parent_when_peer_headers_are_orphaned() -> None:
    with TemporaryDirectory() as tempdir:
        target = _make_service(Path(tempdir) / "target.sqlite3", start_time=1_700_000_000)
        orphan_header = BlockHeader(
            version=1,
            previous_block_hash="AA" * 32,
            merkle_root="BB" * 32,
            timestamp=1_700_000_100,
            bits=target.params.genesis_bits,
            nonce=0,
        )

        class OrphanPeer:
            def handle_getheaders(self, request: GetHeadersMessage, *, limit: int = 2000) -> HeadersMessage:
                del request, limit
                return HeadersMessage(headers=(orphan_header,))

            def get_block_by_hash(self, block_hash: str):
                del block_hash
                return None

        result = SyncManager(node=target).synchronize(OrphanPeer())

        assert result.parent_unknown == "AA" * 32
        assert result.activated_tip is None
        assert target.chain_tip() is None


def test_sync_manager_continues_across_multiple_header_batches() -> None:
    with TemporaryDirectory() as tempdir:
        source = _make_service(Path(tempdir) / "source.sqlite3", start_time=1_700_000_000)
        target = _make_service(Path(tempdir) / "target.sqlite3", start_time=1_700_001_000)
        blocks = _mine_chain(source, 5, "CHCminer-source")

        result = SyncManager(node=target, max_headers=2).synchronize(source)

        assert result.headers_received == 5
        assert result.blocks_fetched == 5
        assert result.activated_tip == blocks[-1].block_hash()
        assert target.chain_tip() is not None
        assert target.chain_tip().block_hash == blocks[-1].block_hash()


def test_sync_manager_accepts_orphan_block_after_parent_arrives() -> None:
    with TemporaryDirectory() as tempdir:
        source = _make_service(Path(tempdir) / "source.sqlite3", start_time=1_700_000_000)
        target = _make_service(Path(tempdir) / "target.sqlite3", start_time=1_700_001_000)
        blocks = _mine_chain(source, 2, "CHCminer-source")
        manager = SyncManager(node=target)

        child_first = manager.receive_block(blocks[1])
        parent_second = manager.receive_block(blocks[0])

        assert child_first.parent_unknown == blocks[0].block_hash()
        assert child_first.accepted_blocks == 0
        assert parent_second.accepted_blocks == 2
        assert target.get_block_by_hash(blocks[0].block_hash()) == blocks[0]
        assert target.get_block_by_hash(blocks[1].block_hash()) == blocks[1]
        assert target.chain_tip() is not None
        assert target.chain_tip().block_hash == blocks[1].block_hash()


def test_service_retargets_difficulty_every_thousand_blocks() -> None:
    with TemporaryDirectory() as tempdir:
        timestamps = iter(range(1_700_000_000, 1_700_010_500))
        service = NodeService.open_sqlite(Path(tempdir) / "node.sqlite3", time_provider=lambda: next(timestamps))
        mined_timestamps = []
        for height in range(MAINNET_PARAMS.difficulty_adjustment_window):
            template = service.build_candidate_block("CHCminer-retarget")
            faster_header = replace(template.block.header, timestamp=1_700_000_000 + (height * 60))
            block = _mine_block(replace(template.block, header=faster_header))
            service.apply_block(block)
            mined_timestamps.append(block.header.timestamp)

        next_bits = service.expected_next_bits()

        assert next_bits != MAINNET_PARAMS.genesis_bits


def test_reorg_readds_transactions_from_disconnected_branch_back_to_mempool() -> None:
    with TemporaryDirectory() as tempdir:
        test_params = replace(MAINNET_PARAMS, coinbase_maturity=0)
        common_times = iter(range(1_700_000_000, 1_700_000_500))
        target_times = iter(range(1_700_001_000, 1_700_001_500))
        alt_times = iter(range(1_700_002_000, 1_700_002_500))
        common = NodeService.open_sqlite(
            Path(tempdir) / "common.sqlite3",
            params=test_params,
            time_provider=lambda: next(common_times),
        )
        target = NodeService.open_sqlite(
            Path(tempdir) / "target.sqlite3",
            params=test_params,
            time_provider=lambda: next(target_times),
        )
        alt = NodeService.open_sqlite(
            Path(tempdir) / "alt.sqlite3",
            params=test_params,
            time_provider=lambda: next(alt_times),
        )
        target.mempool.policy = MempoolPolicy(min_fee_chipbits_normal_tx=1)

        common_block = _mine_block(common.build_candidate_block(wallet_key(0).address).block)
        common.apply_block(common_block)
        SyncManager(node=target).receive_block(common_block)
        alt.apply_block(common_block)

        spend = signed_payment(
            OutPoint(txid=common_block.transactions[0].txid(), index=0),
            value=int(common_block.transactions[0].outputs[0].value),
            sender=wallet_key(0),
            fee=10,
        )
        target.receive_transaction(spend)
        branch_a = _mine_block(target.build_candidate_block(wallet_key(1).address).block)
        target.apply_block(branch_a)
        assert target.find_transaction(spend.txid())["location"] == "chain"

        branch_b1 = _mine_block(alt.build_candidate_block(wallet_key(2).address).block)
        alt.apply_block(branch_b1)
        branch_b2 = _mine_block(alt.build_candidate_block(wallet_key(2).address).block)
        alt.apply_block(branch_b2)

        manager = SyncManager(node=target)
        manager.receive_block(branch_b1)
        result = manager.receive_block(branch_b2)

        assert result.reorged is True
        assert result.reorg_depth == 1
        assert result.readded_transaction_count == 1
        readded = target.find_transaction(spend.txid())
        assert readded is not None
        assert readded["location"] == "mempool"


def test_sync_manager_reorgs_across_difficulty_retarget_boundary() -> None:
    with TemporaryDirectory() as tempdir:
        custom_params = replace(MAINNET_PARAMS, difficulty_adjustment_window=3, coinbase_maturity=0)
        a_times = iter(range(1_700_010_000, 1_700_011_000))
        b_times = iter(range(1_700_020_000, 1_700_021_000))
        c_times = iter(range(1_700_030_000, 1_700_031_000))
        node_a = NodeService.open_sqlite(
            Path(tempdir) / "node-a.sqlite3",
            params=custom_params,
            time_provider=lambda: next(a_times),
        )
        node_b = NodeService.open_sqlite(
            Path(tempdir) / "node-b.sqlite3",
            params=custom_params,
            time_provider=lambda: next(b_times),
        )
        node_c = NodeService.open_sqlite(
            Path(tempdir) / "node-c.sqlite3",
            params=custom_params,
            time_provider=lambda: next(c_times),
        )

        chain_a = []
        for height in range(4):
            template = node_a.build_candidate_block(wallet_key(0).address)
            block = _mine_block(replace(template.block, header=replace(template.block.header, timestamp=1_700_010_000 + (height * 180))))
            node_a.apply_block(block)
            chain_a.append(block)

        chain_c = []
        for height in range(5):
            template = node_c.build_candidate_block(wallet_key(1).address)
            block = _mine_block(replace(template.block, header=replace(template.block.header, timestamp=1_700_030_000 + (height * 60))))
            node_c.apply_block(block)
            chain_c.append(block)

        sync_b = SyncManager(node=node_b)
        first = sync_b.synchronize(node_a)
        second = sync_b.synchronize(node_c)

        assert first.activated_tip == chain_a[-1].block_hash()
        assert second.activated_tip == chain_c[-1].block_hash()
        assert second.reorged is True
        assert node_b.chain_tip() is not None
        assert node_b.chain_tip().block_hash == chain_c[-1].block_hash()
