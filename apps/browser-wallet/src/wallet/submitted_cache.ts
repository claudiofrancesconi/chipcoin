import type { SubmittedTransactionRecord, SubmittedTransactionState } from "../state/app_state";
import type { HistoryEntry, TxLookup } from "../api/types";
import { SUBMITTED_TX_POLL_BACKOFF_MS } from "../shared/constants";

export function upsertSubmittedTransaction(
  entries: SubmittedTransactionRecord[],
  record: SubmittedTransactionRecord,
): SubmittedTransactionRecord[] {
  const filtered = entries.filter((entry) => entry.txid !== record.txid);
  return [record, ...filtered].sort((left, right) => right.submittedAt - left.submittedAt);
}

export function updateSubmittedTransactionState(
  entries: SubmittedTransactionRecord[],
  txid: string,
  status: SubmittedTransactionState,
  errorMessage?: string,
): SubmittedTransactionRecord[] {
  return entries.map((entry) => (
    entry.txid === txid
      ? { ...entry, status, errorMessage }
      : entry
  ));
}

export function nextSubmittedPollAt(entry: SubmittedTransactionRecord, now = Date.now()): number {
  const attempts = entry.pollAttempts ?? 0;
  const index = Math.min(attempts, SUBMITTED_TX_POLL_BACKOFF_MS.length - 1);
  return now + SUBMITTED_TX_POLL_BACKOFF_MS[index];
}

export function markSubmittedTransactionChecked(
  entries: SubmittedTransactionRecord[],
  txid: string,
  now = Date.now(),
): SubmittedTransactionRecord[] {
  return entries.map((entry) => (
    entry.txid === txid && entry.status === "submitted"
      ? {
          ...entry,
          lastCheckedAt: now,
          pollAttempts: (entry.pollAttempts ?? 0) + 1,
          nextCheckAt: nextSubmittedPollAt({
            ...entry,
            pollAttempts: (entry.pollAttempts ?? 0) + 1,
          }, now),
        }
      : entry
  ));
}

export function markSubmittedTransactionConfirmed(
  entries: SubmittedTransactionRecord[],
  txid: string,
  now = Date.now(),
): SubmittedTransactionRecord[] {
  return entries.map((entry) => (
    entry.txid === txid
      ? {
          ...entry,
          status: "confirmed",
          confirmedAt: now,
          lastCheckedAt: now,
          nextCheckAt: undefined,
          pollAttempts: entry.pollAttempts ?? 0,
          errorMessage: undefined,
        }
      : entry
  ));
}

export function createSubmittedTransactionRecord(
  record: Omit<SubmittedTransactionRecord, "status" | "pollAttempts" | "lastCheckedAt" | "nextCheckAt" | "confirmedAt">,
  now = Date.now(),
): SubmittedTransactionRecord {
  return {
    ...record,
    status: "submitted",
    lastCheckedAt: undefined,
    pollAttempts: 0,
    nextCheckAt: nextSubmittedPollAt({
      ...record,
      status: "submitted",
      pollAttempts: 0,
    }, now),
  };
}

export function isConfirmedTxLookup(payload: TxLookup): boolean {
  return payload.location === "chain" || payload.block_hash !== null || payload.height !== null;
}

export function dedupeConfirmedHistory(
  history: HistoryEntry[],
  submittedTransactions: SubmittedTransactionRecord[],
): HistoryEntry[] {
  const locallyTrackedTxids = new Set(submittedTransactions.map((entry) => entry.txid));
  return history.filter((entry) => !locallyTrackedTxids.has(entry.txid));
}
