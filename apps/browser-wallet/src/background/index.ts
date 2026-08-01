import type { BackgroundRequest, BackgroundResponse } from "../state/actions";
import {
  addWatchOnlyAddress,
  createWallet,
  createWalletFromSeed,
  exportPrivateKey,
  exportRecoveryPhrase,
  generateWalletRecoveryPhrase,
  getWalletHistory,
  getAppState,
  handleAutoLockAlarm,
  initializeBackground,
  importWallet,
  lockWallet,
  removeWallet,
  removeWatchOnlyAddress,
  refreshWalletData,
  recoverWalletFromSeed,
  submitTransaction,
  unlockWallet,
  updateNodeEndpoint,
} from "./session";
import { extensionAlarms, extensionRuntime } from "../shared/browser";
import { loadConnectedSites, revokeConnectedSite } from "../provider/permissions";
import {
  getPendingProviderApproval,
  handleProviderRuntimeRequest,
  respondToProviderApproval,
} from "./provider";

const runtime = extensionRuntime();

void initializeBackground();
runtime.onStartup?.addListener(() => {
  void initializeBackground();
});

runtime.onMessage.addListener((message: BackgroundRequest, sender, sendResponse) => {
  void handleMessage(message, sender).then(sendResponse);
  return true;
});

extensionAlarms().onAlarm.addListener((alarm) => {
  void handleAutoLockAlarm(alarm.name);
});

async function handleMessage(message: BackgroundRequest, sender: chrome.runtime.MessageSender): Promise<BackgroundResponse<unknown> | unknown> {
  try {
    switch (message.type) {
      case "provider:request":
        return handleProviderRuntimeRequest(message, sender);
      case "provider:approval:get": {
        const pending = getPendingProviderApproval(message.approvalId);
        return pending ? { ok: true, payload: pending } : { ok: false, error: "Approval request expired or was already handled." };
      }
      case "provider:approval:respond":
        respondToProviderApproval(message.approvalId, message.approved);
        return { ok: true, payload: { approved: message.approved } };
      case "wallet:getState":
        return { ok: true, payload: await getAppState() };
      case "wallet:getHistory":
        return { ok: true, payload: await getWalletHistory() };
      case "wallet:generateRecoveryPhrase":
        return { ok: true, payload: { recoveryPhrase: generateWalletRecoveryPhrase() } };
      case "wallet:create":
        return { ok: true, payload: await createWallet(message.password) };
      case "wallet:createFromSeed":
        return { ok: true, payload: await createWalletFromSeed(message.recoveryPhrase, message.password) };
      case "wallet:import":
        return { ok: true, payload: await importWallet(message.privateKeyHex, message.password) };
      case "wallet:recoverFromSeed":
        return { ok: true, payload: await recoverWalletFromSeed(message.recoveryPhrase, message.password) };
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
      case "wallet:exportRecoveryPhrase":
        return {
          ok: true,
          payload: {
            recoveryPhrase: await exportRecoveryPhrase({
              password: message.password,
              confirmActiveSession: message.confirmActiveSession,
            }),
          },
        };
      case "wallet:updateNode":
        return { ok: true, payload: await updateNodeEndpoint(message.nodeApiBaseUrl, message.expectedNetwork, message.autoLockMinutes) };
      case "wallet:refresh":
        return { ok: true, payload: await refreshWalletData() };
      case "wallet:addWatchOnlyAddress":
        return { ok: true, payload: await addWatchOnlyAddress(message) };
      case "wallet:removeWatchOnlyAddress":
        return { ok: true, payload: await removeWatchOnlyAddress(message.address) };
      case "wallet:submit":
        return { ok: true, payload: await submitTransaction(message) };
      case "wallet:listConnectedSites":
        return { ok: true, payload: await loadConnectedSites() };
      case "wallet:revokeConnectedSite":
        return { ok: true, payload: await revokeConnectedSite(message.origin) };
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
