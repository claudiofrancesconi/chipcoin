const CHIPBITS_PER_CHC = 100_000_000;

export function formatChipbits(value: number): string {
  return new Intl.NumberFormat("en-US").format(value);
}

export function formatChc(valueChipbits: number): string {
  const valueChc = valueChipbits / CHIPBITS_PER_CHC;
  return `${new Intl.NumberFormat("en-US", {
    minimumFractionDigits: 0,
    maximumFractionDigits: 8,
  }).format(valueChc)} CHC`;
}

export function parseChcToChipbits(value: string): number {
  const normalized = value.trim().replace(/,/g, "");
  if (!normalized) {
    throw new Error("Amount is required.");
  }
  if (!/^\d+(\.\d{1,8})?$/.test(normalized)) {
    throw new Error("Amount must be a valid CHC value with up to 8 decimals.");
  }
  const [whole, fraction = ""] = normalized.split(".", 2);
  const chipbits = `${whole}${fraction.padEnd(8, "0")}`;
  return Number(chipbits);
}

export function shortHash(value: string, visible = 8): string {
  if (value.length <= visible * 2) {
    return value;
  }
  return `${value.slice(0, visible)}…${value.slice(-visible)}`;
}
