import { isValidAddress, privateKeyHexToAddress } from "../crypto/addresses";
import {
  serializeSignedTransactionToRawHex,
  transactionId,
  transactionSignatureDigest,
} from "../crypto/serialization";
import { bytesToHex } from "../crypto/keys";
import { signDigestHex, walletKeyMaterialFromPrivateKeyHex } from "../crypto/signing";
import type { AddressUtxo } from "../api/types";
import type { BuiltTransaction, SendPlan, SpendCandidate, TransactionModel } from "./models";
import { selectInputs } from "./selection";

export function filterSpendableCandidates(address: string, utxos: AddressUtxo[]): SpendCandidate[] {
  return utxos
    .filter((utxo) => utxo.status === "unspent" && utxo.mature)
    .map((utxo) => ({
      txid: utxo.txid,
      index: utxo.vout,
      amountChipbits: utxo.amount_chipbits,
      recipient: address,
    }));
}

export function buildSendPlan(args: {
  walletAddress: string;
  recipient: string;
  amountChipbits: number;
  feeChipbits: number;
  utxos: AddressUtxo[];
}): SendPlan {
  const { walletAddress, recipient, amountChipbits, feeChipbits, utxos } = args;
  if (amountChipbits <= 0) {
    throw new Error("Amount must be positive.");
  }
  if (feeChipbits < 0) {
    throw new Error("Fee cannot be negative.");
  }
  if (!isValidAddress(recipient)) {
    throw new Error("Recipient must be a valid CHC address.");
  }
  const selection = selectInputs(filterSpendableCandidates(walletAddress, utxos), amountChipbits + feeChipbits);
  return {
    recipient,
    amountChipbits,
    feeChipbits,
    changeRecipient: walletAddress,
    selectedInputs: selection.selected,
    totalInputChipbits: selection.totalInputChipbits,
    changeChipbits: selection.changeChipbits,
  };
}

export function buildSignedPaymentTransaction(args: {
  privateKeyHex: string;
  walletAddress: string;
  recipient: string;
  amountChipbits: number;
  feeChipbits: number;
  utxos: AddressUtxo[];
}): BuiltTransaction {
  const plan = buildSendPlan(args);
  const keyMaterial = walletKeyMaterialFromPrivateKeyHex(args.privateKeyHex);
  const derivedAddress = privateKeyHexToAddress(args.privateKeyHex);
  if (args.walletAddress !== derivedAddress) {
    throw new Error("Wallet address does not match the provided private key.");
  }

  const unsigned: TransactionModel = {
    version: 1,
    inputs: plan.selectedInputs.map((input) => ({
      previousOutput: {
        txid: input.txid,
        index: input.index,
      },
      signatureHex: "",
      publicKeyHex: "",
      sequence: 0xffffffff,
    })),
    outputs: [
      {
        value: plan.amountChipbits,
        recipient: plan.recipient,
      },
      ...(plan.changeChipbits > 0
        ? [{
            value: plan.changeChipbits,
            recipient: plan.changeRecipient,
          }]
        : []),
    ],
    locktime: 0,
    metadata: {},
  };

  const signedInputs = unsigned.inputs.map((input, index) => {
    const candidate = plan.selectedInputs[index];
      if (candidate.recipient !== derivedAddress) {
        throw new Error("Spend candidate recipient does not belong to this wallet key.");
      }
    const digestHex = bytesToHex(
      transactionSignatureDigest({
        transaction: unsigned,
        inputIndex: index,
        previousOutputValue: candidate.amountChipbits,
        previousOutputRecipient: candidate.recipient,
      }),
    );
    return {
      ...input,
      signatureHex: signDigestHex(args.privateKeyHex, digestHex),
      publicKeyHex: keyMaterial.publicKeyHex,
    };
  });

  const signed: TransactionModel = {
    ...unsigned,
    inputs: signedInputs,
  };

  return {
    transaction: signed,
    rawHex: serializeSignedTransactionToRawHex(signed),
    txid: transactionId(signed),
    feeChipbits: plan.feeChipbits,
    changeChipbits: plan.changeChipbits,
  };
}
