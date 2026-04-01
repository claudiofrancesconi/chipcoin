import type { AppState } from "./app_state";
import type { HistoryEntry } from "../api/types";

export type BackgroundRequest =
  | { type: "wallet:getState" }
  | { type: "wallet:getHistory" }
  | { type: "wallet:create"; password: string }
  | { type: "wallet:import"; password: string; privateKeyHex: string }
  | { type: "wallet:unlock"; password: string }
  | { type: "wallet:lock" }
  | { type: "wallet:remove" }
  | { type: "wallet:exportPrivateKey"; password?: string; confirmActiveSession?: boolean }
  | { type: "wallet:updateNode"; nodeApiBaseUrl: string }
  | { type: "wallet:refresh" }
  | { type: "wallet:submit"; recipient: string; amountChipbits: number; feeChipbits: number };

export type BackgroundSuccess<T> = { ok: true; payload: T };
export type BackgroundFailure = { ok: false; error: string };
export type BackgroundResponse<T> = BackgroundSuccess<T> | BackgroundFailure;

export type WalletStateResponse = BackgroundResponse<AppState>;
export type ExportPrivateKeyResponse = BackgroundResponse<{ privateKeyHex: string }>;
export type SubmitTransactionResponse = BackgroundResponse<{ status: "submitted" | "rejected" | "failed_to_submit"; txid?: string }>;
export type HistoryResponse = BackgroundResponse<HistoryEntry[]>;
