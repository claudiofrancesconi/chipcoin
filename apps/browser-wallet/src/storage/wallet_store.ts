import { STORAGE_KEYS, WALLET_FORMAT_VERSION } from "../shared/constants";
import { storageGet, storageRemove, storageSet } from "../shared/browser";
import type { EncryptedWalletRecord } from "../state/app_state";

export async function loadWalletRecord(): Promise<EncryptedWalletRecord | null> {
  return (await storageGet<EncryptedWalletRecord>(STORAGE_KEYS.wallet)) ?? null;
}

export async function saveWalletRecord(record: EncryptedWalletRecord): Promise<void> {
  await storageSet(STORAGE_KEYS.wallet, {
    ...record,
    walletFormatVersion: record.walletFormatVersion ?? WALLET_FORMAT_VERSION,
  });
}

export async function clearWalletRecord(): Promise<void> {
  await storageRemove(STORAGE_KEYS.wallet);
}
