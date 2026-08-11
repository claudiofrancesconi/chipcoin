import { extensionRuntime } from "./browser";
import type { BackgroundRequest, BackgroundResponse } from "../state/actions";

export async function sendWalletMessage<T>(message: BackgroundRequest): Promise<T> {
  const runtime = extensionRuntime();
  const response = await new Promise<BackgroundResponse<T>>((resolve) => {
    runtime.sendMessage(message, (payload) => {
      const runtimeError = runtime.lastError;
      if (runtimeError) {
        resolve({
          ok: false,
          error: runtimeError.message || "Extension background is not available.",
        });
        return;
      }
      resolve(payload as BackgroundResponse<T>);
    });
  });
  if (!response.ok) {
    throw new Error(response.error);
  }
  return response.payload;
}
