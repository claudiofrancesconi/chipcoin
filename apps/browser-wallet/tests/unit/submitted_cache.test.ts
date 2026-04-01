import { describe, expect, it } from "vitest";

import {
  createSubmittedTransactionRecord,
  dedupeConfirmedHistory,
  isConfirmedTxLookup,
  markSubmittedTransactionChecked,
  markSubmittedTransactionConfirmed,
} from "../../src/wallet/submitted_cache";

describe("submitted transaction cache", () => {
  it("creates submitted records with initial polling metadata", () => {
    const now = 1_700_000_000_000;
    const record = createSubmittedTransactionRecord({
      txid: "aa".repeat(32),
      submittedAt: now,
      recipient: "CHCCrecipient",
      amountChipbits: 123,
      feeChipbits: 10,
    }, now);

    expect(record.status).toBe("submitted");
    expect(record.pollAttempts).toBe(0);
    expect(record.nextCheckAt).toBe(now + 15_000);
  });

  it("backs off polling after an unsuccessful check", () => {
    const now = 1_700_000_000_000;
    const record = createSubmittedTransactionRecord({
      txid: "bb".repeat(32),
      submittedAt: now,
      recipient: "CHCCrecipient",
      amountChipbits: 456,
      feeChipbits: 20,
    }, now);

    const [checkedOnce] = markSubmittedTransactionChecked([record], record.txid, now + 5_000);
    expect(checkedOnce.pollAttempts).toBe(1);
    expect(checkedOnce.lastCheckedAt).toBe(now + 5_000);
    expect(checkedOnce.nextCheckAt).toBe(now + 35_000);

    const [checkedTwice] = markSubmittedTransactionChecked([checkedOnce], record.txid, now + 35_000);
    expect(checkedTwice.pollAttempts).toBe(2);
    expect(checkedTwice.nextCheckAt).toBe(now + 95_000);
  });

  it("marks submitted records confirmed and stops polling", () => {
    const now = 1_700_000_000_000;
    const [confirmed] = markSubmittedTransactionConfirmed([
      createSubmittedTransactionRecord({
        txid: "cc".repeat(32),
        submittedAt: now,
        recipient: "CHCCrecipient",
        amountChipbits: 789,
        feeChipbits: 30,
      }, now),
    ], "cc".repeat(32), now + 60_000);

    expect(confirmed.status).toBe("confirmed");
    expect(confirmed.confirmedAt).toBe(now + 60_000);
    expect(confirmed.nextCheckAt).toBeUndefined();
  });

  it("treats only chain inclusion as confirmed", () => {
    expect(isConfirmedTxLookup({
      location: "mempool",
      block_hash: null,
      height: null,
      transaction: {
        txid: "dd".repeat(32),
        version: 1,
        locktime: 0,
        inputs: [],
        outputs: [],
        metadata: {},
      },
    })).toBe(false);

    expect(isConfirmedTxLookup({
      location: "chain",
      block_hash: "ee".repeat(32),
      height: 12,
      transaction: {
        txid: "dd".repeat(32),
        version: 1,
        locktime: 0,
        inputs: [],
        outputs: [],
        metadata: {},
      },
    })).toBe(true);
  });

  it("removes locally tracked txids from confirmed history to prevent duplicates", () => {
    const history = [
      {
        block_height: 12,
        block_hash: "11".repeat(32),
        txid: "aa".repeat(32),
        incoming_chipbits: 10,
        outgoing_chipbits: 0,
        net_chipbits: 10,
        timestamp: 1_700_000_100,
      },
      {
        block_height: 13,
        block_hash: "22".repeat(32),
        txid: "bb".repeat(32),
        incoming_chipbits: 20,
        outgoing_chipbits: 0,
        net_chipbits: 20,
        timestamp: 1_700_000_200,
      },
    ];
    const local = [
      {
        txid: "aa".repeat(32),
        submittedAt: 1_700_000_000,
        recipient: "CHCCrecipient",
        amountChipbits: 10,
        feeChipbits: 1,
        status: "confirmed" as const,
      },
    ];

    expect(dedupeConfirmedHistory(history, local)).toEqual([history[1]]);
  });
});
