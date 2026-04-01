export const EXPECTED_NETWORK = "devnet";
export const MIN_PASSWORD_LENGTH = 10;
export const DEFAULT_AUTO_LOCK_MINUTES = 15;
export const DEFAULT_NODE_ENDPOINT = "http://127.0.0.1:8081";
export const WALLET_FORMAT_VERSION = 1;
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
} as const;
