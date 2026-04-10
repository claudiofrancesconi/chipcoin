from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

from chipcoin.consensus.merkle import merkle_root
from chipcoin.consensus.models import Block, BlockHeader, Transaction, TxOutput
from chipcoin.consensus.pow import verify_proof_of_work
from chipcoin.consensus.serialization import deserialize_transaction
from chipcoin.miner.config import MinerWorkerConfig
from chipcoin.miner.template_client import MiningApiError
from chipcoin.miner.worker import MinerWorker
from chipcoin.node.service import NodeService
from tests.helpers import wallet_key


class FakeTime:
    def __init__(self, *, now: float = 1_700_000_000.0, monotonic_value: float = 0.0) -> None:
        self._now = now
        self._monotonic = monotonic_value

    def time(self) -> float:
        return self._now

    def monotonic(self) -> float:
        return self._monotonic

    def sleep(self, seconds: float) -> None:
        self._now += seconds
        self._monotonic += seconds


def _make_service(database_path: Path) -> NodeService:
    timestamps = iter(range(1_700_000_000, 1_700_100_000))
    return NodeService.open_sqlite(database_path, time_provider=lambda: next(timestamps))


def _mine_block(block: Block) -> Block:
    for nonce in range(2_000_000):
        header = replace(block.header, nonce=nonce)
        if verify_proof_of_work(header):
            return replace(block, header=header)
    raise AssertionError("Expected to find a valid nonce for the easy target.")


def _block_from_template(template: dict[str, object]) -> Block:
    coinbase = Transaction(
        version=1,
        inputs=(),
        outputs=tuple(
            TxOutput(value=int(output["amount_chipbits"]), recipient=str(output["recipient"]))
            for output in template["coinbase_tx"]["outputs"]
        ),
        metadata={"coinbase": "true", "height": str(template["height"]), "extra_nonce": "1"},
    )
    transactions = [coinbase]
    for row in template["transactions"]:
        raw = bytes.fromhex(str(row["raw_hex"]))
        transaction, offset = deserialize_transaction(raw)
        assert offset == len(raw)
        transactions.append(transaction)
    return _mine_block(
        Block(
            header=BlockHeader(
                version=int(template["version"]),
                previous_block_hash=str(template["previous_block_hash"]),
                merkle_root=merkle_root([transaction.txid() for transaction in transactions]),
                timestamp=int(template["curtime"]),
                bits=int(template["bits"]),
                nonce=0,
            ),
            transactions=tuple(transactions),
        )
    )


class FakeMiningClient:
    def __init__(self, service: NodeService, *, base_url: str = "memory://node", fail: bool = False) -> None:
        self.service = service
        self.base_url = base_url
        self.fail = fail

    def status(self) -> dict[str, object]:
        if self.fail:
            raise MiningApiError("node unavailable")
        return self.service.mining_status()

    def get_block_template(self, *, payout_address: str, miner_id: str, template_mode: str = "full_block") -> dict[str, object]:
        if self.fail:
            raise MiningApiError("node unavailable")
        return self.service.get_block_template(
            payout_address=payout_address,
            miner_id=miner_id,
            template_mode=template_mode,
        )

    def submit_block(self, *, template_id: str, serialized_block: str, miner_id: str) -> dict[str, object]:
        if self.fail:
            raise MiningApiError("node unavailable")
        return self.service.submit_mined_block(
            template_id=template_id,
            serialized_block_hex=serialized_block,
            miner_id=miner_id,
        )


def test_miner_worker_fetches_template_immediately_and_mines_remote_block() -> None:
    with TemporaryDirectory() as tempdir:
        service = _make_service(Path(tempdir) / "node.sqlite3")
        worker = MinerWorker(
            MinerWorkerConfig(
                network="mainnet",
                payout_address=wallet_key(0).address,
                node_urls=("memory://node",),
                miner_id="worker-a",
                nonce_batch_size=2_000_000,
                run_seconds=3.0,
            )
        )
        worker.clients = [FakeMiningClient(service)]

        result = worker.run()

        assert result["accepted_blocks"] >= 1
        assert result["current_node_endpoint"] == "memory://node"
        assert result["submit_accepted_count"] >= 1
        assert result["submit_rejected_count"] == 0
        assert service.chain_tip() is not None


