import {
  CHIPCOIN_PROVIDER_REQUEST,
  CHIPCOIN_PROVIDER_RESPONSE,
  type ChipcoinProviderRequestPayload,
  type ChipcoinProviderResponsePayload,
} from "./types";

interface ChipcoinRequestArgs {
  method: string;
  params?: unknown;
}

interface ChipcoinProvider {
  request(args: ChipcoinRequestArgs): Promise<unknown>;
  connect(): Promise<unknown>;
  getAddress(): Promise<unknown>;
  signMessage(message: string): Promise<unknown>;
}

declare global {
  interface Window {
    chipcoin?: ChipcoinProvider;
  }
}

const pendingRequests = new Map<string, {
  resolve(value: unknown): void;
  reject(error: unknown): void;
  timeout: number;
}>();

function request(args: ChipcoinRequestArgs): Promise<unknown> {
  if (!args || typeof args.method !== "string") {
    return Promise.reject(new Error("Chipcoin provider request requires a method."));
  }
  const requestId = crypto.randomUUID();
  const payload: ChipcoinProviderRequestPayload = {
    type: CHIPCOIN_PROVIDER_REQUEST,
    request_id: requestId,
    method: args.method,
    params: args.params,
  };
  return new Promise((resolve, reject) => {
    const timeout = window.setTimeout(() => {
      pendingRequests.delete(requestId);
      reject(new Error("Chipcoin provider request timed out."));
    }, 120_000);
    pendingRequests.set(requestId, { resolve, reject, timeout });
    window.postMessage(payload, window.location.origin);
  });
}

window.addEventListener("message", (event: MessageEvent<ChipcoinProviderResponsePayload>) => {
  if (event.source !== window || event.origin !== window.location.origin) {
    return;
  }
  const payload = event.data;
  if (!payload || payload.type !== CHIPCOIN_PROVIDER_RESPONSE || typeof payload.request_id !== "string") {
    return;
  }
  const pending = pendingRequests.get(payload.request_id);
  if (!pending) {
    return;
  }
  pendingRequests.delete(payload.request_id);
  window.clearTimeout(pending.timeout);
  if (payload.error) {
    pending.reject(payload.error);
  } else {
    pending.resolve(payload.result);
  }
});

if (!window.chipcoin) {
  window.chipcoin = {
    request,
    connect: () => request({ method: "chipcoin_connect" }),
    getAddress: () => request({ method: "chipcoin_getAddress" }),
    signMessage: (message: string) => request({
      method: "chipcoin_signMessage",
      params: { message, domain: window.location.hostname },
    }),
  };
  window.dispatchEvent(new Event("chipcoin_providerReady"));
}
