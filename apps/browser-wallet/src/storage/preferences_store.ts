import { DEFAULT_AUTO_LOCK_MINUTES, DEFAULT_NODE_ENDPOINT, EXPECTED_NETWORK, STORAGE_KEYS } from "../shared/constants";
import { storageGet, storageSet } from "../shared/browser";
import type { WalletSettings } from "../state/app_state";

const DEFAULT_SETTINGS: WalletSettings = {
  nodeApiBaseUrl: DEFAULT_NODE_ENDPOINT,
  expectedNetwork: EXPECTED_NETWORK,
  autoLockMinutes: DEFAULT_AUTO_LOCK_MINUTES,
};

export async function loadSettings(): Promise<WalletSettings> {
  const saved = await storageGet<Partial<WalletSettings>>(STORAGE_KEYS.settings);
  return {
    ...DEFAULT_SETTINGS,
    ...saved,
  };
}

export async function saveSettings(settings: WalletSettings): Promise<void> {
  await storageSet(STORAGE_KEYS.settings, settings);
}
