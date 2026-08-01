import { extensionRuntime } from "../shared/browser";
import { isOriginConnected, rememberConnectedSite } from "../provider/permissions";
import { providerError, errorCodeFromUnknown } from "../provider/errors";
import {
  parseChipcoinSignedLoginMessage,
  validateLoginOriginBinding,
} from "../provider/login_message";
import type {
  ChipcoinSignMessageParams,
  ChipcoinProviderError,
} from "../provider/types";
import type {
  ProviderApprovalSummary,
  ProviderRuntimeRequest,
  ProviderRuntimeResponse,
} from "../state/actions";
import { getProviderAddress, signProviderLoginMessage } from "./session";

type PendingApproval = ProviderApprovalSummary & {
  resolve(value: boolean): void;
  expiresAt: number;
};

const pendingApprovals = new Map<string, PendingApproval>();

export async function handleProviderRuntimeRequest(
  request: ProviderRuntimeRequest,
  sender: chrome.runtime.MessageSender,
): Promise<ProviderRuntimeResponse> {
  const originError = validateSenderOrigin(request, sender);
  if (originError) {
    return withError(request.requestId, originError);
  }
  const origin = canonicalOrigin(request.origin);

  try {
    switch (request.method) {
      case "chipcoin_connect":
        return { request_id: request.requestId, result: await connectOrigin(origin) };
      case "chipcoin_getAddress":
        return { request_id: request.requestId, result: await getAddressForOrigin(origin) };
      case "chipcoin_signMessage":
        return { request_id: request.requestId, result: await signMessageForOrigin(origin, request.params) };
      default:
        return withError(request.requestId, providerError("UNSUPPORTED_METHOD"));
    }
  } catch (error) {
    return withError(request.requestId, providerError(errorCodeFromUnknown(error, "SIGNING_FAILED")));
  }
}

export function getPendingProviderApproval(approvalId: string): ProviderApprovalSummary | null {
  const pending = pendingApprovals.get(approvalId);
  if (!pending || pending.expiresAt < Date.now()) {
    pendingApprovals.delete(approvalId);
    return null;
  }
  const { resolve: _resolve, expiresAt: _expiresAt, ...summary } = pending;
  return summary;
}

export function respondToProviderApproval(approvalId: string, approved: boolean): void {
  const pending = pendingApprovals.get(approvalId);
  if (!pending) {
    return;
  }
  pendingApprovals.delete(approvalId);
  pending.resolve(approved);
}

async function connectOrigin(origin: string): Promise<{ address: string }> {
  const address = await getProviderAddress();
  if (await isOriginConnected(origin)) {
    await rememberConnectedSite(origin, new URL(origin).hostname);
    return { address };
  }
  const approved = await requestApproval({
    id: crypto.randomUUID(),
    kind: "connect",
    origin,
    domain: new URL(origin).hostname,
    address,
  });
  if (!approved) {
    throw new Error("USER_REJECTED");
  }
  await rememberConnectedSite(origin, new URL(origin).hostname);
  return { address };
}

async function getAddressForOrigin(origin: string): Promise<{ address: string }> {
  if (!(await isOriginConnected(origin))) {
    return connectOrigin(origin);
  }
  await rememberConnectedSite(origin, new URL(origin).hostname);
  return { address: await getProviderAddress() };
}

async function signMessageForOrigin(origin: string, params: unknown): Promise<unknown> {
  if (!(await isOriginConnected(origin))) {
    throw new Error("PERMISSION_DENIED");
  }
  if (!isSignMessageParams(params)) {
    throw new Error("INVALID_MESSAGE");
  }
  const parsed = parseChipcoinSignedLoginMessage(params.message);
  validateLoginOriginBinding(parsed, origin, params.domain);
  const address = await getProviderAddress();
  if (parsed.address !== address) {
    throw new Error("ADDRESS_MISMATCH");
  }
  const approved = await requestApproval({
    id: crypto.randomUUID(),
    kind: "signMessage",
    origin,
    domain: parsed.domain,
    address: parsed.address,
    network: parsed.network,
    scheme: parsed.scheme,
    issued_at: parsed.issuedAt,
    expires_at: parsed.expiresAt,
    statement: parsed.statement,
    message: params.message,
  });
  if (!approved) {
    throw new Error("USER_REJECTED");
  }
  return signProviderLoginMessage({
    message: params.message,
    origin,
    domain: params.domain,
  });
}

function requestApproval(summary: ProviderApprovalSummary): Promise<boolean> {
  return new Promise((resolve) => {
    pendingApprovals.set(summary.id, {
      ...summary,
      resolve,
      expiresAt: Date.now() + 120_000,
    });
    openApprovalWindow(summary.id);
  });
}

function openApprovalWindow(approvalId: string): void {
  const url = extensionRuntime().getURL(`approval.html?id=${encodeURIComponent(approvalId)}`);
  const windowsApi = globalThis.chrome?.windows ?? (globalThis as { browser?: { windows?: typeof chrome.windows } }).browser?.windows;
  if (windowsApi?.create) {
    windowsApi.create({ url, type: "popup", width: 420, height: 620 });
    return;
  }
  extensionRuntime().openOptionsPage?.();
}

function validateSenderOrigin(request: ProviderRuntimeRequest, sender: chrome.runtime.MessageSender): ChipcoinProviderError | null {
  if (!sender.tab || sender.frameId !== 0 || !sender.url || typeof request.origin !== "string") {
    return providerError("ORIGIN_MISMATCH");
  }
  try {
    const senderOrigin = new URL(sender.url).origin;
    if (senderOrigin !== request.origin) {
      return providerError("ORIGIN_MISMATCH");
    }
  } catch {
    return providerError("ORIGIN_MISMATCH");
  }
  return null;
}

function canonicalOrigin(origin: string): string {
  return new URL(origin).origin;
}

function withError(requestId: string, error: ChipcoinProviderError): ProviderRuntimeResponse {
  return { request_id: requestId, error };
}

function isSignMessageParams(value: unknown): value is ChipcoinSignMessageParams {
  if (!value || typeof value !== "object") {
    return false;
  }
  const record = value as Record<string, unknown>;
  return typeof record.message === "string" && typeof record.domain === "string";
}
