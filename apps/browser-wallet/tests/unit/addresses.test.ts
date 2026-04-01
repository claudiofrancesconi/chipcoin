import { describe, expect, it } from "vitest";

import { isValidAddress, privateKeyHexToAddress } from "../../src/crypto/addresses";

describe("address helpers", () => {
  it("derives a valid CHC address from private key hex", () => {
    const address = privateKeyHexToAddress("0000000000000000000000000000000000000000000000000000000000000001");
    expect(address.startsWith("CHC")).toBe(true);
    expect(isValidAddress(address)).toBe(true);
  });

  it("rejects malformed addresses", () => {
    expect(isValidAddress("not-a-valid-address")).toBe(false);
  });
});
