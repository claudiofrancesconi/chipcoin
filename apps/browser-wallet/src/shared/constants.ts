export type SupportedNetworkId = "devnet" | "testnet";

export interface SupportedNetworkConfig {
  id: SupportedNetworkId;
  label: string;
  defaultEndpointLabel: string;
  defaultNodeApiBaseUrl: string;
  localNodeApiBaseUrl?: string;
  description: string;
  statusLabel: string;
  httpSafetyNote: string;
}

export const MIN_PASSWORD_LENGTH = 10;
export const DEFAULT_AUTO_LOCK_MINUTES = 30;
export const AUTO_LOCK_MINUTES_OPTIONS = [5, 15, 30, 60, 120] as const;
declare const __CHIPCOIN_DEFAULT_NODE_ENDPOINT__: string;
declare const __CHIPCOIN_DEFAULT_EXPLORER_URL__: string;
export const DEFAULT_NODE_ENDPOINT = __CHIPCOIN_DEFAULT_NODE_ENDPOINT__;
export const DEFAULT_EXPLORER_URL = __CHIPCOIN_DEFAULT_EXPLORER_URL__;
export const DEFAULT_NETWORK: SupportedNetworkId = "testnet";
export const TESTNET_PQ_ACTIVATION_HEIGHT = 20_000;
export const ENABLE_EXPERIMENTAL_BROWSER_MLDSA = false;
export const SUPPORTED_NETWORKS: readonly SupportedNetworkConfig[] = [
  {
    id: "devnet",
    label: "Devnet",
    defaultEndpointLabel: "Public Devnet API",
    defaultNodeApiBaseUrl: DEFAULT_NODE_ENDPOINT,
    description: "Legacy public devnet/fallback environment.",
    statusLabel: "legacy devnet",
    httpSafetyNote: "Public devnet endpoint is provided for convenience and may change.",
  },
  {
    id: "testnet",
    label: "Testnet",
    defaultEndpointLabel: "Public Testnet API",
    defaultNodeApiBaseUrl: "https://testnet-api.chipcoinprotocol.com",
    localNodeApiBaseUrl: "http://127.0.0.1:28081",
    description: "Public testnet candidate. Uses the wallet-safe public API by default; operators can switch to a local node API.",
    statusLabel: "public testnet candidate",
    httpSafetyNote: "The public testnet API only allows wallet-safe reads and transaction submit. Keep raw node HTTP local/private and do not use the readonly explorer API for wallet submissions.",
  },
] as const;
export const WALLET_FORMAT_VERSION = 2;
export const SUBMITTED_TX_POLL_ALARM = "chipcoin-submitted-tx-poll";
export const SUBMITTED_TX_POLL_BACKOFF_MS = [
  15_000,
  30_000,
  60_000,
  120_000,
  300_000,
  600_000,
] as const;
export const API_TIMEOUTS_MS = {
  health: 2_000,
  status: 20_000,
  summary: 20_000,
  utxos: 5_000,
  history: 5_000,
  txLookup: 10_000,
  txSubmit: 20_000,
} as const;
export const STORAGE_KEYS = {
  wallet: "chipcoin.wallet",
  settings: "chipcoin.settings",
  submittedTransactions: "chipcoin.submittedTransactions",
  walletDataCache: "chipcoin.walletDataCache",
  watchOnlyAddresses: "chipcoin.watchOnlyAddresses",
  connectedSites: "chipcoin.connectedSites",
  unlockedSession: "chipcoin.unlockedSession",
} as const;

export function isSupportedNetwork(value: string): value is SupportedNetworkId {
  return SUPPORTED_NETWORKS.some((network) => network.id === value);
}

export function getSupportedNetwork(value: string): SupportedNetworkConfig {
  const network = SUPPORTED_NETWORKS.find((entry) => entry.id === value);
  if (!network) {
    throw new Error(`Unsupported Chipcoin network: ${value}.`);
  }
  return network;
}

export function networkScopedStorageKey(baseKey: string, network: SupportedNetworkId): string {
  return `${baseKey}.${network}`;
}
