import { ChipcoinApiClient } from "../api/client";
import { ApiClientError } from "../api/errors";
import type { AddressSummary, AddressUtxo, HistoryEntry } from "../api/types";
import { privateKeyHexToAddress } from "../crypto/addresses";
import { decryptPrivateKeyHex, encryptPrivateKeyHex } from "../crypto/encryption";
import { buildWalletKeyMaterial, generatePrivateKeyHex, normalizePrivateKeyHex } from "../crypto/keys";
import { buildSignedPaymentTransaction } from "../wallet/build_transaction";
import {
  createSubmittedTransactionRecord,
  dedupeConfirmedHistory,
  isConfirmedTxLookup,
  markSubmittedTransactionChecked,
  markSubmittedTransactionConfirmed,
  upsertSubmittedTransaction,
} from "../wallet/submitted_cache";
import { extensionAlarms } from "../shared/browser";
import {
  API_TIMEOUTS_MS,
  DEFAULT_AUTO_LOCK_MINUTES,
  SUBMITTED_TX_POLL_ALARM,
  WALLET_FORMAT_VERSION,
} from "../shared/constants";
import { minutesToMilliseconds } from "../shared/time";
import { normalizeNodeEndpoint, requireMinPasswordLength } from "../shared/validation";
import type {
  AppState,
  EncryptedWalletRecord,
  SubmittedTransactionRecord,
  UnlockedSession,
  WalletDataCache,
  WalletOverviewState,
  WalletSettings,
} from "../state/app_state";
import { loadSettings, saveSettings } from "../storage/preferences_store";
import { clearSubmittedTransactions, loadSubmittedTransactions, saveSubmittedTransactions } from "../storage/session_store";
import { clearWalletDataCache, loadWalletDataCache, saveWalletDataCache } from "../storage/wallet_data_store";
import { clearWalletRecord, loadWalletRecord, saveWalletRecord } from "../storage/wallet_store";
import { AUTO_LOCK_ALARM } from "./alarms";

let activeSession: UnlockedSession | null = null;

export async function initializeBackground(): Promise<void> {
  const [walletRecord, settings] = await Promise.all([loadWalletRecord(), loadSettings()]);
  if (!walletRecord) {
    extensionAlarms().clear(SUBMITTED_TX_POLL_ALARM);
    await clearWalletDataCache();
    return;
  }
  await reconcileSubmittedTransactions(settings, walletRecord.address, { forceCheckAll: true });
  await refreshWalletDataCache(settings, walletRecord.address, { includeHistory: false });
}

export async function createWallet(password: string): Promise<AppState> {
  requireMinPasswordLength(password);
  const privateKeyHex = generatePrivateKeyHex();
  return persistNewWallet(privateKeyHex, password);
}

export async function importWallet(privateKeyHex: string, password: string): Promise<AppState> {
  requireMinPasswordLength(password);
  return persistNewWallet(normalizePrivateKeyHex(privateKeyHex), password);
}

export async function unlockWallet(password: string): Promise<AppState> {
  const record = await requireWalletRecord();
  const privateKeyHex = await decryptPrivateKeyHex(
    record.encryptedWalletBlob,
    password,
    record.saltBase64,
    record.ivBase64,
    record.iterations,
  );
  const settings = await loadSettings();
  activeSession = makeUnlockedSession(privateKeyHex, settings.autoLockMinutes);
  await scheduleAutoLock(settings.autoLockMinutes);
  await reconcileSubmittedTransactions(settings, activeSession.address, { forceCheckAll: true });
  await refreshWalletDataCache(settings, activeSession.address, { includeHistory: false });
  return getAppState();
}

export async function lockWallet(): Promise<AppState> {
  activeSession = null;
  extensionAlarms().clear(AUTO_LOCK_ALARM);
  return getAppState();
}

export async function removeWallet(): Promise<AppState> {
  activeSession = null;
  extensionAlarms().clear(AUTO_LOCK_ALARM);
  extensionAlarms().clear(SUBMITTED_TX_POLL_ALARM);
  await clearWalletRecord();
  await clearSubmittedTransactions();
  await clearWalletDataCache();
  return getAppState();
}

