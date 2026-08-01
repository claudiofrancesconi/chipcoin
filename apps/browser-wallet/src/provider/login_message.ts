import { sha256 } from "@noble/hashes/sha256";
import { secp256k1 } from "@noble/curves/secp256k1";

import { parseAddress, publicKeyHexToAddress } from "../crypto/addresses";
import { bytesToHex, hexToBytes } from "../crypto/keys";

export const SIGNED_LOGIN_TITLE = "Chipcoin Signed Login v1";
export const LOGIN_SIGNING_DOMAIN = "chipcoin:web-auth:v1:testnet";
export const LOGIN_MESSAGE_MAX_LENGTH = 2_048;
export const LOGIN_STATEMENT_MAX_LENGTH = 140;
export const LOGIN_NONCE_MAX_LENGTH = 128;

const FIELD_ORDER = [
  "Domain",
  "Origin",
  "Network",
  "Address",
  "Scheme",
  "Nonce",
  "Issued At",
  "Expires At",
  "Statement",
] as const;

export type LoginMessageField = typeof FIELD_ORDER[number];

export interface ParsedLoginMessage {
  title: typeof SIGNED_LOGIN_TITLE;
  domain: string;
  origin: string;
  network: "testnet";
  address: string;
  scheme: 0;
  nonce: string;
  issuedAt: string;
  expiresAt: string;
  statement: string;
}

export function parseChipcoinSignedLoginMessage(message: string, now = Date.now()): ParsedLoginMessage {
  if (typeof message !== "string" || message.length === 0 || message.length > LOGIN_MESSAGE_MAX_LENGTH) {
    throw new Error("INVALID_MESSAGE");
  }
  if (message.includes("\r")) {
    throw new Error("INVALID_MESSAGE");
  }
  const lines = message.split("\n");
  if (lines[0] !== SIGNED_LOGIN_TITLE) {
    throw new Error("INVALID_MESSAGE");
  }
  if (lines.length !== FIELD_ORDER.length + 1) {
    throw new Error("INVALID_MESSAGE");
  }

  const values = new Map<LoginMessageField, string>();
  for (let index = 1; index < lines.length; index += 1) {
    const line = lines[index];
    const separator = line.indexOf(": ");
    if (separator <= 0) {
      throw new Error("INVALID_MESSAGE");
    }
    const field = line.slice(0, separator) as LoginMessageField;
    const value = line.slice(separator + 2);
    if (!FIELD_ORDER.includes(field)) {
      throw new Error("INVALID_MESSAGE");
    }
    if (values.has(field)) {
      throw new Error("INVALID_MESSAGE");
    }
    values.set(field, value);
  }

  for (const field of FIELD_ORDER) {
    if (!values.has(field)) {
      throw new Error("INVALID_MESSAGE");
    }
  }

  const domain = requireText(values.get("Domain"));
  const origin = requireText(values.get("Origin"));
  const network = requireText(values.get("Network"));
  const address = requireText(values.get("Address"));
  const schemeRaw = requireText(values.get("Scheme"));
  const nonce = requireText(values.get("Nonce"));
  const issuedAt = requireText(values.get("Issued At"));
  const expiresAt = requireText(values.get("Expires At"));
  const statement = requireText(values.get("Statement"));

  if (network !== "testnet") {
    throw new Error("UNSUPPORTED_NETWORK");
  }
  if (schemeRaw !== "0") {
    throw new Error("SCHEME_MISMATCH");
  }
  if (nonce.length > LOGIN_NONCE_MAX_LENGTH || statement.length > LOGIN_STATEMENT_MAX_LENGTH) {
    throw new Error("INVALID_MESSAGE");
  }
  if (statement.includes("\n") || nonce.includes("\n")) {
    throw new Error("INVALID_MESSAGE");
  }
  if (!isValidDomainText(domain)) {
    throw new Error("INVALID_MESSAGE");
  }
  const originUrl = parseOrigin(origin);
  if (originUrl.hostname !== domain) {
    throw new Error("ORIGIN_MISMATCH");
  }
  const info = parseAddress(address);
  if (info.kind !== "legacy" || info.schemeId !== 0) {
    throw new Error("SCHEME_MISMATCH");
  }
  const issuedMs = parseIsoTimestamp(issuedAt);
  const expiresMs = parseIsoTimestamp(expiresAt);
  if (expiresMs <= issuedMs) {
    throw new Error("INVALID_MESSAGE");
  }
  if (Number.isFinite(now) && expiresMs <= now) {
    throw new Error("INVALID_MESSAGE");
  }

  return {
    title: SIGNED_LOGIN_TITLE,
    domain,
    origin: originUrl.origin,
    network,
    address,
    scheme: 0,
    nonce,
    issuedAt: new Date(issuedMs).toISOString(),
    expiresAt: new Date(expiresMs).toISOString(),
    statement,
  };
}

export function validateLoginOriginBinding(parsed: ParsedLoginMessage, requestingOrigin: string, requestedDomain?: string): void {
  const origin = parseOrigin(requestingOrigin);
  if (origin.origin !== parsed.origin || origin.hostname !== parsed.domain) {
    throw new Error("ORIGIN_MISMATCH");
  }
  if (requestedDomain && requestedDomain !== parsed.domain) {
    throw new Error("ORIGIN_MISMATCH");
  }
}

export function loginMessageDigest(message: string): Uint8Array {
  const tagBytes = new TextEncoder().encode(`${LOGIN_SIGNING_DOMAIN}\0`);
  const messageBytes = new TextEncoder().encode(message);
  const payload = new Uint8Array(tagBytes.length + messageBytes.length);
  payload.set(tagBytes);
  payload.set(messageBytes, tagBytes.length);
  return sha256(payload);
}

export function signLoginMessage(privateKeyHex: string, message: string): string {
  const signature = secp256k1.sign(loginMessageDigest(message), hexToBytes(privateKeyHex), {
    lowS: true,
    prehash: false,
  });
  return bytesToHex(signature.toDERRawBytes());
}

export function verifyLoginMessageSignature(publicKeyHex: string, signatureHex: string, message: string): boolean {
  const signature = secp256k1.Signature.fromDER(hexToBytes(signatureHex)).toCompactRawBytes();
  return secp256k1.verify(
    signature,
    loginMessageDigest(message),
    hexToBytes(publicKeyHex),
    { lowS: true, prehash: false },
  );
}

export function assertPublicKeyMatchesAddress(publicKeyHex: string, address: string): void {
  if (publicKeyHexToAddress(publicKeyHex) !== address) {
    throw new Error("ADDRESS_MISMATCH");
  }
}

function requireText(value: string | undefined): string {
  if (value === undefined || value.length === 0) {
    throw new Error("INVALID_MESSAGE");
  }
  return value;
}

function parseOrigin(value: string): URL {
  let url: URL;
  try {
    url = new URL(value);
  } catch {
    throw new Error("INVALID_MESSAGE");
  }
  if ((url.protocol !== "https:" && url.protocol !== "http:") || url.pathname !== "/" || url.search || url.hash) {
    throw new Error("INVALID_MESSAGE");
  }
  return url;
}

function parseIsoTimestamp(value: string): number {
  const parsed = Date.parse(value);
  if (!Number.isFinite(parsed) || new Date(parsed).toISOString() !== value) {
    throw new Error("INVALID_MESSAGE");
  }
  return parsed;
}

function isValidDomainText(value: string): boolean {
  return value.length <= 253 && /^[a-z0-9.-]+$/i.test(value) && !value.includes("..");
}
