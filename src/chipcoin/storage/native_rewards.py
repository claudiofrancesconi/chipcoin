"""Repositories for native reward-node attestations and epoch settlements."""

from __future__ import annotations

import json
from dataclasses import dataclass
from sqlite3 import Connection

from ..consensus.epoch_settlement import (
    RewardAttestation,
    RewardAttestationBundle,
    RewardSettlement,
    RewardSettlementEntry,
    attestation_identity,
    parse_reward_attestation_bundle_metadata,
    parse_reward_settlement_metadata,
)


@dataclass(frozen=True)
class StoredRewardAttestationBundle:
    """One persisted native attestation bundle."""

    txid: str
    block_height: int
    bundle: RewardAttestationBundle


@dataclass(frozen=True)
class StoredEpochSettlement:
    """One persisted native epoch settlement payload."""

    txid: str
    block_height: int
    settlement: RewardSettlement


class SQLiteRewardAttestationRepository:
    """SQLite-backed repository for native attestation bundles."""

    def __init__(self, connection: Connection) -> None:
        self.connection = connection

    def add_bundle(self, *, txid: str, block_height: int, bundle: RewardAttestationBundle) -> None:
        raw_attestations_json = json.dumps(
            [
                {
                    "epoch_index": attestation.epoch_index,
                    "check_window_index": attestation.check_window_index,
                    "candidate_node_id": attestation.candidate_node_id,
                    "verifier_node_id": attestation.verifier_node_id,
                    "result_code": attestation.result_code,
                    "observed_sync_gap": attestation.observed_sync_gap,
                    "endpoint_commitment": attestation.endpoint_commitment,
                    "concentration_key": attestation.concentration_key,
                    "signature_hex": attestation.signature_hex,
                }
                for attestation in bundle.attestations
            ],
            sort_keys=True,
            separators=(",", ":"),
        )
        with self.connection:
            self.connection.execute(
                """
                INSERT OR REPLACE INTO reward_attestation_bundles(
                    txid,
                    block_height,
                    epoch_index,
                    bundle_window_index,
                    bundle_submitter_node_id,
                    attestation_count,
                    attestations_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    txid,
                    block_height,
                    bundle.epoch_index,
                    bundle.bundle_window_index,
                    bundle.bundle_submitter_node_id,
                    len(bundle.attestations),
                    raw_attestations_json,
                ),
            )
            self.connection.execute("DELETE FROM reward_attestation_entries WHERE txid = ?", (txid,))
            self.connection.executemany(
                """
                INSERT INTO reward_attestation_entries(
                    txid,
                    bundle_position,
                    epoch_index,
                    check_window_index,
                    candidate_node_id,
                    verifier_node_id,
                    result_code,
                    observed_sync_gap,
                    endpoint_commitment,
                    concentration_key,
                    signature_hex
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        txid,
                        index,
                        attestation.epoch_index,
                        attestation.check_window_index,
                        attestation.candidate_node_id,
                        attestation.verifier_node_id,
                        attestation.result_code,
                        attestation.observed_sync_gap,
                        attestation.endpoint_commitment,
                        attestation.concentration_key,
                        attestation.signature_hex,
                    )
                    for index, attestation in enumerate(bundle.attestations)
                ],
            )

    def list_bundles(self, *, epoch_index: int | None = None) -> list[StoredRewardAttestationBundle]:
        if epoch_index is None:
            rows = self.connection.execute(
                """
                SELECT txid, block_height, epoch_index, bundle_window_index, bundle_submitter_node_id, attestation_count, attestations_json
                FROM reward_attestation_bundles
                ORDER BY block_height, epoch_index, bundle_window_index, txid
                """
            ).fetchall()
        else:
            rows = self.connection.execute(
                """
                SELECT txid, block_height, epoch_index, bundle_window_index, bundle_submitter_node_id, attestation_count, attestations_json
                FROM reward_attestation_bundles
                WHERE epoch_index = ?
                ORDER BY block_height, epoch_index, bundle_window_index, txid
                """,
                (epoch_index,),
            ).fetchall()
        stored: list[StoredRewardAttestationBundle] = []
        for row in rows:
            bundle = parse_reward_attestation_bundle_metadata(
                {
                    "epoch_index": str(row["epoch_index"]),
                    "bundle_window_index": str(row["bundle_window_index"]),
                    "bundle_submitter_node_id": str(row["bundle_submitter_node_id"]),
                    "attestation_count": str(row["attestation_count"]),
                    "attestations_json": str(row["attestations_json"]),
                }
            )
            stored.append(
                StoredRewardAttestationBundle(
                    txid=str(row["txid"]),
                    block_height=int(row["block_height"]),
                    bundle=bundle,
                )
            )
        return stored

    def attestation_identities(self) -> set[tuple[int, int, str, str]]:
        rows = self.connection.execute(
            """
            SELECT epoch_index, check_window_index, candidate_node_id, verifier_node_id
            FROM reward_attestation_entries
            """
        ).fetchall()
        return {
            (
                int(row["epoch_index"]),
                int(row["check_window_index"]),
                str(row["candidate_node_id"]),
                str(row["verifier_node_id"]),
            )
            for row in rows
        }

    def replace_all(self, bundles: list[StoredRewardAttestationBundle]) -> None:
        with self.connection:
            self.connection.execute("DELETE FROM reward_attestation_entries")
            self.connection.execute("DELETE FROM reward_attestation_bundles")
        for stored in bundles:
            self.add_bundle(txid=stored.txid, block_height=stored.block_height, bundle=stored.bundle)