def test_miner_worker_marks_template_stale_after_tip_change() -> None:
    with TemporaryDirectory() as tempdir:
        service = _make_service(Path(tempdir) / "node.sqlite3")
        fake_time = FakeTime()
        worker = MinerWorker(
            MinerWorkerConfig(
                network="mainnet",
                payout_address=wallet_key(0).address,
                node_urls=("memory://node",),
                miner_id="worker-a",
                nonce_batch_size=1000,
                run_seconds=0.1,
            ),
            time_module=fake_time,
        )
        worker.clients = [FakeMiningClient(service)]
        template = worker._acquire_template()
        service.apply_block(_mine_block(service.build_candidate_block(wallet_key(1).address).block))
        fake_time._monotonic = template.next_status_check_at

        assert worker._template_is_stale(template) is True
        decision = worker._template_refresh_decision(template)
        assert decision is not None
        assert decision.reason == "tip_changed"
        assert decision.details["current_best_height"] == 0


def test_miner_worker_marks_template_expired_after_ttl() -> None:
    with TemporaryDirectory() as tempdir:
        service = _make_service(Path(tempdir) / "node.sqlite3")
        fake_time = FakeTime()
        worker = MinerWorker(
            MinerWorkerConfig(
                network="mainnet",
                payout_address=wallet_key(0).address,
                node_urls=("memory://node",),
                miner_id="worker-a",
                nonce_batch_size=1000,
                run_seconds=0.1,
                template_refresh_skew_seconds=1,
            ),
            time_module=fake_time,
        )
        worker.clients = [FakeMiningClient(service)]
        template = worker._acquire_template()
        fake_time._now = int(template.payload["template_expiry"])

        decision = worker._template_refresh_decision(template)

        assert decision is not None
        assert decision.reason == "expired"
        assert decision.details["template_expiry"] == int(template.payload["template_expiry"])


def test_miner_worker_fails_over_to_secondary_node() -> None:
    with TemporaryDirectory() as tempdir:
        service = _make_service(Path(tempdir) / "node.sqlite3")
        worker = MinerWorker(
            MinerWorkerConfig(
                network="mainnet",
                payout_address=wallet_key(0).address,
                node_urls=("memory://primary", "memory://secondary"),
                miner_id="worker-a",
                nonce_batch_size=2_000_000,
                run_seconds=3.0,
            )
        )
        worker.clients = [
            FakeMiningClient(service, base_url="memory://primary", fail=True),
            FakeMiningClient(service, base_url="memory://secondary"),
        ]

        result = worker.run()

        assert result["accepted_blocks"] >= 1
        assert result["current_node_endpoint"] == "memory://secondary"
        assert result["failover_count"] == 1


def test_miner_worker_template_startup_does_not_depend_on_history_depth() -> None:
    with TemporaryDirectory() as tempdir:
        service = _make_service(Path(tempdir) / "node.sqlite3")
        for _ in range(20):
            service.apply_block(_mine_block(service.build_candidate_block(wallet_key(0).address).block))
        worker = MinerWorker(
            MinerWorkerConfig(
                network="mainnet",
                payout_address=wallet_key(0).address,
                node_urls=("memory://node",),
                miner_id="worker-a",
                nonce_batch_size=1000,
                run_seconds=0.1,
            )
        )
        worker.clients = [FakeMiningClient(service)]

        template = worker._acquire_template()

        assert int(template.payload["height"]) == 20
        assert str(template.payload["previous_block_hash"]) == service.chain_tip().block_hash