export async function exportPrivateKey(args: { password?: string; confirmActiveSession?: boolean }): Promise<string> {
  if (activeSession) {
    if (!args.confirmActiveSession) {
      throw new Error("Explicit confirmation is required before revealing the private key.");
    }
    await touchSession();
    return activeSession.privateKeyHex;
  }
  if (!args.password) {
    throw new Error("Password is required to export the private key while locked.");
  }
  const record = await requireWalletRecord();
  return decryptPrivateKeyHex(record.encryptedWalletBlob, args.password, record.saltBase64, record.ivBase64, record.iterations);
}

export async function updateNodeEndpoint(nodeApiBaseUrl: string): Promise<AppState> {
  await touchSession();
  const settings = await loadSettings();
  const normalized = normalizeNodeEndpoint(nodeApiBaseUrl);
  const client = ChipcoinApiClient.fromBaseUrl(normalized);
  await client.health();
  const status = await client.status();
  if (status.network !== settings.expectedNetwork) {
    throw new Error(`Wrong network. Expected ${settings.expectedNetwork}, got ${status.network}.`);
  }
  const nextSettings = { ...settings, nodeApiBaseUrl: normalized };
  await saveSettings(nextSettings);
  const walletRecord = await loadWalletRecord();
  if (walletRecord) {
    await reconcileSubmittedTransactions(nextSettings, walletRecord.address, { forceCheckAll: true });
    await refreshWalletDataCache(nextSettings, walletRecord.address, { includeHistory: false });
  }
  return getAppState();
}

export async function refreshWalletData(): Promise<AppState> {
  await touchSession();
  const settings = await loadSettings();
  if (activeSession) {
    await reconcileSubmittedTransactions(settings, activeSession.address, { forceCheckAll: true });
    await refreshWalletDataCache(settings, activeSession.address, { includeHistory: false });
  }
  return getAppState();
}

export async function getWalletHistory(): Promise<WalletOverviewState["history"]> {
  await touchSession();
  const [walletRecord, settings, submittedTransactions] = await Promise.all([
    loadWalletRecord(),
    loadSettings(),
    loadSubmittedTransactions(),
  ]);
  if (!walletRecord) {
    return [];
  }
  const cache = await refreshWalletDataCache(settings, walletRecord.address, { includeHistory: true });
  return dedupeConfirmedHistory(cache.history, submittedTransactions);
}

export async function submitTransaction(args: {
  recipient: string;
  amountChipbits: number;
  feeChipbits: number;
}): Promise<{ status: "submitted" | "rejected" | "failed_to_submit"; txid?: string }> {
  if (!activeSession) {
    throw new Error("Unlock the wallet before sending transactions.");
  }
  await touchSession();
  const settings = await loadSettings();
  const client = ChipcoinApiClient.fromBaseUrl(settings.nodeApiBaseUrl);
  let built: ReturnType<typeof buildSignedPaymentTransaction> | null = null;

  try {
    const utxos = await client.utxos(activeSession.address);
    built = buildSignedPaymentTransaction({
      privateKeyHex: activeSession.privateKeyHex,
      walletAddress: activeSession.address,
      recipient: args.recipient,
      amountChipbits: args.amountChipbits,
      feeChipbits: args.feeChipbits,
      utxos,
    });
    await client.submitRawTransaction(built.rawHex);
    await rememberSubmittedTransaction(createSubmittedTransactionRecord({
      txid: built.txid,
      submittedAt: Date.now(),
      recipient: args.recipient,
      amountChipbits: args.amountChipbits,
      feeChipbits: args.feeChipbits,
    }));
    await refreshWalletDataCache(settings, activeSession.address, { includeHistory: true });
    await scheduleSubmittedTransactionPolling();
    return { status: "submitted", txid: built.txid };
  } catch (error) {
    if (built && error instanceof ApiClientError && error.code === "validation_error") {
      await rememberSubmittedTransaction({
        txid: built.txid,
        submittedAt: Date.now(),
        recipient: args.recipient,
        amountChipbits: args.amountChipbits,
        feeChipbits: args.feeChipbits,
        status: "rejected",
        errorMessage: error.message,
      });
      return { status: "rejected", txid: built.txid };
    }
    if (built) {
      await rememberSubmittedTransaction({
        txid: built.txid,
        submittedAt: Date.now(),
        recipient: args.recipient,
        amountChipbits: args.amountChipbits,
        feeChipbits: args.feeChipbits,
        status: "failed_to_submit",
        errorMessage: error instanceof Error ? error.message : "Unable to submit transaction.",
      });
    }
    return { status: "failed_to_submit", txid: built?.txid };
  }
}

