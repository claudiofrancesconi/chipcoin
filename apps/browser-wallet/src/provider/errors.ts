import type { ChipcoinProviderError, ChipcoinProviderErrorCode } from "./types";

const DEFAULT_MESSAGES: Record<ChipcoinProviderErrorCode, string> = {
  USER_REJECTED: "User rejected the signing request.",
  WALLET_LOCKED: "Unlock the wallet before using it with this site.",
  WALLET_NOT_FOUND: "No wallet is configured yet.",
  UNSUPPORTED_METHOD: "Unsupported Chipcoin provider method.",
  UNSUPPORTED_NETWORK: "Unsupported Chipcoin network.",
  INVALID_MESSAGE: "The login message is invalid.",
  ORIGIN_MISMATCH: "The requesting origin does not match the login message.",
  ADDRESS_MISMATCH: "The login message address does not match the wallet.",
  SCHEME_MISMATCH: "The login message signature scheme does not match the wallet.",
  PERMISSION_DENIED: "This site is not connected to the wallet.",
  SIGNING_FAILED: "Unable to sign the login message.",
};

export function providerError(code: ChipcoinProviderErrorCode, message = DEFAULT_MESSAGES[code]): ChipcoinProviderError {
  return { code, message };
}

export function errorCodeFromUnknown(error: unknown, fallback: ChipcoinProviderErrorCode): ChipcoinProviderErrorCode {
  if (error instanceof Error && error.message in DEFAULT_MESSAGES) {
    return error.message as ChipcoinProviderErrorCode;
  }
  return fallback;
}
