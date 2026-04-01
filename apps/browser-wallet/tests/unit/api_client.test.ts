import { afterEach, describe, expect, it, vi } from "vitest";

import { ChipcoinApiClient } from "../../src/api/client";
import { ApiClientError } from "../../src/api/errors";

describe("ChipcoinApiClient", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("turns aborted requests into request_timeout errors", async () => {
    vi.useFakeTimers();
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => new Promise<Response>((_resolve, reject) => {
      init?.signal?.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")));
    }));
    vi.stubGlobal("fetch", fetchMock);

    const client = ChipcoinApiClient.fromBaseUrl("http://127.0.0.1:8081");
    const pending = expect(client.address("CHCCfW1doC5nV2HXB3m5aJhJdiuQP8ft5dPkL", 25)).rejects.toMatchObject<ApiClientError>({
      code: "request_timeout",
      message: "The node API request timed out.",
    });

    await vi.advanceTimersByTimeAsync(25);

    await pending;
    vi.useRealTimers();
  });

  it("preserves structured API errors", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({
      error: {
        code: "validation_error",
        message: "transaction rejected",
      },
    }), {
      status: 400,
      headers: { "Content-Type": "application/json" },
    })));

    const client = ChipcoinApiClient.fromBaseUrl("http://127.0.0.1:8081");

    await expect(client.submitRawTransaction("abcd", 100)).rejects.toMatchObject<ApiClientError>({
      code: "validation_error",
      message: "transaction rejected",
      status: 400,
    });
  });
});
