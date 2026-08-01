export const CHIPCOIN_PROVIDER_REQUEST = "chipcoin:provider:request";
export const CHIPCOIN_PROVIDER_RESPONSE = "chipcoin:provider:response";

export type ChipcoinProviderMethod = "chipcoin_connect" | "chipcoin_getAddress" | "chipcoin_signMessage";

export type ChipcoinProviderErrorCode =
  | "USER_REJECTED"
  | "WALLET_LOCKED"
  | "WALLET_NOT_FOUND"
  | "UNSUPPORTED_METHOD"
  | "UNSUPPORTED_NETWORK"
  | "INVALID_MESSAGE"
  | "ORIGIN_MISMATCH"
  | "ADDRESS_MISMATCH"
  | "SCHEME_MISMATCH"
  | "PERMISSION_DENIED"
  | "SIGNING_FAILED";

export interface ChipcoinProviderError {
  code: ChipcoinProviderErrorCode;
  message: string;
}

export interface ChipcoinProviderRequestPayload {
  type: typeof CHIPCOIN_PROVIDER_REQUEST;
  request_id: string;
  method: string;
  params?: unknown;
}

export interface ChipcoinProviderResponsePayload {
  type: typeof CHIPCOIN_PROVIDER_RESPONSE;
  request_id: string;
  result?: unknown;
  error?: ChipcoinProviderError;
}

export interface ChipcoinSignMessageParams {
  message: string;
  domain: string;
}

export interface ChipcoinSignedLoginResponse {
  address: string;
  signature_scheme: 0;
  public_key: string;
  signature: string;
  message: string;
}

export interface ConnectedSite {
  origin: string;
  domain: string;
  connectedAt: number;
  lastUsedAt: number;
}
