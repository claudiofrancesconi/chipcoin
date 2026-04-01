from pathlib import Path
from tempfile import TemporaryDirectory

from chipcoin.consensus.merkle import merkle_root
from chipcoin.consensus.models import Block, BlockHeader, OutPoint, Transaction, TxOutput
from chipcoin.consensus.utxo import UtxoEntry
from chipcoin.storage.blocks import SQLiteBlockRepository
from chipcoin.storage.chainstate import SQLiteChainStateRepository
from chipcoin.storage.db import initialize_database
from chipcoin.storage.headers import SQLiteHeaderRepository
from chipcoin.storage.node_registry import SQLiteNodeRegistryRepository
from chipcoin.consensus.nodes import NodeRecord
from tests.helpers import wallet_key


def _sample_transaction() -> Transaction:
    return Transaction(
        version=1,
        inputs=(),
        outputs=(TxOutput(value=50, recipient="CHCminer"),),
        metadata={"coinbase": "true"},
    )


def _sample_block() -> Block:
    transaction = _sample_transaction()
    return Block(
        header=BlockHeader(
            version=1,
            previous_block_hash="00" * 32,
            merkle_root=merkle_root([transaction.txid()]),
            timestamp=1_700_000_000,
            bits=0x207FFFFF,
            nonce=1,
        ),
        transactions=(transaction,),
    )


def test_header_repository_roundtrip_and_tip_storage() -> None:
    with TemporaryDirectory() as tempdir:
        connection = initialize_database(Path(tempdir) / "chipcoin.sqlite3")
        try:
            repository = SQLiteHeaderRepository(connection)
            block = _sample_block()

            repository.put(block.header, height=0, cumulative_work=1, is_main_chain=True)
            repository.set_tip(block.block_hash(), 0)

            assert repository.get(block.block_hash()) == block.header
            tip = repository.get_tip()
            assert tip is not None
            assert tip.block_hash == block.block_hash()
            assert tip.height == 0
        finally:
            connection.close()


def test_block_repository_roundtrip_returns_same_block() -> None:
    with TemporaryDirectory() as tempdir:
        connection = initialize_database(Path(tempdir) / "chipcoin.sqlite3")
        try:
            block = _sample_block()
            SQLiteHeaderRepository(connection).put(block.header, height=0, cumulative_work=1, is_main_chain=True)
            repository = SQLiteBlockRepository(connection)

            repository.put(block)

            assert repository.get(block.block_hash()) == block
        finally:
            connection.close()


def test_chainstate_repository_can_store_read_and_spend_utxo() -> None:
    with TemporaryDirectory() as tempdir:
        connection = initialize_database(Path(tempdir) / "chipcoin.sqlite3")
        try:
            repository = SQLiteChainStateRepository(connection)
            outpoint = OutPoint(txid="11" * 32, index=1)
            entry = UtxoEntry(
                output=TxOutput(value=125, recipient="CHCalice"),
                height=7,
                is_coinbase=False,
            )

            repository.put_utxo(outpoint, entry)
            assert repository.get_utxo(outpoint) == entry

            repository.spend_utxo(outpoint)
            assert repository.get_utxo(outpoint) is None
        finally:
            connection.close()


def test_chainstate_repository_apply_transaction_persists_outputs() -> None:
    with TemporaryDirectory() as tempdir:
        connection = initialize_database(Path(tempdir) / "chipcoin.sqlite3")
        try:
            repository = SQLiteChainStateRepository(connection)
            transaction = Transaction(
                version=1,
                inputs=(),
                outputs=(
                    TxOutput(value=30, recipient="CHCbob"),
                    TxOutput(value=20, recipient="CHCchange"),
                ),
                metadata={"coinbase": "true"},
            )

            repository.apply_transaction(transaction, height=5, is_coinbase=True)

            first = repository.get_utxo(OutPoint(txid=transaction.txid(), index=0))
            second = repository.get_utxo(OutPoint(txid=transaction.txid(), index=1))
            assert first is not None
            assert second is not None
            assert first.is_coinbase is True
            assert second.output.recipient == "CHCchange"
        finally:
            connection.close()


def test_node_registry_repository_roundtrip() -> None:
    with TemporaryDirectory() as tempdir:
        connection = initialize_database(Path(tempdir) / "chipcoin.sqlite3")
        try:
            repository = SQLiteNodeRegistryRepository(connection)
            record = NodeRecord(
                node_id="node-1",
                payout_address=wallet_key(0).address,
                owner_pubkey=wallet_key(0).public_key,
                registered_height=10,
                last_renewed_height=10,
            )

            repository.upsert(record)

            assert repository.get_by_node_id("node-1") == record
            assert repository.get_by_owner_pubkey(wallet_key(0).public_key) == record
        finally:
            connection.close()
