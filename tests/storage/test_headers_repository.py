from pathlib import Path
from tempfile import TemporaryDirectory

from chipcoin.consensus.merkle import merkle_root
from chipcoin.consensus.models import Block, BlockHeader, Transaction, TxOutput
from chipcoin.storage.db import initialize_database
from chipcoin.storage.headers import SQLiteHeaderRepository


def _block(previous_block_hash: str, *, timestamp: int, nonce: int) -> Block:
    transaction = Transaction(
        version=1,
        inputs=(),
        outputs=(TxOutput(value=50, recipient="CHCminer"),),
        metadata={"coinbase": "true"},
    )
    return Block(
        header=BlockHeader(
            version=1,
            previous_block_hash=previous_block_hash,
            merkle_root=merkle_root([transaction.txid()]),
            timestamp=timestamp,
            bits=0x207FFFFF,
            nonce=nonce,
        ),
        transactions=(transaction,),
    )


def test_find_best_tip_orders_large_cumulative_work_as_full_precision_integer() -> None:
    with TemporaryDirectory() as tempdir:
        connection = initialize_database(Path(tempdir) / "chipcoin.sqlite3")
        try:
            repository = SQLiteHeaderRepository(connection)
            genesis = _block("00" * 32, timestamp=1_700_000_000, nonce=1)
            branch_a = _block(genesis.block_hash(), timestamp=1_700_000_001, nonce=2)
            branch_b = _block(genesis.block_hash(), timestamp=1_700_000_002, nonce=3)

            repository.put(genesis.header, height=0, cumulative_work=2**63 - 1, is_main_chain=True)
            repository.put(branch_a.header, height=1, cumulative_work=2**63 + 5, is_main_chain=False)
            repository.put(branch_b.header, height=1, cumulative_work=10**30, is_main_chain=False)

            best_tip = repository.find_best_tip()

            assert best_tip is not None
            assert best_tip.block_hash == branch_b.block_hash()
        finally:
            connection.close()
