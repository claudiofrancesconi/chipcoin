import { STORAGE_KEYS } from "../shared/constants";
import { storageGet, storageRemove, storageSet } from "../shared/browser";
import type { SubmittedTransactionRecord } from "../state/app_state";

export async function loadSubmittedTransactions(): Promise<SubmittedTransactionRecord[]> {
  return (await storageGet<SubmittedTransactionRecord[]>(STORAGE_KEYS.submittedTransactions)) ?? [];
}

export async function saveSubmittedTransactions(entries: SubmittedTransactionRecord[]): Promise<void> {
  await storageSet(STORAGE_KEYS.submittedTransactions, entries);
}

export async function clearSubmittedTransactions(): Promise<void> {
  await storageRemove(STORAGE_KEYS.submittedTransactions);
}
