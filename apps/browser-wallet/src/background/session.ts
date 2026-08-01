import { ChipcoinApiClient } from "../api/client";
import { ApiClientError } from "../api/errors";
import type { AddressSummary, AddressUtxo, HistoryEntry } from "../api/types";
import { privateKeyHexToAddress } from "../crypto/addresses";
import { decryptPrivateKeyHex, decryptWalletSecret, encryptPrivateKeyHex, encryptWalletSecret } from "../crypto/encryption";
import { buildWalletKeyMaterial, generatePrivateKeyHex, normalizePrivateKeyHex } from "../crypto/keys";
import {
  assertPublicKeyMatchesAddress,
  parseChipcoinSignedLoginMessage,
  signLoginMessage,
  validateLoginOriginBinding,
} from "../provider/login_message";
import type { ChipcoinSignedLoginResponse } from "../provider/types";
import {
  RECOVERY_PHRASE_WORD_COUNT,
  derivePrivateKeyHexFromRecoveryPhrase,
  generateRecoveryPhrase,
  validateRecoveryPhrase,
} from "../crypto/recovery_phrase";
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
  getSupportedNetwork,
  type SupportedNetworkId,
} from "../shared/constants";
import { minutesToMilliseconds } from "../shared/time";
import { normalizeNodeEndpoint, requireMinPasswordLength } from "../shared/validation";
import { validateWatchOnlyAddress } from "../shared/address_scheme";
import type {
  AppState,
  EncryptedWalletRecord,
  SubmittedTransactionRecord,
  UnlockedSession,
  WalletDataCache,
  WalletOverviewState,
  WalletSettings,
  WatchOnlyAddressRecord,
  WatchOnlyAddressState,
} from "../state/app_state";
import { loadSettings, saveSettings } from "../storage/preferences_store";
import {
  clearAllSubmittedTransactions,
  loadSubmittedTransactions,
  saveSubmittedTransactions,
} from "../storage/session_store";
import {
  clearAllWalletDataCaches,
  loadWalletDataCache,
  saveWalletDataCache,
} from "../storage/wallet_data_store";
import { clearWalletRecord, loadWalletRecord, saveWalletRecord } from "../storage/wallet_store";
import {
  clearAllWatchOnlyAddressRecords,
  loadWatchOnlyAddressRecords,
  saveWatchOnlyAddressRecords,
} from "../storage/watch_only_store";
import { AUTO_LOCK_ALARM } from "./alarms";

let activeSession: UnlockedSession | null = null;

export async function initializeBackground(): Promise<void> {
  const [walletRecord, settings] = await Promise.all([loadWalletRecord(), loadSettings()]);
  if (!walletRecord) {
    extensionAlarms().clear(SUBMITTED_TX_POLL_ALARM);
    await clearAllWalletDataCaches();
    return;
  }
  await reconcileSubmittedTransactions(settings, walletRecord.address, { forceCheckAll: true });
  await refreshWalletDataCache(settings, walletRecord.address, { includeHistory: false });
}

export async function createWallet(password: string): Promise<AppState> {
  requireMinPasswordLength(password);
  const privateKeyHex = generatePrivateKeyHex();
  return persistPrivateKeyWallet(normalizePrivateKeyHex(privateKeyHex), password);
}

export function generateWalletRecoveryPhrase(): string {
  return generateRecoveryPhrase();
}

export async function createWalletFromSeed(recoveryPhrase: string, password: string): Promise<AppState> {
  requireMinPasswordLength(password);
  return persistSeedWallet(validateRecoveryPhrase(recoveryPhrase), password, 0);
}

export async function recoverWalletFromSeed(recoveryPhrase: string, password: string): Promise<AppState> {
  requireMinPasswordLength(password);
  return persistSeedWallet(validateRecoveryPhrase(recoveryPhrase), password, 0);
}

export async function importWallet(privateKeyHex: string, password: string): Promise<AppState> {
  requireMinPasswordLength(password);
  return persistPrivateKeyWallet(normalizePrivateKeyHex(privateKeyHex), password);
}

