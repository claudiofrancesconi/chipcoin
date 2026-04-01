import type { SelectionResult, SpendCandidate } from "./models";

export function selectInputs(candidates: SpendCandidate[], targetValue: number): SelectionResult {
  if (targetValue <= 0) {
    throw new Error("Target value must be positive.");
  }
  const ordered = [...candidates].sort((left, right) => {
    if (left.amountChipbits !== right.amountChipbits) {
      return left.amountChipbits - right.amountChipbits;
    }
    if (left.txid !== right.txid) {
      return left.txid.localeCompare(right.txid);
    }
    return left.index - right.index;
  });

  const selected: SpendCandidate[] = [];
  let total = 0;
  for (const candidate of ordered) {
    selected.push(candidate);
    total += candidate.amountChipbits;
    if (total >= targetValue) {
      return {
        selected,
        totalInputChipbits: total,
        changeChipbits: total - targetValue,
      };
    }
  }
  throw new Error("Insufficient spendable balance for the requested amount and fee.");
}
