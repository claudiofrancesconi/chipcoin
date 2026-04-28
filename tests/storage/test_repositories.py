from pathlib import Path
from tempfile import TemporaryDirectory

from chipcoin.consensus.merkle import merkle_root
from chipcoin.consensus.models import Block, BlockHeader, OutPoint, Transaction, TxOutput
from chipcoin.consensus.utxo import UtxoEntry
from chipcoin.storage.blocks import SQLiteBlockRepository
from chipcoin.storage.chainstate import SQLiteChainStateRepository
from chipcoin.storage.db import initialize_database
from chipcoin.storage.headers import SQLiteHeaderRepository
from chipcoin.storage.native_rewards import SQLiteEpochSettlementRepository, SQLiteRewardAttestationRepository
from chipcoin.storage.node_registry import SQLiteNodeRegistryRepository
from chipcoin.consensus.nodes import NodeRecord
from chipcoin.consensus.epoch_settlement import (
    RewardAttestation,
    RewardAttestationBundle,
    RewardSettlement,
    RewardSettlementEntry,
)
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
                node_pubkey=wallet_key(1).public_key,
                declared_host="node-1.example",
                declared_port=18444,
                reward_registration=True,
            )

            repository.upsert(record)

            assert repository.get_by_node_id("node-1") == record
            assert repository.get_by_owner_pubkey(wallet_key(0).public_key) == record
        finally:
            connection.close()


def test_native_reward_repositories_roundtrip() -> None:
    with TemporaryDirectory() as tempdir:
        connection = initialize_database(Path(tempdir) / "chipcoin.sqlite3")
        try:
            attestation_repository = SQLiteRewardAttestationRepository(connection)
            settlement_repository = SQLiteEpochSettlementRepository(connection)
            bundle = RewardAttestationBundle(
                epoch_index=3,
                bundle_window_index=2,
                bundle_submitter_node_id="node-submitter",
                attestations=(
                    RewardAttestation(
                        epoch_index=3,
                        check_window_index=2,
                        candidate_node_id="node-a",
                        verifier_node_id="node-b",
                        result_code="pass",
                        observed_sync_gap=1,
                        endpoint_commitment="endpoint-a",
                        concentration_key="ip:1.2.3.4",
                        signature_hex="aa",
                    ),
                ),
            )
            settlement = RewardSettlement(
                epoch_index=3,
                epoch_start_height=300,
                epoch_end_height=399,
                epoch_seed_hex="11" * 32,
                policy_version="v1",
                submission_mode="auto",
                candidate_summary_root="22" * 32,
                verified_nodes_root="33" * 32,
                rewarded_nodes_root="44" * 32,
                rewarded_node_count=1,
                distributed_node_reward_chipbits=5_000_000_000,
                undistributed_node_reward_chipbits=0,
                reward_entries=(
                    RewardSettlementEntry(
                        node_id="node-a",
                        payout_address=wallet_key(0).address,
                        reward_chipbits=5_000_000_000,
                        selection_rank=0,
                        concentration_key="ip:1.2.3.4",
                        final_confirmation_passed=True,
                    ),
                ),
            )

            attestation_repository.add_bundle(txid="aa" * 32, block_height=321, bundle=bundle)
            settlement_repository.add_settlement(txid="bb" * 32, block_height=399, settlement=settlement)

            stored_bundles = attestation_repository.list_bundles(epoch_index=3)
            stored_settlements = settlement_repository.list_settlements(epoch_index=3)

            assert len(stored_bundles) == 1
            assert stored_bundles[0].bundle == bundle
            assert attestation_repository.attestation_identities() == {(3, 2, "node-a", "node-b")}
            assert len(stored_settlements) == 1
            assert stored_settlements[0].settlement == settlement
            assert settlement_repository.settled_epoch_indexes() == {3}
            assert settlement_repository.total_distributed_node_reward_chipbits() == 5_000_000_000
        finally:
            connection.close()