export async function getAppState(): Promise<AppState> {
  await touchSession();
  const [walletRecord, settings, submittedTransactions, walletDataCache] = await Promise.all([
    loadWalletRecord(),
    loadSettings(),
    loadSubmittedTransactions(),
    loadWalletDataCache(),
  ]);

  const overview = await buildOverview(walletRecord, settings, submittedTransactions, walletDataCache);
  return {
    hasWallet: walletRecord !== null,
    isLocked: activeSession === null,
    address: walletRecord?.address ?? null,
    nodeApiBaseUrl: settings.nodeApiBaseUrl,
    expectedNetwork: settings.expectedNetwork,
    autoLockMinutes: settings.autoLockMinutes,
    nodeStatus: overview.status,
    overview,
  };
}

export async function handleAutoLockAlarm(name: string): Promise<void> {
  if (name === AUTO_LOCK_ALARM) {
    activeSession = null;
    return;
  }
  if (name === SUBMITTED_TX_POLL_ALARM) {
    const [walletRecord, settings] = await Promise.all([loadWalletRecord(), loadSettings()]);
    if (!walletRecord) {
      extensionAlarms().clear(SUBMITTED_TX_POLL_ALARM);
      return;
    }
    await reconcileSubmittedTransactions(settings, walletRecord.address, { forceCheckAll: false });
  }
}

async function persistNewWallet(privateKeyHex: string, password: string): Promise<AppState> {
  const keyMaterial = buildWalletKeyMaterial(privateKeyHex);
  const encrypted = await encryptPrivateKeyHex(privateKeyHex, password);
  const record: EncryptedWalletRecord = {
    walletFormatVersion: WALLET_FORMAT_VERSION,
    address: privateKeyHexToAddress(privateKeyHex),
    publicKeyHex: keyMaterial.publicKeyHex,
    createdAt: Date.now(),
    ...encrypted,
  };
  extensionAlarms().clear(SUBMITTED_TX_POLL_ALARM);
  await clearSubmittedTransactions();
  await clearWalletDataCache();
  await saveWalletRecord(record);
  const settings = await loadSettings();
  activeSession = makeUnlockedSession(privateKeyHex, settings.autoLockMinutes);
  await scheduleAutoLock(settings.autoLockMinutes);
  await refreshWalletDataCache(settings, record.address, { includeHistory: false });
  return getAppState();
}

function makeUnlockedSession(privateKeyHex: string, autoLockMinutes: number): UnlockedSession {
  const keyMaterial = buildWalletKeyMaterial(privateKeyHex);
  const now = Date.now();
  return {
    privateKeyHex,
    publicKeyHex: keyMaterial.publicKeyHex,
    address: privateKeyHexToAddress(privateKeyHex),
    unlockedAt: now,
    expiresAt: now + minutesToMilliseconds(autoLockMinutes || DEFAULT_AUTO_LOCK_MINUTES),
  };
}

async function scheduleAutoLock(autoLockMinutes: number): Promise<void> {
  extensionAlarms().clear(AUTO_LOCK_ALARM);
  extensionAlarms().create(AUTO_LOCK_ALARM, { delayInMinutes: autoLockMinutes || DEFAULT_AUTO_LOCK_MINUTES });
}

async function touchSession(): Promise<void> {
  if (!activeSession) {
    return;
  }
  const settings = await loadSettings();
  const expiresAt = Date.now() + minutesToMilliseconds(settings.autoLockMinutes || DEFAULT_AUTO_LOCK_MINUTES);
  activeSession = {
    ...activeSession,
    expiresAt,
  };
  await scheduleAutoLock(settings.autoLockMinutes);
}

