import type { BackgroundRequest, BackgroundResponse } from "../state/actions";
import {
  createWallet,
  exportPrivateKey,
  getWalletHistory,
  getAppState,
  handleAutoLockAlarm,
  initializeBackground,
  importWallet,
  lockWallet,
  removeWallet,
  refreshWalletData,
  submitTransaction,
  unlockWallet,
  updateNodeEndpoint,
} from "./session";
import { extensionAlarms, extensionRuntime } from "../shared/browser";

const runtime = extensionRuntime();

void initializeBackground();
runtime.onStartup?.addListener(() => {
  void initializeBackground();
});

runtime.onMessage.addListener((message: BackgroundRequest, _sender, sendResponse) => {
  void handleMessage(message).then(sendResponse);
  return true;
});

extensionAlarms().onAlarm.addListener((alarm) => {
  void handleAutoLockAlarm(alarm.name);
});

async function handleMessage(message: BackgroundRequest): Promise<BackgroundResponse<unknown>> {
  try {
    switch (message.type) {
      case "wallet:getState":
        return { ok: true, payload: await getAppState() };
      case "wallet:getHistory":
        return { ok: true, payload: await getWalletHistory() };
      case "wallet:create":
        return { ok: true, payload: await createWallet(message.password) };
      case "wallet:import":
        return { ok: true, payload: await importWallet(message.privateKeyHex, message.password) };
      case "wallet:unlock":
        return { ok: true, payload: await unlockWallet(message.password) };
      case "wallet:lock":
        return { ok: true, payload: await lockWallet() };
      case "wallet:remove":
        return { ok: true, payload: await removeWallet() };
      case "wallet:exportPrivateKey":
        return {
          ok: true,
          payload: {
            privateKeyHex: await exportPrivateKey({
              password: message.password,
              confirmActiveSession: message.confirmActiveSession,
            }),
          },
        };
      case "wallet:updateNode":
        return { ok: true, payload: await updateNodeEndpoint(message.nodeApiBaseUrl) };
      case "wallet:refresh":
        return { ok: true, payload: await refreshWalletData() };
      case "wallet:submit":
        return { ok: true, payload: await submitTransaction(message) };
      default:
        return { ok: false, error: "Unsupported wallet action." };
    }
  } catch (error) {
    return {
      ok: false,
      error: error instanceof Error ? error.message : "Unknown wallet error.",
    };
  }
}
