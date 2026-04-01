import { STORAGE_KEYS } from "../shared/constants";
import { storageGet, storageRemove, storageSet } from "../shared/browser";
import type { WalletDataCache } from "../state/app_state";

const EMPTY_WALLET_DATA_CACHE: WalletDataCache = {
  summary: null,
  utxos: [],
  history: [],
  updatedAt: null,
};

export async function loadWalletDataCache(): Promise<WalletDataCache> {
  return (await storageGet<WalletDataCache>(STORAGE_KEYS.walletDataCache)) ?? EMPTY_WALLET_DATA_CACHE;
}

export async function saveWalletDataCache(cache: WalletDataCache): Promise<void> {
  await storageSet(STORAGE_KEYS.walletDataCache, cache);
}

export async function clearWalletDataCache(): Promise<void> {
  await storageRemove(STORAGE_KEYS.walletDataCache);
}