async function buildOverview(
  walletRecord: EncryptedWalletRecord | null,
  settings: WalletSettings,
  submittedTransactions: SubmittedTransactionRecord[],
  walletDataCache: WalletDataCache,
): Promise<WalletOverviewState> {
  if (!walletRecord) {
    return {
      summary: null,
      utxos: [],
      history: [],
      status: null,
      submittedTransactions,
    };
  }

  const client = ChipcoinApiClient.fromBaseUrl(settings.nodeApiBaseUrl);
  const status = await withFallback(client.status(), null);
  return {
    summary: walletDataCache.summary,
    utxos: walletDataCache.utxos,
    history: dedupeConfirmedHistory(walletDataCache.history, submittedTransactions),
    status,
    submittedTransactions,
  };
}

async function refreshWalletDataCache(
  settings: WalletSettings,
  address: string,
  options: { includeHistory: boolean },
): Promise<WalletDataCache> {
  const client = ChipcoinApiClient.fromBaseUrl(settings.nodeApiBaseUrl);
  const previous = await loadWalletDataCache();
  const [summary, utxos, history] = await Promise.all([
    withFallback<AddressSummary | null>(client.address(address), previous.summary),
    withFallback<AddressUtxo[]>(client.utxos(address), previous.utxos),
    options.includeHistory
      ? withFallback<HistoryEntry[]>(client.history(address, 50, API_TIMEOUTS_MS.history), previous.history)
      : Promise.resolve(previous.history),
  ]);
  const next: WalletDataCache = {
    summary,
    utxos,
    history,
    updatedAt: Date.now(),
  };
  await saveWalletDataCache(next);
  return next;
}

async function reconcileSubmittedTransactions(
  settings: WalletSettings,
  address: string,
  options: { forceCheckAll: boolean },
): Promise<void> {
  const submittedTransactions = await loadSubmittedTransactions();
  if (submittedTransactions.length === 0) {
    extensionAlarms().clear(SUBMITTED_TX_POLL_ALARM);
    return;
  }

  const client = ChipcoinApiClient.fromBaseUrl(settings.nodeApiBaseUrl);
  const now = Date.now();
  let next = submittedTransactions;
  let didConfirmAny = false;

  for (const entry of submittedTransactions) {
    if (entry.status !== "submitted") {
      continue;
    }
    if (!options.forceCheckAll && entry.nextCheckAt && entry.nextCheckAt > now) {
      continue;
    }
    try {
      const transaction = await client.tx(entry.txid);
      if (isConfirmedTxLookup(transaction)) {
        next = markSubmittedTransactionConfirmed(next, entry.txid, now);
        didConfirmAny = true;
      } else {
        next = markSubmittedTransactionChecked(next, entry.txid, now);
      }
    } catch {
      next = markSubmittedTransactionChecked(next, entry.txid, now);
    }
  }

  await saveSubmittedTransactions(next);
  if (didConfirmAny) {
    await refreshWalletDataCache(settings, address, { includeHistory: true });
  }
  await scheduleSubmittedTransactionPolling();
}

async function scheduleSubmittedTransactionPolling(): Promise<void> {
  const submittedTransactions = await loadSubmittedTransactions();
  const nextChecks = submittedTransactions
    .filter((entry) => entry.status === "submitted")
    .map((entry) => entry.nextCheckAt ?? Date.now());
  if (nextChecks.length === 0) {
    extensionAlarms().clear(SUBMITTED_TX_POLL_ALARM);
    return;
  }
  extensionAlarms().create(SUBMITTED_TX_POLL_ALARM, {
    when: Math.max(Date.now() + 1_000, Math.min(...nextChecks)),
  });
}

async function withFallback<T>(promise: Promise<T>, fallback: T): Promise<T> {
  try {
    return await promise;
  } catch {
    return fallback;
  }
}

async function requireWalletRecord(): Promise<EncryptedWalletRecord> {
  const record = await loadWalletRecord();
  if (!record) {
    throw new Error("No wallet is configured yet.");
  }
  return record;
}

export async function rememberSubmittedTransaction(record: SubmittedTransactionRecord): Promise<void> {
  const current = await loadSubmittedTransactions();
  await saveSubmittedTransactions(upsertSubmittedTransaction(current, record));
}
