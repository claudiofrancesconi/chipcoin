import { beforeEach, describe, expect, it, vi } from "vitest";

interface InMemoryStorageArea {
  get: (key: string, callback: (result: Record<string, unknown>) => void) => void;
  set: (items: Record<string, unknown>, callback: () => void) => void;
  remove: (key: string, callback: () => void) => void;
}

describe("wallet session security", () => {
  beforeEach(() => {
    vi.resetModules();

    const storage = new Map<string, unknown>();
    const local: InMemoryStorageArea = {
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

    (globalThis as { chrome?: unknown }).chrome = {
      storage: { local },
      alarms: {
        create: vi.fn(),
        clear: vi.fn(),
      },
    };
  });

  it("requires explicit confirmation before revealing a private key from an active session", async () => {
    const { createWallet, exportPrivateKey } = await import("../../src/background/session");

    await createWallet("phase6-password");

    await expect(exportPrivateKey({})).rejects.toThrow("Explicit confirmation is required before revealing the private key.");
    await expect(exportPrivateKey({ confirmActiveSession: true })).resolves.toMatch(/^[0-9a-f]{64}$/);
  });

  it("clears wallet state, submitted cache, and local snapshot on remove", async () => {
    const session = await import("../../src/background/session");
    const { createSubmittedTransactionRecord } = await import("../../src/wallet/submitted_cache");
    const { loadSubmittedTransactions } = await import("../../src/storage/session_store");
    const { loadWalletDataCache, saveWalletDataCache } = await import("../../src/storage/wallet_data_store");
    const { loadWalletRecord } = await import("../../src/storage/wallet_store");

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
  });
});
