import { describe, expect, it } from "vitest";

import { selectInputs } from "../../src/wallet/selection";

describe("selectInputs", () => {
  it("selects deterministically by amount, txid, and index", () => {
    const selection = selectInputs(
      [
        { txid: "bb", index: 0, amountChipbits: 5, recipient: "CHC1" },
        { txid: "aa", index: 2, amountChipbits: 5, recipient: "CHC1" },
        { txid: "aa", index: 1, amountChipbits: 2, recipient: "CHC1" },
      ],
      7,
    );

    expect(selection.selected).toEqual([
      { txid: "aa", index: 1, amountChipbits: 2, recipient: "CHC1" },
      { txid: "aa", index: 2, amountChipbits: 5, recipient: "CHC1" },
    ]);
    expect(selection.changeChipbits).toBe(0);
  });
});
