import type { AppState } from "./app_state";
import type { HistoryEntry } from "../api/types";
import type { SupportedNetworkId } from "../shared/constants";
import type { ChipcoinProviderError, ChipcoinSignedLoginResponse, ConnectedSite } from "../provider/types";

export interface ProviderRuntimeRequest {
  type: "provider:request";
  requestId: string;
  method: string;
  params?: unknown;
  origin: string;
}

export interface ProviderApprovalGetRequest {
  type: "provider:approval:get";
  approvalId: string;
}

export interface ProviderApprovalRespondRequest {
  type: "provider:approval:respond";
  approvalId: string;
  approved: boolean;
}

export type BackgroundRequest =
  | { type: "wallet:getState" }
  | { type: "wallet:getHistory" }
  | { type: "wallet:generateRecoveryPhrase" }
  | { type: "wallet:create"; password: string }
  | { type: "wallet:createFromSeed"; password: string; recoveryPhrase: string }
  | { type: "wallet:import"; password: string; privateKeyHex: string }
  | { type: "wallet:recoverFromSeed"; password: string; recoveryPhrase: string }
  | { type: "wallet:unlock"; password: string }
  | { type: "wallet:lock" }
  | { type: "wallet:remove" }
  | { type: "wallet:exportPrivateKey"; password?: string; confirmActiveSession?: boolean }
  | { type: "wallet:exportRecoveryPhrase"; password?: string; confirmActiveSession?: boolean }
  | { type: "wallet:updateNode"; nodeApiBaseUrl: string; expectedNetwork: SupportedNetworkId; autoLockMinutes?: number }
  | { type: "wallet:refresh" }
  | { type: "wallet:addWatchOnlyAddress"; address: string; label?: string }
  | { type: "wallet:removeWatchOnlyAddress"; address: string }
  | { type: "wallet:submit"; recipient: string; amountChipbits: number; feeChipbits: number }
  | { type: "wallet:listConnectedSites" }
  | { type: "wallet:revokeConnectedSite"; origin: string }
  | ProviderRuntimeRequest
  | ProviderApprovalGetRequest
  | ProviderApprovalRespondRequest;

export type BackgroundSuccess<T> = { ok: true; payload: T };
export type BackgroundFailure = { ok: false; error: string };
export type BackgroundResponse<T> = BackgroundSuccess<T> | BackgroundFailure;

export type WalletStateResponse = BackgroundResponse<AppState>;
export type ExportPrivateKeyResponse = BackgroundResponse<{ privateKeyHex: string }>;
export type SubmitTransactionResponse = BackgroundResponse<{ status: "submitted" | "rejected" | "failed_to_submit"; txid?: string }>;
export type HistoryResponse = BackgroundResponse<HistoryEntry[]>;
export type ProviderRuntimeResponse = {
  request_id: string;
  result?: unknown;
  error?: ChipcoinProviderError;
};
export type ProviderApprovalSummary =
  | {
    id: string;
    kind: "connect";
    origin: string;
    domain: string;
    address: string;
  }
  | {
    id: string;
    kind: "signMessage";
    origin: string;
    domain: string;
    address: string;
    network: "testnet";
    scheme: 0;
    issued_at: string;
    expires_at: string;
    statement: string;
    message: string;
  };
export type ProviderApprovalGetResponse = BackgroundResponse<ProviderApprovalSummary>;
export type ConnectedSitesResponse = BackgroundResponse<ConnectedSite[]>;
export type SignMessageProviderResponse = BackgroundResponse<ChipcoinSignedLoginResponse>;
