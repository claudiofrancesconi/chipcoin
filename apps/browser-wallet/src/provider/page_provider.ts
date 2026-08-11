import type {
  ChipcoinProviderRequestPayload,
  ChipcoinProviderResponsePayload,
} from "./types";

const CHIPCOIN_PROVIDER_REQUEST = "chipcoin:provider:request";
const CHIPCOIN_PROVIDER_RESPONSE = "chipcoin:provider:response";

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
    __chipcoinProviderState?: ProviderPageState;
  }
}

interface PendingRequest {
  resolve(value: unknown): void;
  reject(error: unknown): void;
  timeout: number;
}

interface ProviderPageState {
  installed?: boolean;
  pendingRequests: Map<string, PendingRequest>;
  listener?: (event: MessageEvent<ChipcoinProviderResponsePayload>) => void;
  cleanupListener?: () => void;
}

const providerState = window.__chipcoinProviderState ??= {
  pendingRequests: new Map<string, PendingRequest>(),
};
const pendingRequests = providerState.pendingRequests;

if (providerState.installed) {
  window.dispatchEvent(new Event("chipcoin_providerReady"));
} else {
  providerState.installed = true;
  installProvider();
}

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

function installProvider(): void {
  const listener = (event: MessageEvent<ChipcoinProviderResponsePayload>) => {
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
  };

  const cleanupListener = () => {
    window.removeEventListener("message", listener);
    window.removeEventListener("pagehide", cleanupListener);
    for (const [requestId, pending] of pendingRequests.entries()) {
      pendingRequests.delete(requestId);
      window.clearTimeout(pending.timeout);
      pending.reject(new Error("Chipcoin provider disconnected."));
    }
    providerState.installed = false;
    providerState.listener = undefined;
    providerState.cleanupListener = undefined;
  };

  providerState.listener = listener;
  providerState.cleanupListener = cleanupListener;
  window.addEventListener("message", listener);
  window.addEventListener("pagehide", cleanupListener, { once: true });

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
  }
  window.dispatchEvent(new Event("chipcoin_providerReady"));
}
