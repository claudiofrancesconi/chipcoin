import { secp256k1 } from "@noble/curves/secp256k1";

const SECP256K1_ORDER = BigInt("0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141");

export interface WalletKeyMaterial {
  privateKeyHex: string;
  publicKeyHex: string;
  compressed: true;
}

export function generatePrivateKeyHex(): string {
  return bytesToHex(secp256k1.utils.randomPrivateKey());
}

export function normalizePrivateKeyHex(value: string): string {
  const normalized = value.trim().toLowerCase();
  if (!/^[0-9a-f]{64}$/.test(normalized)) {
    throw new Error("Private key hex is invalid.");
  }
  const valueBigInt = BigInt(`0x${normalized}`);
  if (valueBigInt <= 0n || valueBigInt >= SECP256K1_ORDER) {
    throw new Error("Private key is outside the valid secp256k1 range.");
  }
  return normalized;
}

export function derivePublicKeyHex(privateKeyHex: string): string {
  const normalized = normalizePrivateKeyHex(privateKeyHex);
  return bytesToHex(secp256k1.getPublicKey(normalized, true));
}

export function buildWalletKeyMaterial(privateKeyHex: string): WalletKeyMaterial {
  const normalized = normalizePrivateKeyHex(privateKeyHex);
  return {
    privateKeyHex: normalized,
    publicKeyHex: derivePublicKeyHex(normalized),
    compressed: true,
  };
}

export function hexToBytes(value: string): Uint8Array {
  const normalized = value.trim();
  if (normalized.length % 2 !== 0) {
    throw new Error("Hex input must have an even length.");
  }
  const bytes = new Uint8Array(normalized.length / 2);
  for (let index = 0; index < normalized.length; index += 2) {
    bytes[index / 2] = Number.parseInt(normalized.slice(index, index + 2), 16);
  }
  return bytes;
}

export function bytesToHex(value: Uint8Array): string {
  return Array.from(value, (item) => item.toString(16).padStart(2, "0")).join("");
}
