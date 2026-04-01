import type { AppState } from "./app_state";

export function canSendTransactions(state: AppState): boolean {
  return !state.isLocked && !!state.address && !!state.nodeStatus && state.nodeStatus.network === state.expectedNetwork;
}

export function hasHealthyNode(state: AppState): boolean {
  return !!state.nodeStatus && state.nodeStatus.network === state.expectedNetwork;
}
