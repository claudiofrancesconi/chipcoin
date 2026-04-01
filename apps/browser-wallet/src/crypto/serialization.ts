import { sha256 } from "@noble/hashes/sha256";

import type { TransactionModel } from "../wallet/models";
import { bytesToHex, hexToBytes } from "./keys";

function encodeVarint(value: number): Uint8Array {
  if (!Number.isInteger(value) || value < 0) {
    throw new Error("varint value cannot be negative");
  }
  if (value < 0xfd) {
    return Uint8Array.from([value]);
  }
  if (value <= 0xffff) {
    return concatBytes(Uint8Array.from([0xfd]), encodeUint16(value));
  }
  if (value <= 0xffffffff) {
    return concatBytes(Uint8Array.from([0xfe]), encodeUint32(value));
  }
  return concatBytes(Uint8Array.from([0xff]), encodeUint64(value));
}

function encodeBytes(value: Uint8Array): Uint8Array {
  return concatBytes(encodeVarint(value.length), value);
}

function encodeString(value: string): Uint8Array {
  return encodeBytes(new TextEncoder().encode(value));
}

function encodeHash(hexValue: string): Uint8Array {
  const raw = hexToBytes(hexValue);
  if (raw.length !== 32) {
    throw new Error("Hash values must be exactly 32 bytes.");
  }
  return raw;
}

function encodeMetadata(metadata: Record<string, string>): Uint8Array {
  const items = Object.entries(metadata).sort(([left], [right]) => left.localeCompare(right));
  const encoded = [encodeVarint(items.length)];
  for (const [key, value] of items) {
    encoded.push(encodeString(key), encodeString(value));
  }
  return concatBytes(...encoded);
}

export function serializeTransaction(transaction: TransactionModel): Uint8Array {
  const encoded: Uint8Array[] = [encodeUint32(transaction.version), encodeVarint(transaction.inputs.length)];
  for (const input of transaction.inputs) {
    encoded.push(
      encodeHash(input.previousOutput.txid),
      encodeUint32(input.previousOutput.index),
      encodeBytes(input.signatureHex ? hexToBytes(input.signatureHex) : new Uint8Array()),
      encodeBytes(input.publicKeyHex ? hexToBytes(input.publicKeyHex) : new Uint8Array()),
      encodeUint32(input.sequence),
    );
  }
  encoded.push(encodeVarint(transaction.outputs.length));
  for (const output of transaction.outputs) {
    encoded.push(encodeUint64(output.value), encodeString(output.recipient));
  }
  encoded.push(encodeUint32(transaction.locktime), encodeMetadata(transaction.metadata));
  return concatBytes(...encoded);
}

export function serializeTransactionHex(transaction: TransactionModel): string {
  return bytesToHex(serializeTransaction(transaction));
}

export function serializeTransactionForSigning(args: {
  transaction: TransactionModel;
  inputIndex: number;
  previousOutputValue: number;
  previousOutputRecipient: string;
}): Uint8Array {
  const { transaction, inputIndex, previousOutputValue, previousOutputRecipient } = args;
  if (inputIndex < 0 || inputIndex >= transaction.inputs.length) {
    throw new Error("Transaction input index is out of range.");
  }
  const stripped: TransactionModel = {
    version: transaction.version,
    inputs: transaction.inputs.map((input) => ({
      previousOutput: input.previousOutput,
      signatureHex: "",
      publicKeyHex: "",
      sequence: input.sequence,
    })),
    outputs: transaction.outputs,
    locktime: transaction.locktime,
    metadata: transaction.metadata,
  };
  return concatBytes(
    serializeTransaction(stripped),
    encodeUint32(inputIndex),
    encodeUint64(previousOutputValue),
    encodeString(previousOutputRecipient),
    encodeUint32(1),
  );
}

export function transactionSignatureDigest(args: {
  transaction: TransactionModel;
  inputIndex: number;
  previousOutputValue: number;
  previousOutputRecipient: string;
}): Uint8Array {
  return doubleSha256(serializeTransactionForSigning(args));
}

export function transactionId(transaction: TransactionModel): string {
  return bytesToHex(doubleSha256(serializeTransaction(transaction)));
}

export function serializeSignedTransactionToRawHex(transaction: TransactionModel): string {
  return serializeTransactionHex(transaction);
}

function encodeUint16(value: number): Uint8Array {
  const buffer = new ArrayBuffer(2);
  new DataView(buffer).setUint16(0, value, true);
  return new Uint8Array(buffer);
}

function encodeUint32(value: number): Uint8Array {
  const buffer = new ArrayBuffer(4);
  new DataView(buffer).setUint32(0, value, true);
  return new Uint8Array(buffer);
}

function encodeUint64(value: number): Uint8Array {
  const buffer = new ArrayBuffer(8);
  new DataView(buffer).setBigUint64(0, BigInt(value), true);
  return new Uint8Array(buffer);
}

function doubleSha256(value: Uint8Array): Uint8Array {
  return sha256(sha256(value));
}

function concatBytes(...parts: Uint8Array[]): Uint8Array {
  const length = parts.reduce((total, item) => total + item.length, 0);
  const joined = new Uint8Array(length);
  let offset = 0;
  for (const part of parts) {
    joined.set(part, offset);
    offset += part.length;
  }
  return joined;
}
