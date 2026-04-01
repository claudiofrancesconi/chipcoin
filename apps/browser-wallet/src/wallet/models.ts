export interface SpendCandidate {
  txid: string;
  index: number;
  amountChipbits: number;
  recipient: string;
}

export interface SelectionResult {
  selected: SpendCandidate[];
  totalInputChipbits: number;
  changeChipbits: number;
}

export interface SendPlan {
  recipient: string;
  amountChipbits: number;
  feeChipbits: number;
  changeRecipient: string;
  selectedInputs: SpendCandidate[];
  totalInputChipbits: number;
  changeChipbits: number;
}

export interface TxOutPoint {
  txid: string;
  index: number;
}

export interface TxInputModel {
  previousOutput: TxOutPoint;
  signatureHex: string;
  publicKeyHex: string;
  sequence: number;
}

export interface TxOutputModel {
  value: number;
  recipient: string;
}

export interface TransactionModel {
  version: number;
  inputs: TxInputModel[];
  outputs: TxOutputModel[];
  locktime: number;
  metadata: Record<string, string>;
}

export interface BuiltTransaction {
  transaction: TransactionModel;
  rawHex: string;
  txid: string;
  feeChipbits: number;
  changeChipbits: number;
}
