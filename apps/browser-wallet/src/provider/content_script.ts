import type {
  ChipcoinProviderRequestPayload,
  ChipcoinProviderResponsePayload,
} from "./types";

const CHIPCOIN_PROVIDER_REQUEST = "chipcoin:provider:request";
const CHIPCOIN_PROVIDER_RESPONSE = "chipcoin:provider:response";

const runtime = globalThis.chrome?.runtime;

injectPageProvider();

window.addEventListener("message", (event: MessageEvent<ChipcoinProviderRequestPayload>) => {
  if (event.source !== window || event.origin !== window.location.origin) {
    return;
  }
  const payload = event.data;
  if (!isProviderRequest(payload)) {
    return;
  }
  runtime?.sendMessage(
    {
      type: "provider:request",
      requestId: payload.request_id,
      method: payload.method,
      params: payload.params,
      origin: window.location.origin,
    },
    (response: Omit<ChipcoinProviderResponsePayload, "type"> | undefined) => {
      const safeResponse: ChipcoinProviderResponsePayload = {
        type: CHIPCOIN_PROVIDER_RESPONSE,
        request_id: response?.request_id === payload.request_id ? response.request_id : payload.request_id,
        result: response?.result,
        error: response?.error,
      };
      window.postMessage(safeResponse, window.location.origin);
    },
  );
});

function injectPageProvider(): void {
  if (!runtime?.getURL || location.protocol.startsWith("chrome-extension") || location.protocol.startsWith("moz-extension")) {
    return;
  }
  const script = document.createElement("script");
  script.src = runtime.getURL("assets/page_provider.js");
  script.onload = () => script.remove();
  (document.head || document.documentElement).append(script);
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