export async function unlockWallet(password: string): Promise<AppState> {
  const record = await requireWalletRecord();
  const secret = await decryptWalletSecret(
    record.encryptedWalletBlob,
    password,
    record.saltBase64,
    record.ivBase64,
    record.iterations,
  );
  const settings = await loadSettings();
  if (secret.walletType === "private_key") {
    if (!secret.privateKeyHex) {
      throw new Error("Wallet payload does not include a private key.");
    }
    activeSession = makeUnlockedSession({ walletType: "private_key", privateKeyHex: secret.privateKeyHex }, settings.autoLockMinutes, record.accountIndex);
  } else {
    if (!secret.recoveryPhrase) {
      throw new Error("Wallet payload does not include a recovery phrase.");
    }
    activeSession = makeUnlockedSession(
      { walletType: "seed_phrase", recoveryPhrase: secret.recoveryPhrase, accountIndex: secret.accountIndex },
      settings.autoLockMinutes,
      record.accountIndex,
    );
  }
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
  await clearAllSubmittedTransactions();
  await clearAllWalletDataCaches();
  await clearAllWatchOnlyAddressRecords();
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

export async function exportRecoveryPhrase(args: { password?: string; confirmActiveSession?: boolean }): Promise<string> {
  if (activeSession?.walletType === "seed_phrase") {
    if (!args.confirmActiveSession) {
      throw new Error("Explicit confirmation is required before revealing the recovery phrase.");
    }
    await touchSession();
    if (!activeSession.recoveryPhrase) {
      throw new Error("Recovery phrase is unavailable for this wallet.");
    }
    return activeSession.recoveryPhrase;
  }
  const record = await requireWalletRecord();
  if (record.walletType !== "seed_phrase") {
    throw new Error("This wallet was imported from a private key and has no recovery phrase.");
  }
  if (!args.password) {
    throw new Error("Password is required to export the recovery phrase while locked.");
  }
  const secret = await decryptWalletSecret(
    record.encryptedWalletBlob,
    args.password,
    record.saltBase64,
    record.ivBase64,
    record.iterations,
  );
  if (!secret.recoveryPhrase) {
    throw new Error("Recovery phrase is unavailable for this wallet.");
  }
  return secret.recoveryPhrase;
}

export async function updateNodeEndpoint(nodeApiBaseUrl: string, expectedNetwork: SupportedNetworkId): Promise<AppState> {
  await touchSession();
  const settings = await loadSettings();
  const network = getSupportedNetwork(expectedNetwork);
  const normalized = normalizeNodeEndpoint(nodeApiBaseUrl);
  const client = ChipcoinApiClient.fromBaseUrl(normalized);
  await validateClientNetwork(client, network.id);
  const nextSettings = { ...settings, nodeApiBaseUrl: normalized, expectedNetwork: network.id };
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

export async function addWatchOnlyAddress(args: { address: string; label?: string }): Promise<AppState> {
  await touchSession();
  const settings = await loadSettings();
  const walletRecord = await requireWalletRecord();
  const validation = validateWatchOnlyAddress(args.address);
  if (validation.status !== "watch_only" || !validation.normalizedAddress) {
    throw new Error(validation.error ?? "Only supported CHCQ post-quantum addresses can be added as watch-only.");
  }
  if (validation.normalizedAddress === walletRecord.address) {
    throw new Error("The active wallet address is already managed by this wallet.");
  }

  const current = await loadWatchOnlyAddressRecords(settings.expectedNetwork);
  if (current.some((record) => record.address === validation.normalizedAddress)) {
    throw new Error("This CHCQ watch-only address is already tracked.");
  }

  await saveWatchOnlyAddressRecords([
    ...current,
    {
      address: validation.normalizedAddress,
      label: args.label?.trim() || undefined,
      addedAt: Date.now(),
    },
  ], settings.expectedNetwork);
  return getAppState();
}

export async function removeWatchOnlyAddress(address: string): Promise<AppState> {
  await touchSession();
  const settings = await loadSettings();
  const current = await loadWatchOnlyAddressRecords(settings.expectedNetwork);
  await saveWatchOnlyAddressRecords(
    current.filter((record) => record.address !== address.trim()),
    settings.expectedNetwork,
  );
  return getAppState();
}

export async function getWalletHistory(): Promise<WalletOverviewState["history"]> {
  await touchSession();
  const [walletRecord, settings] = await Promise.all([loadWalletRecord(), loadSettings()]);
  const submittedTransactions = await loadSubmittedTransactions(settings.expectedNetwork);
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
    await validateClientNetwork(client, settings.expectedNetwork);
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
  const [walletRecord, settings] = await Promise.all([loadWalletRecord(), loadSettings()]);
  const [submittedTransactions, walletDataCache, watchOnlyRecords] = await Promise.all([
    loadSubmittedTransactions(settings.expectedNetwork),
    loadWalletDataCache(settings.expectedNetwork),
    loadWatchOnlyAddressRecords(settings.expectedNetwork),
  ]);

  const [overview, watchOnlyAddresses] = await Promise.all([
    buildOverview(walletRecord, settings, submittedTransactions, walletDataCache),
    buildWatchOnlyState(settings, watchOnlyRecords),
  ]);
  return {
    hasWallet: walletRecord !== null,
    isLocked: activeSession === null,
    walletType: walletRecord?.walletType ?? null,
    accountIndex: walletRecord?.accountIndex ?? null,
    recoveryPhraseWordCount: walletRecord?.recoveryPhraseWordCount ?? null,
    address: walletRecord?.address ?? null,
    nodeApiBaseUrl: settings.nodeApiBaseUrl,
    expectedNetwork: settings.expectedNetwork,
    autoLockMinutes: settings.autoLockMinutes,
    nodeStatus: overview.status,
    overview,
    watchOnlyAddresses,
  };
}

export async function getProviderAddress(): Promise<string> {
  const walletRecord = await loadWalletRecord();
  if (!walletRecord) {
    throw new Error("WALLET_NOT_FOUND");
  }
  return walletRecord.address;
}

export async function signProviderLoginMessage(args: {
  message: string;
  origin: string;
  domain?: string;
}): Promise<ChipcoinSignedLoginResponse> {
  const walletRecord = await loadWalletRecord();
  if (!walletRecord) {
    throw new Error("WALLET_NOT_FOUND");
  }
  if (!activeSession) {
    throw new Error("WALLET_LOCKED");
  }
  const settings = await loadSettings();
  if (settings.expectedNetwork !== "testnet") {
    throw new Error("UNSUPPORTED_NETWORK");
  }

  const parsed = parseChipcoinSignedLoginMessage(args.message);
  validateLoginOriginBinding(parsed, args.origin, args.domain);
  if (parsed.address !== activeSession.address || parsed.address !== walletRecord.address) {
    throw new Error("ADDRESS_MISMATCH");
  }
  assertPublicKeyMatchesAddress(activeSession.publicKeyHex, parsed.address);
  await touchSession();
  return {
    address: activeSession.address,
    signature_scheme: 0,
    public_key: activeSession.publicKeyHex,
    signature: signLoginMessage(activeSession.privateKeyHex, args.message),
    message: args.message,
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

async function persistPrivateKeyWallet(privateKeyHex: string, password: string): Promise<AppState> {
  const keyMaterial = buildWalletKeyMaterial(privateKeyHex);
  const encrypted = await encryptPrivateKeyHex(privateKeyHex, password);
  const record: EncryptedWalletRecord = {
    walletFormatVersion: WALLET_FORMAT_VERSION,
    walletType: "private_key",
    address: privateKeyHexToAddress(privateKeyHex),
    publicKeyHex: keyMaterial.publicKeyHex,
    accountIndex: 0,
    createdAt: Date.now(),
    ...encrypted,
  };
  extensionAlarms().clear(SUBMITTED_TX_POLL_ALARM);
  await clearAllSubmittedTransactions();
  await clearAllWalletDataCaches();
  await saveWalletRecord(record);
  const settings = await loadSettings();
  activeSession = makeUnlockedSession({ walletType: "private_key", privateKeyHex }, settings.autoLockMinutes, 0);
  await scheduleAutoLock(settings.autoLockMinutes);
  await refreshWalletDataCache(settings, record.address, { includeHistory: false });
  return getAppState();
}

async function persistSeedWallet(recoveryPhrase: string, password: string, accountIndex: number): Promise<AppState> {
  const privateKeyHex = derivePrivateKeyHexFromRecoveryPhrase(recoveryPhrase, accountIndex);
  const keyMaterial = buildWalletKeyMaterial(privateKeyHex);
  const encrypted = await encryptWalletSecret(
    {
      walletType: "seed_phrase",
      recoveryPhrase,
      accountIndex,
    },
    password,
  );
  const record: EncryptedWalletRecord = {
    walletFormatVersion: WALLET_FORMAT_VERSION,
    walletType: "seed_phrase",
    address: privateKeyHexToAddress(privateKeyHex),
    publicKeyHex: keyMaterial.publicKeyHex,
    accountIndex,
    recoveryPhraseWordCount: RECOVERY_PHRASE_WORD_COUNT,
    createdAt: Date.now(),
    ...encrypted,
  };
  extensionAlarms().clear(SUBMITTED_TX_POLL_ALARM);
  await clearAllSubmittedTransactions();
  await clearAllWalletDataCaches();
  await saveWalletRecord(record);
  const settings = await loadSettings();
  activeSession = makeUnlockedSession(
    { walletType: "seed_phrase", recoveryPhrase, accountIndex },
    settings.autoLockMinutes,
    accountIndex,
  );
  await scheduleAutoLock(settings.autoLockMinutes);
  await refreshWalletDataCache(settings, record.address, { includeHistory: false });
  return getAppState();
}

function makeUnlockedSession(
  secret: { walletType: "private_key"; privateKeyHex: string } | { walletType: "seed_phrase"; recoveryPhrase: string; accountIndex?: number },
  autoLockMinutes: number,
  accountIndex: number,
): UnlockedSession {
  const privateKeyHex = secret.walletType === "seed_phrase"
    ? derivePrivateKeyHexFromRecoveryPhrase(secret.recoveryPhrase, accountIndex)
    : secret.privateKeyHex;
  const keyMaterial = buildWalletKeyMaterial(privateKeyHex);
  const now = Date.now();
  return {
    walletType: secret.walletType,
    privateKeyHex,
    recoveryPhrase: secret.walletType === "seed_phrase" ? secret.recoveryPhrase : undefined,
    publicKeyHex: keyMaterial.publicKeyHex,
    address: privateKeyHexToAddress(privateKeyHex),
    accountIndex,
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
  const previous = await loadWalletDataCache(settings.expectedNetwork);
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
  await saveWalletDataCache(next, settings.expectedNetwork);
  return next;
}

async function buildWatchOnlyState(
  settings: WalletSettings,
  records: WatchOnlyAddressRecord[],
): Promise<WatchOnlyAddressState[]> {
  if (records.length === 0) {
    return [];
  }
  const client = ChipcoinApiClient.fromBaseUrl(settings.nodeApiBaseUrl);
  return Promise.all(records.map(async (record) => {
    try {
      const [summary, history] = await Promise.all([
        client.address(record.address, API_TIMEOUTS_MS.summary),
        client.history(record.address, 10, API_TIMEOUTS_MS.history),
      ]);
      return {
        ...record,
        summary,
        history,
        error: null,
        updatedAt: Date.now(),
      };
    } catch (error) {
      return {
        ...record,
        summary: null,
        history: [],
        error: error instanceof Error ? error.message : "Unable to load watch-only address data.",
        updatedAt: null,
      };
    }
  }));
}

async function reconcileSubmittedTransactions(
  settings: WalletSettings,
  address: string,
  options: { forceCheckAll: boolean },
): Promise<void> {
  const submittedTransactions = await loadSubmittedTransactions(settings.expectedNetwork);
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

  await saveSubmittedTransactions(next, settings.expectedNetwork);
  if (didConfirmAny) {
    await refreshWalletDataCache(settings, address, { includeHistory: true });
  }
  await scheduleSubmittedTransactionPolling();
}

async function scheduleSubmittedTransactionPolling(): Promise<void> {
  const settings = await loadSettings();
  const submittedTransactions = await loadSubmittedTransactions(settings.expectedNetwork);
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

async function validateClientNetwork(client: ChipcoinApiClient, expectedNetwork: SupportedNetworkId): Promise<void> {
  await client.health();
  const status = await client.status();
  if (status.network !== expectedNetwork) {
    throw new Error(`Wrong network. Expected ${expectedNetwork}, got ${status.network}.`);
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
  const settings = await loadSettings();
  const current = await loadSubmittedTransactions(settings.expectedNetwork);
  await saveSubmittedTransactions(upsertSubmittedTransaction(current, record), settings.expectedNetwork);
}
