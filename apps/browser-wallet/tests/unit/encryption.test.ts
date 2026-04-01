import { describe, expect, it } from "vitest";

import { decryptPrivateKeyHex, encryptPrivateKeyHex } from "../../src/crypto/encryption";

describe("wallet encryption", () => {
  it("round-trips encrypted private key material", async () => {
    const encrypted = await encryptPrivateKeyHex(
      "0000000000000000000000000000000000000000000000000000000000000001",
      "very-strong-password",
    );

    const decrypted = await decryptPrivateKeyHex(
      encrypted.encryptedWalletBlob,
      "very-strong-password",
      encrypted.saltBase64,
      encrypted.ivBase64,
      encrypted.iterations,
    );

    expect(decrypted).toBe("0000000000000000000000000000000000000000000000000000000000000001");
  });
});
