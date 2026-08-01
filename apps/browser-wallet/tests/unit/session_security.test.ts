import { beforeEach, describe, expect, it, vi } from "vitest";

interface InMemoryStorageArea {
  get: (key: string, callback: (result: Record<string, unknown>) => void) => void;
  set: (items: Record<string, unknown>, callback: () => void) => void;
  remove: (key: string, callback: () => void) => void;
}

describe("wallet session security", () => {
  beforeEach(() => {
    vi.resetModules();

    function makeStorageArea(storage: Map<string, unknown>): InMemoryStorageArea {
      return {
        get: (key, callback) => callback({ [key]: storage.get(key) }),
        set: (items, callback) => {
          for (const [key, value] of Object.entries(items)) {
            storage.set(key, value);
          }
          callback();
        },
        remove: (key, callback) => {
          storage.delete(key);
          callback();
        },
      };
    }

    const localStorage = new Map<string, unknown>();
    const sessionStorage = new Map<string, unknown>();
    const local = makeStorageArea(localStorage);
    const session = makeStorageArea(sessionStorage);

    (globalThis as { chrome?: unknown }).chrome = {
      storage: { local, session },
      alarms: {
        create: vi.fn(),
        clear: vi.fn(),
      },
    };
  });

  it("restores the unlocked session after a service worker restart", async () => {
    const session = await import("../../src/background/session");
    const { privateKeyHexToAddress } = await import("../../src/crypto/addresses");

    const privateKeyHex = "0000000000000000000000000000000000000000000000000000000000000001";
    const address = privateKeyHexToAddress(privateKeyHex);
    const issuedAt = new Date(Date.now()).toISOString();
    const expiresAt = new Date(Date.now() + 10 * 60 * 1000).toISOString();
    const message = [
      "Chipcoin Signed Login v1",
      "Domain: chipcoinprotocol.com",
      "Origin: https://chipcoinprotocol.com",
      "Network: testnet",
      `Address: ${address}`,
      "Scheme: 0",
      "Nonce: service-worker-restart-test",
      `Issued At: ${issuedAt}`,
      `Expires At: ${expiresAt}`,
      "Statement: Sign in to chipcoinprotocol.com",
    ].join("\n");

    await session.importWallet(privateKeyHex, "phase12-password");

    vi.resetModules();

    const restartedSession = await import("../../src/background/session");
    await expect(restartedSession.signProviderLoginMessage({
      origin: "https://chipcoinprotocol.com",
      domain: "chipcoinprotocol.com",
      message,
    })).resolves.toMatchObject({
      address,
      signature_scheme: 0,
      message,
    });
  });

  it("requires explicit confirmation before revealing a private key from an active session", async () => {
    const { createWallet, exportPrivateKey } = await import("../../src/background/session");

    await createWallet("phase6-password");

    await expect(exportPrivateKey({})).rejects.toThrow("Explicit confirmation is required before revealing the private key.");
    await expect(exportPrivateKey({ confirmActiveSession: true })).resolves.toMatch(/^[0-9a-f]{64}$/);
  });

  it("recovers the same wallet from the same recovery phrase", async () => {
    const session = await import("../../src/background/session");
    const { loadWalletRecord, clearWalletRecord } = await import("../../src/storage/wallet_store");

    const recoveryPhrase = session.generateWalletRecoveryPhrase();
    const createdState = await session.createWalletFromSeed(recoveryPhrase, "phase12-password");
    const createdAddress = createdState.address;

    await session.removeWallet();
    await clearWalletRecord();

    const recoveredState = await session.recoverWalletFromSeed(recoveryPhrase, "phase12-password");
    const recoveredRecord = await loadWalletRecord();

    expect(createdAddress).toBeTruthy();
    expect(recoveredState.address).toBe(createdAddress);
    expect(recoveredRecord?.walletType).toBe("seed_phrase");
    expect(recoveredRecord?.accountIndex).toBe(0);
  });

  it("exports the recovery phrase for seed-based wallets only", async () => {
    const session = await import("../../src/background/session");

    const recoveryPhrase = session.generateWalletRecoveryPhrase();
    await session.createWalletFromSeed(recoveryPhrase, "phase12-password");

    await expect(session.exportRecoveryPhrase({})).rejects.toThrow("Explicit confirmation is required");
    await expect(session.exportRecoveryPhrase({ confirmActiveSession: true })).resolves.toBe(recoveryPhrase);

    await session.removeWallet();
    await session.importWallet("0000000000000000000000000000000000000000000000000000000000000001", "phase12-password");
    await expect(session.exportRecoveryPhrase({ confirmActiveSession: true })).rejects.toThrow("has no recovery phrase");
  });

  it("clears wallet state, submitted cache, and local snapshot on remove", async () => {
    const session = await import("../../src/background/session");
    const { createSubmittedTransactionRecord } = await import("../../src/wallet/submitted_cache");
    const { loadSubmittedTransactions } = await import("../../src/storage/session_store");
    const { loadWalletDataCache, saveWalletDataCache } = await import("../../src/storage/wallet_data_store");
    const { loadWalletRecord } = await import("../../src/storage/wallet_store");
    const { loadWatchOnlyAddressRecords, saveWatchOnlyAddressRecords } = await import("../../src/storage/watch_only_store");

    const state = await session.createWallet("phase6-password");
    await session.rememberSubmittedTransaction(createSubmittedTransactionRecord({
      txid: "ab".repeat(32),
      submittedAt: Date.now(),
      recipient: "CHCCdoRFzAkxWSzD8CYNPa9qSqChy8vau9RQj",
      amountChipbits: 2_000_000_000,
      feeChipbits: 1_000,
    }));
    await saveWalletDataCache({
      summary: null,
      utxos: [],
      history: [],
      updatedAt: Date.now(),
    });
    await saveWatchOnlyAddressRecords([{
      address: "CHCQCqjJWcT8Jqxvmn9xspxBWnTojXQp93Wqu9sP5F6GkFd1f5xKiRhE",
      addedAt: Date.now(),
    }]);

    await session.removeWallet();

    expect(state.hasWallet).toBe(true);
    await expect(loadWalletRecord()).resolves.toBeNull();
    await expect(loadSubmittedTransactions()).resolves.toEqual([]);
    await expect(loadWalletDataCache()).resolves.toEqual({
      summary: null,
      utxos: [],
      history: [],
      updatedAt: null,
    });
    await expect(loadWatchOnlyAddressRecords()).resolves.toEqual([]);
  });

  it("refuses provider login signing after the wallet auto-locks", async () => {
    const session = await import("../../src/background/session");
    const { privateKeyHexToAddress } = await import("../../src/crypto/addresses");

    const privateKeyHex = "0000000000000000000000000000000000000000000000000000000000000001";
    const address = privateKeyHexToAddress(privateKeyHex);
    const issuedAt = new Date(Date.now()).toISOString();
    const expiresAt = new Date(Date.now() + 10 * 60 * 1000).toISOString();
    await session.importWallet(privateKeyHex, "phase12-password");
    await session.lockWallet();

    await expect(session.signProviderLoginMessage({
      origin: "https://chipcoinprotocol.com",
      domain: "chipcoinprotocol.com",
      message: [
        "Chipcoin Signed Login v1",
        "Domain: chipcoinprotocol.com",
        "Origin: https://chipcoinprotocol.com",
        "Network: testnet",
        `Address: ${address}`,
        "Scheme: 0",
        "Nonce: auto-lock-test",
        `Issued At: ${issuedAt}`,
        `Expires At: ${expiresAt}`,
        "Statement: Sign in to chipcoinprotocol.com",
      ].join("\n"),
    })).rejects.toThrow("WALLET_LOCKED");
  });
});
