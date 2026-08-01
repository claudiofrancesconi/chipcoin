import { secp256k1 } from "@noble/curves/secp256k1";
import { sha256 } from "@noble/hashes/sha256";
import { describe, expect, it } from "vitest";

import {
  LOGIN_SIGNING_DOMAIN,
  assertPublicKeyMatchesAddress,
  loginMessageDigest,
  parseChipcoinSignedLoginMessage,
  signLoginMessage,
  validateLoginOriginBinding,
  verifyLoginMessageSignature,
} from "../../src/provider/login_message";
import { privateKeyHexToAddress } from "../../src/crypto/addresses";
import { derivePublicKeyHex, hexToBytes } from "../../src/crypto/keys";
import { transactionSignatureDigest } from "../../src/crypto/serialization";
import type { TransactionModel } from "../../src/wallet/models";

const PRIVATE_KEY = "0000000000000000000000000000000000000000000000000000000000000001";
const PUBLIC_KEY = derivePublicKeyHex(PRIVATE_KEY);
const ADDRESS = privateKeyHexToAddress(PRIVATE_KEY);

function canonicalMessage(overrides: Partial<Record<string, string>> = {}): string {
  const fields = {
    Domain: "chipcoinprotocol.com",
    Origin: "https://chipcoinprotocol.com",
    Network: "testnet",
    Address: ADDRESS,
    Scheme: "0",
    Nonce: "nonce-123",
    "Issued At": "2026-08-01T10:00:00.000Z",
    "Expires At": "2026-08-01T10:10:00.000Z",
    Statement: "Sign in to chipcoinprotocol.com",
    ...overrides,
  };
  return [
    "Chipcoin Signed Login v1",
    `Domain: ${fields.Domain}`,
    `Origin: ${fields.Origin}`,
    `Network: ${fields.Network}`,
    `Address: ${fields.Address}`,
    `Scheme: ${fields.Scheme}`,
    `Nonce: ${fields.Nonce}`,
    `Issued At: ${fields["Issued At"]}`,
    `Expires At: ${fields["Expires At"]}`,
    `Statement: ${fields.Statement}`,
  ].join("\n");
}

describe("Chipcoin Signed Login v1", () => {
  it("parses the canonical login message", () => {
    const parsed = parseChipcoinSignedLoginMessage(canonicalMessage(), Date.parse("2026-08-01T10:01:00.000Z"));
    expect(parsed.address).toBe(ADDRESS);
    expect(parsed.scheme).toBe(0);
    expect(parsed.network).toBe("testnet");
  });

  it("rejects duplicate or missing fields", () => {
    const message = canonicalMessage().replace("Statement: Sign in to chipcoinprotocol.com", "Domain: chipcoinprotocol.com");
    expect(() => parseChipcoinSignedLoginMessage(message, Date.parse("2026-08-01T10:01:00.000Z"))).toThrow("INVALID_MESSAGE");
  });

  it("rejects malformed title, overlong statement, overlong nonce, and expired messages", () => {
    expect(() => parseChipcoinSignedLoginMessage(canonicalMessage().replace("Chipcoin Signed Login v1", "Bad"), Date.parse("2026-08-01T10:01:00.000Z"))).toThrow("INVALID_MESSAGE");
    expect(() => parseChipcoinSignedLoginMessage(canonicalMessage({ Statement: "x".repeat(141) }), Date.parse("2026-08-01T10:01:00.000Z"))).toThrow("INVALID_MESSAGE");
    expect(() => parseChipcoinSignedLoginMessage(canonicalMessage({ Nonce: "x".repeat(129) }), Date.parse("2026-08-01T10:01:00.000Z"))).toThrow("INVALID_MESSAGE");
    expect(() => parseChipcoinSignedLoginMessage(canonicalMessage(), Date.parse("2026-08-01T10:11:00.000Z"))).toThrow("INVALID_MESSAGE");
    expect(() => parseChipcoinSignedLoginMessage(`${canonicalMessage()}\n${"x".repeat(2_050)}`, Date.parse("2026-08-01T10:01:00.000Z"))).toThrow("INVALID_MESSAGE");
  });

  it("rejects origin/domain mismatch", () => {
    const parsed = parseChipcoinSignedLoginMessage(canonicalMessage(), Date.parse("2026-08-01T10:01:00.000Z"));
    expect(() => validateLoginOriginBinding(parsed, "https://evil.example", "chipcoinprotocol.com")).toThrow("ORIGIN_MISMATCH");
    expect(() => parseChipcoinSignedLoginMessage(canonicalMessage({ Origin: "https://evil.example" }), Date.parse("2026-08-01T10:01:00.000Z"))).toThrow("ORIGIN_MISMATCH");
  });

  it("requires textual domains to be ASCII/punycode and validates punycode origins", () => {
    const parsed = parseChipcoinSignedLoginMessage(canonicalMessage({
      Domain: "xn--bcher-kva.example",
      Origin: "https://xn--bcher-kva.example",
      Statement: "Sign in to xn--bcher-kva.example",
    }), Date.parse("2026-08-01T10:01:00.000Z"));
    expect(parsed.domain).toBe("xn--bcher-kva.example");
    expect(parsed.origin).toBe("https://xn--bcher-kva.example");
    validateLoginOriginBinding(parsed, "https://bücher.example", "xn--bcher-kva.example");
    expect(() => parseChipcoinSignedLoginMessage(canonicalMessage({
      Domain: "çhipcoin.example",
      Origin: "https://çhipcoin.example",
      Statement: "Sign in to çhipcoin.example",
    }), Date.parse("2026-08-01T10:01:00.000Z"))).toThrow("INVALID_MESSAGE");
  });

  it("rejects unsupported schemes and prevents CHC scheme 10 signing", () => {
    expect(() => parseChipcoinSignedLoginMessage(canonicalMessage({ Scheme: "10" }), Date.parse("2026-08-01T10:01:00.000Z"))).toThrow("SCHEME_MISMATCH");
  });

  it("signs and verifies over the login-message domain", () => {
    const message = canonicalMessage();
    const signature = signLoginMessage(PRIVATE_KEY, message);
    expect(verifyLoginMessageSignature(PUBLIC_KEY, signature, message)).toBe(true);
    assertPublicKeyMatchesAddress(PUBLIC_KEY, ADDRESS);
    expect(LOGIN_SIGNING_DOMAIN).toBe("chipcoin:web-auth:v1:testnet");
  });

  it("does not sign the transaction sighash domain", () => {
    const message = canonicalMessage();
    const signature = signLoginMessage(PRIVATE_KEY, message);
    const transaction: TransactionModel = {
      version: 1,
      inputs: [{
        previousOutput: { txid: "11".repeat(32), index: 0 },
        signatureHex: "",
        publicKeyHex: "",
        sequence: 0xffffffff,
      }],
      outputs: [{ value: 1, recipient: ADDRESS }],
      locktime: 0,
      metadata: {},
    };
    const txDigest = transactionSignatureDigest({
      transaction,
      inputIndex: 0,
      previousOutputValue: 1,
      previousOutputRecipient: ADDRESS,
    });
    expect(loginMessageDigest(message)).not.toEqual(txDigest);
    expect(
      secp256k1.verify(secp256k1.Signature.fromDER(hexToBytes(signature)).toCompactRawBytes(), txDigest, hexToBytes(PUBLIC_KEY), {
        lowS: true,
        prehash: false,
      }),
    ).toBe(false);
    expect(loginMessageDigest(message)).toEqual(sha256(new TextEncoder().encode(`${LOGIN_SIGNING_DOMAIN}\0${message}`)));
  });
});
