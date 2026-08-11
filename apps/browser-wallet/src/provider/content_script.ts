import type {
  ChipcoinProviderRequestPayload,
  ChipcoinProviderResponsePayload,
} from "./types";

const CHIPCOIN_PROVIDER_REQUEST = "chipcoin:provider:request";
const CHIPCOIN_PROVIDER_RESPONSE = "chipcoin:provider:response";

type RuntimeLike = typeof chrome.runtime;

interface ContentScriptBridgeState {
  listener?: (event: MessageEvent<ChipcoinProviderRequestPayload>) => void;
  cleanupListener?: () => void;
  providerInjected?: boolean;
}

declare global {
  // eslint-disable-next-line no-var
  var __chipcoinContentScriptBridge: ContentScriptBridgeState | undefined;
}

const runtime = extensionRuntime();
const bridgeState = globalThis.__chipcoinContentScriptBridge ??= {};

if (bridgeState.listener) {
  window.removeEventListener("message", bridgeState.listener);
}
if (bridgeState.cleanupListener) {
  window.removeEventListener("pagehide", bridgeState.cleanupListener);
}

injectPageProvider();

const listener = (event: MessageEvent<ChipcoinProviderRequestPayload>) => {
  if (event.source !== window || event.origin !== window.location.origin) {
    return;
  }
  const payload = event.data;
  if (!isProviderRequest(payload)) {
    return;
  }
  if (!runtime?.sendMessage) {
    postProviderError(payload.request_id, "Chipcoin wallet background is not available.");
    return;
  }
  runtime.sendMessage(
    {
      type: "provider:request",
      requestId: payload.request_id,
      method: payload.method,
      params: payload.params,
      origin: window.location.origin,
    },
    (response: Omit<ChipcoinProviderResponsePayload, "type"> | undefined) => {
      const runtimeError = runtime.lastError;
      if (runtimeError) {
        postProviderError(payload.request_id, runtimeError.message || "Chipcoin wallet background is not available.");
        return;
      }
      const safeResponse: ChipcoinProviderResponsePayload = {
        type: CHIPCOIN_PROVIDER_RESPONSE,
        request_id: response?.request_id === payload.request_id ? response.request_id : payload.request_id,
        result: response?.result,
        error: response?.error,
      };
      window.postMessage(safeResponse, window.location.origin);
    },
  );
};

const cleanupListener = () => {
  window.removeEventListener("message", listener);
  window.removeEventListener("pagehide", cleanupListener);
  if (globalThis.__chipcoinContentScriptBridge?.listener === listener) {
    globalThis.__chipcoinContentScriptBridge = { providerInjected: bridgeState.providerInjected };
  }
};

bridgeState.listener = listener;
bridgeState.cleanupListener = cleanupListener;
window.addEventListener("message", listener);
window.addEventListener("pagehide", cleanupListener, { once: true });

function injectPageProvider(): void {
  if (!runtime?.getURL || location.protocol.startsWith("chrome-extension") || location.protocol.startsWith("moz-extension")) {
    return;
  }
  if (bridgeState.providerInjected || document.documentElement.dataset.chipcoinProviderInjected === "true") {
    return;
  }
  bridgeState.providerInjected = true;
  document.documentElement.dataset.chipcoinProviderInjected = "true";
  const script = document.createElement("script");
  script.src = runtime.getURL("assets/page_provider.js");
  script.onload = () => script.remove();
  script.onerror = () => {
    bridgeState.providerInjected = false;
    delete document.documentElement.dataset.chipcoinProviderInjected;
    script.remove();
  };
  (document.head || document.documentElement).append(script);
}

function extensionRuntime(): RuntimeLike | undefined {
  return globalThis.chrome?.runtime ?? (globalThis as { browser?: { runtime?: RuntimeLike } }).browser?.runtime;
}

function postProviderError(requestId: string, message: string): void {
  const response: ChipcoinProviderResponsePayload = {
    type: CHIPCOIN_PROVIDER_RESPONSE,
    request_id: requestId,
    error: {
      code: "SIGNING_FAILED",
      message,
    },
  };
  window.postMessage(response, window.location.origin);
}

function isProviderRequest(value: unknown): value is ChipcoinProviderRequestPayload {
  if (!value || typeof value !== "object") {
    return false;
  }
  const record = value as Record<string, unknown>;
  return record.type === CHIPCOIN_PROVIDER_REQUEST
    && typeof record.request_id === "string"
    && typeof record.method === "string";
}
