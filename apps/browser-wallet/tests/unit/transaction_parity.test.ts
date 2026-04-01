import { secp256k1 } from "@noble/curves/secp256k1";

import { describe, expect, it } from "vitest";

import { privateKeyHexToAddress } from "../../src/crypto/addresses";
import {
  serializeSignedTransactionToRawHex,
  serializeTransactionForSigning,
  transactionId,
  transactionSignatureDigest,
} from "../../src/crypto/serialization";
import { bytesToHex, hexToBytes } from "../../src/crypto/keys";
import { signDigestHex } from "../../src/crypto/signing";
import { buildSignedPaymentTransaction } from "../../src/wallet/build_transaction";
import type { TransactionModel } from "../../src/wallet/models";

const SENDER_PRIVATE_KEY = "0000000000000000000000000000000000000000000000000000000000000001";
const RECIPIENT_PRIVATE_KEY = "0000000000000000000000000000000000000000000000000000000000000002";
const SENDER_ADDRESS = "CHCCT9A8CEgF7qJ3T6QuXSFQN31kEexxxa2oX";
const RECIPIENT_ADDRESS = "CHCCH5FG4NCAWBFqa2zZKufrdnAa7rRE1gH5C";
const SENDER_PUBLIC_KEY = "0279be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798";
const EXPECTED_DIGEST = "062cee33f23423fad188b375d429a0b1a09069d41f7062075403c9ec4c0794ba";
const EXPECTED_SIGNING_PAYLOAD = "01000000011111111111111111111111111111111111111111111111111111111111111111000000000000ffffffff02bc02000000000000254348434348354647344e43415742467161327a5a4b756672646e4161377252453167483543c8000000000000002543484343543941384345674637714a3354365175585346514e33316b456578787861326f58000000000000000000e8030000000000002543484343543941384345674637714a3354365175585346514e33316b456578787861326f5801000000";
const EXPECTED_SIGNATURE = "3045022100f63d2eb98c23a0fbd1323fc09f044d6e705c3b47c0b7169c07d7bd8be157c71702203abe9b4cc21e29d095653a2603398269809a8b2ed694ed109e78f5b478a85c2c";
const EXPECTED_RAW = "0100000001111111111111111111111111111111111111111111111111111111111111111100000000473045022100f63d2eb98c23a0fbd1323fc09f044d6e705c3b47c0b7169c07d7bd8be157c71702203abe9b4cc21e29d095653a2603398269809a8b2ed694ed109e78f5b478a85c2c210279be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798ffffffff02bc02000000000000254348434348354647344e43415742467161327a5a4b756672646e4161377252453167483543c8000000000000002543484343543941384345674637714a3354365175585346514e33316b456578787861326f580000000000";
const EXPECTED_TXID = "881ba39bf8018cbd4a6930f5eaf653b4c28010a56f1e77fcab48d1a0ca30105e";

function unsignedTransaction(): TransactionModel {
  return {
    version: 1,
    inputs: [
      {
        previousOutput: { txid: "11".repeat(32), index: 0 },
        signatureHex: "",
        publicKeyHex: "",
        sequence: 0xffffffff,
      },
    ],
    outputs: [
      { value: 700, recipient: RECIPIENT_ADDRESS },
      { value: 200, recipient: SENDER_ADDRESS },
    ],
    locktime: 0,
    metadata: {},
  };
}

function pythonVectorTransaction(): TransactionModel {
  return {
    ...unsignedTransaction(),
    inputs: [
      {
        previousOutput: { txid: "11".repeat(32), index: 0 },
        signatureHex: EXPECTED_SIGNATURE,
        publicKeyHex: SENDER_PUBLIC_KEY,
        sequence: 0xffffffff,
      },
    ],
  };
}

describe("transaction parity", () => {
  it("matches Python address derivation for deterministic test keys", () => {
    expect(privateKeyHexToAddress(SENDER_PRIVATE_KEY)).toBe(SENDER_ADDRESS);
    expect(privateKeyHexToAddress(RECIPIENT_PRIVATE_KEY)).toBe(RECIPIENT_ADDRESS);
  });

  it("matches Python signing payload and digest", () => {
    const transaction = unsignedTransaction();
    expect(
      bytesToHex(
        serializeTransactionForSigning({
          transaction,
          inputIndex: 0,
          previousOutputValue: 1000,
          previousOutputRecipient: SENDER_ADDRESS,
        }),
      ),
    ).toBe(EXPECTED_SIGNING_PAYLOAD);

    expect(
      bytesToHex(
        transactionSignatureDigest({
          transaction,
          inputIndex: 0,
          previousOutputValue: 1000,
          previousOutputRecipient: SENDER_ADDRESS,
        }),
      ),
    ).toBe(EXPECTED_DIGEST);
  });

  it("matches Python raw transaction serialization and txid for the reference vector", () => {
    const transaction = pythonVectorTransaction();
    expect(serializeSignedTransactionToRawHex(transaction)).toBe(EXPECTED_RAW);
    expect(transactionId(transaction)).toBe(EXPECTED_TXID);
  });

  it("produces a valid low-S DER signature over the Python-parity digest", () => {
    const signatureHex = signDigestHex(SENDER_PRIVATE_KEY, EXPECTED_DIGEST);
    expect(typeof signatureHex).toBe("string");
    expect(signatureHex.startsWith("30")).toBe(true);
    const signature = secp256k1.Signature.fromDER(hexToBytes(signatureHex));
    expect(signature.hasHighS()).toBe(false);
    expect(
      secp256k1.verify(signature, hexToBytes(EXPECTED_DIGEST), hexToBytes(SENDER_PUBLIC_KEY), {
        lowS: true,
        prehash: false,
      }),
    ).toBe(true);
  });

  it("builds a real signed transaction with parity outputs and a valid signature", () => {
    const built = buildSignedPaymentTransaction({
      privateKeyHex: SENDER_PRIVATE_KEY,
      walletAddress: SENDER_ADDRESS,
      recipient: RECIPIENT_ADDRESS,
      amountChipbits: 700,
      feeChipbits: 100,
      utxos: [
        {
          txid: "11".repeat(32),
          vout: 0,
          amount_chipbits: 1000,
          coinbase: false,
          mature: true,
          status: "unspent",
          origin_height: 0,
        },
      ],
    });

    expect(built.transaction.inputs[0].publicKeyHex).toBe(SENDER_PUBLIC_KEY);
    expect(built.transaction.outputs[0]).toEqual({ value: 700, recipient: RECIPIENT_ADDRESS });
    expect(built.transaction.outputs[1]).toEqual({ value: 200, recipient: SENDER_ADDRESS });
    expect(
      bytesToHex(
        transactionSignatureDigest({
          transaction: {
            ...built.transaction,
            inputs: built.transaction.inputs.map((input) => ({ ...input, signatureHex: "", publicKeyHex: "" })),
          },
          inputIndex: 0,
          previousOutputValue: 1000,
          previousOutputRecipient: SENDER_ADDRESS,
        }),
      ),
    ).toBe(EXPECTED_DIGEST);
    const signature = secp256k1.Signature.fromDER(hexToBytes(built.transaction.inputs[0].signatureHex));
    expect(signature.hasHighS()).toBe(false);
    expect(
      secp256k1.verify(signature, hexToBytes(EXPECTED_DIGEST), hexToBytes(SENDER_PUBLIC_KEY), {
        lowS: true,
        prehash: false,
      }),
    ).toBe(true);
    expect(typeof built.rawHex).toBe("string");
    expect(built.rawHex.length % 2).toBe(0);
    expect(built.txid).toHaveLength(64);
  });
});
