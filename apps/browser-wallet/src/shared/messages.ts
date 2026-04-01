import { extensionRuntime } from "./browser";
import type { BackgroundRequest, BackgroundResponse } from "../state/actions";

export async function sendWalletMessage<T>(message: BackgroundRequest): Promise<T> {
  const runtime = extensionRuntime();
  const response = await new Promise<BackgroundResponse<T>>((resolve) => {
    runtime.sendMessage(message, (payload) => resolve(payload as BackgroundResponse<T>));
  });
  if (!response.ok) {
    throw new Error(response.error);
  }
  return response.payload;
}