class SQLiteEpochSettlementRepository:
    """SQLite-backed repository for native epoch settlement payloads."""

    def __init__(self, connection: Connection) -> None:
        self.connection = connection

    def add_settlement(self, *, txid: str, block_height: int, settlement: RewardSettlement) -> None:
        raw_reward_entries_json = json.dumps(
            [
                {
                    "node_id": entry.node_id,
                    "payout_address": entry.payout_address,
                    "reward_chipbits": entry.reward_chipbits,
                    "selection_rank": entry.selection_rank,
                    "concentration_key": entry.concentration_key,
                    "final_confirmation_passed": entry.final_confirmation_passed,
                }
                for entry in settlement.reward_entries
            ],
            sort_keys=True,
            separators=(",", ":"),
        )
        with self.connection:
            self.connection.execute(
                """
                INSERT OR REPLACE INTO epoch_settlements(
                    txid,
                    block_height,
                    epoch_index,
                    epoch_start_height,
                    epoch_end_height,
                    epoch_seed_hex,
                    policy_version,
                    submission_mode,
                    candidate_summary_root,
                    verified_nodes_root,
                    rewarded_nodes_root,
                    rewarded_node_count,
                    distributed_node_reward_chipbits,
                    undistributed_node_reward_chipbits,
                    reward_entries_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    txid,
                    block_height,
                    settlement.epoch_index,
                    settlement.epoch_start_height,
                    settlement.epoch_end_height,
                    settlement.epoch_seed_hex,
                    settlement.policy_version,
                    settlement.submission_mode,
                    settlement.candidate_summary_root,
                    settlement.verified_nodes_root,
                    settlement.rewarded_nodes_root,
                    settlement.rewarded_node_count,
                    settlement.distributed_node_reward_chipbits,
                    settlement.undistributed_node_reward_chipbits,
                    raw_reward_entries_json,
                ),
            )
            self.connection.execute("DELETE FROM epoch_settlement_entries WHERE txid = ?", (txid,))
            self.connection.executemany(
                """
                INSERT INTO epoch_settlement_entries(
                    txid,
                    selection_rank,
                    node_id,
                    payout_address,
                    reward_chipbits,
                    concentration_key,
                    final_confirmation_passed
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        txid,
                        entry.selection_rank,
                        entry.node_id,
                        entry.payout_address,
                        entry.reward_chipbits,
                        entry.concentration_key,
                        1 if entry.final_confirmation_passed else 0,
                    )
                    for entry in settlement.reward_entries
                ],
            )

    def list_settlements(self, *, epoch_index: int | None = None) -> list[StoredEpochSettlement]:
        if epoch_index is None:
            rows = self.connection.execute(
                """
                SELECT *
                FROM epoch_settlements
                ORDER BY block_height, epoch_index, txid
                """
            ).fetchall()
        else:
            rows = self.connection.execute(
                """
                SELECT *
                FROM epoch_settlements
                WHERE epoch_index = ?
                ORDER BY block_height, epoch_index, txid
                """,
                (epoch_index,),
            ).fetchall()
        stored: list[StoredEpochSettlement] = []
        for row in rows:
            settlement = parse_reward_settlement_metadata(
                {
                    "epoch_index": str(row["epoch_index"]),
                    "epoch_start_height": str(row["epoch_start_height"]),
                    "epoch_end_height": str(row["epoch_end_height"]),
                    "epoch_seed": str(row["epoch_seed_hex"]),
                    "policy_version": str(row["policy_version"]),
                    "submission_mode": str(row["submission_mode"]),
                    "candidate_summary_root": str(row["candidate_summary_root"]),
                    "verified_nodes_root": str(row["verified_nodes_root"]),
                    "rewarded_nodes_root": str(row["rewarded_nodes_root"]),
                    "rewarded_node_count": str(row["rewarded_node_count"]),
                    "distributed_node_reward_chipbits": str(row["distributed_node_reward_chipbits"]),
                    "undistributed_node_reward_chipbits": str(row["undistributed_node_reward_chipbits"]),
                    "reward_entries_json": str(row["reward_entries_json"]),
                }
            )
            stored.append(
                StoredEpochSettlement(
                    txid=str(row["txid"]),
                    block_height=int(row["block_height"]),
                    settlement=settlement,
                )
            )
        return stored

    def settled_epoch_indexes(self) -> set[int]:
        rows = self.connection.execute("SELECT epoch_index FROM epoch_settlements").fetchall()
        return {int(row["epoch_index"]) for row in rows}

    def total_distributed_node_reward_chipbits(self) -> int:
        row = self.connection.execute(
            """
            SELECT COALESCE(SUM(distributed_node_reward_chipbits), 0) AS total
            FROM epoch_settlements
            """
        ).fetchone()
        return 0 if row is None else int(row["total"])

    def replace_all(self, settlements: list[StoredEpochSettlement]) -> None:
        with self.connection:
            self.connection.execute("DELETE FROM epoch_settlement_entries")
            self.connection.execute("DELETE FROM epoch_settlements")
        for stored in settlements:
            self.add_settlement(txid=stored.txid, block_height=stored.block_height, settlement=stored.settlement)


def settlement_reward_total_chipbits(settlement: RewardSettlement) -> int:
    """Return total distributed reward in one settlement payload."""

    return sum(entry.reward_chipbits for entry in settlement.reward_entries)


def bundle_attestation_identities(bundle: RewardAttestationBundle) -> set[tuple[int, int, str, str]]:
    """Return attestation identities for one bundle."""

    return {attestation_identity(attestation) for attestation in bundle.attestations}
