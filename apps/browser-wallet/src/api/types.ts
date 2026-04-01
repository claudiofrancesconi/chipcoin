export interface HealthResponse {
  status: "ok";
}

export interface RewardWinner {
  node_id: string;
  payout_address: string;
  reward_chipbits: number;
  score_hex: string;
}

export interface NodeStatus {
  network: string;
  network_magic_hex: string;
  height: number | null;
  tip_hash: string | null;
  current_bits: number;
  current_target: string;
  current_difficulty_ratio: string;
  expected_next_bits: number;
  expected_next_target: string;
  cumulative_work: number | null;
  mempool_size: number;
  peer_count: number;
  handshaken_peer_count: number;
  next_block_reward_winners: RewardWinner[];
}

export interface TipResponse {
  height: number | null;
  block_hash: string | null;
}

export interface AddressSummary {
  address: string;
  confirmed_balance_chipbits: number;
  immature_balance_chipbits: number;
  spendable_balance_chipbits: number;
  utxo_count: number;
}

export interface AddressUtxo {
  txid: string;
  vout: number;
  amount_chipbits: number;
  coinbase: boolean;
  mature: boolean;
  status: string;
  origin_height: number | null;
}

export interface HistoryEntry {
  block_height: number;
  block_hash: string;
  txid: string;
  incoming_chipbits: number;
  outgoing_chipbits: number;
  net_chipbits: number;
  timestamp: number | null;
}

export interface TxLookup {
  location: string;
  block_hash: string | null;
  height: number | null;
  transaction: {
    txid: string;
    version: number;
    locktime: number;
    inputs: Array<{
      txid: string;
      index: number;
      sequence: number;
      signature_hex: string | null;
      public_key_hex: string | null;
    }>;
    outputs: Array<{
      value: number;
      recipient: string;
    }>;
    metadata: Record<string, string>;
  };
}

export interface TxSubmitResponse {
  accepted: true;
  txid: string;
  fee: number;
}

export interface ApiErrorPayload {
  error: {
    code: string;
    message: string;
  };
}
