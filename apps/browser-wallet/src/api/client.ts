import { API_TIMEOUTS_MS } from "../shared/constants";
import { normalizeNodeEndpoint } from "../shared/validation";
import { ApiClientError } from "./errors";
import type {
  AddressSummary,
  AddressUtxo,
  ApiErrorPayload,
  HealthResponse,
  HistoryEntry,
  NodeStatus,
  TipResponse,
  TxLookup,
  TxSubmitResponse,
} from "./types";

export class ChipcoinApiClient {
  constructor(private readonly baseUrl: string) {}

  static fromBaseUrl(baseUrl: string): ChipcoinApiClient {
    return new ChipcoinApiClient(normalizeNodeEndpoint(baseUrl));
  }

  async health(timeoutMs = API_TIMEOUTS_MS.health): Promise<HealthResponse> {
    return this.request("/v1/health", undefined, timeoutMs);
  }

  async status(timeoutMs = API_TIMEOUTS_MS.status): Promise<NodeStatus> {
    return this.request("/v1/status", undefined, timeoutMs);
  }

  async tip(): Promise<TipResponse> {
    return this.request("/v1/tip");
  }

  async address(address: string, timeoutMs = API_TIMEOUTS_MS.summary): Promise<AddressSummary> {
    return this.request(`/v1/address/${address}`, undefined, timeoutMs);
  }

  async utxos(address: string, timeoutMs = API_TIMEOUTS_MS.utxos): Promise<AddressUtxo[]> {
    return this.request(`/v1/address/${address}/utxos`, undefined, timeoutMs);
  }

  async history(address: string, limit = 50, timeoutMs = API_TIMEOUTS_MS.history): Promise<HistoryEntry[]> {
    return this.request(`/v1/address/${address}/history?limit=${limit}&order=desc`, undefined, timeoutMs);
  }

  async tx(txid: string, timeoutMs = API_TIMEOUTS_MS.txLookup): Promise<TxLookup> {
    return this.request(`/v1/tx/${txid}`, undefined, timeoutMs);
  }

  async submitRawTransaction(rawHex: string, timeoutMs = API_TIMEOUTS_MS.txSubmit): Promise<TxSubmitResponse> {
    return this.request("/v1/tx/submit", {
      method: "POST",
      body: JSON.stringify({ raw_hex: rawHex }),
      headers: { "Content-Type": "application/json" },
    }, timeoutMs);
  }

  private async request<T>(path: string, init?: RequestInit, timeoutMs?: number): Promise<T> {
    const abortController = typeof AbortController !== "undefined" ? new AbortController() : null;
    const timeoutHandle = abortController && timeoutMs
      ? globalThis.setTimeout(() => abortController.abort(), timeoutMs)
      : null;

    try {
      const response = await fetch(`${this.baseUrl}${path}`, {
        ...init,
        signal: abortController?.signal,
      });
      const text = await response.text();
      const payload = text ? (JSON.parse(text) as T | ApiErrorPayload) : null;
      if (!response.ok) {
        if (payload && typeof payload === "object" && "error" in payload) {
          throw new ApiClientError(payload.error.message, payload.error.code, response.status);
        }
        throw new ApiClientError("Unexpected API error.", "internal_error", response.status);
      }
      return payload as T;
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") {
        throw new ApiClientError("The node API request timed out.", "request_timeout", 0);
      }
      throw error;
    } finally {
      if (timeoutHandle !== null) {
        globalThis.clearTimeout(timeoutHandle);
      }
    }
  }
}
