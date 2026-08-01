import { STORAGE_KEYS } from "../shared/constants";
import { storageGet, storageSet } from "../shared/browser";
import type { ConnectedSite } from "./types";

export async function loadConnectedSites(): Promise<ConnectedSite[]> {
  return (await storageGet<ConnectedSite[]>(STORAGE_KEYS.connectedSites)) ?? [];
}

export async function isOriginConnected(origin: string): Promise<boolean> {
  return (await loadConnectedSites()).some((site) => site.origin === origin);
}

export async function rememberConnectedSite(origin: string, domain: string, now = Date.now()): Promise<ConnectedSite[]> {
  const current = await loadConnectedSites();
  const nextSite: ConnectedSite = {
    origin,
    domain,
    connectedAt: current.find((site) => site.origin === origin)?.connectedAt ?? now,
    lastUsedAt: now,
  };
  const next = [nextSite, ...current.filter((site) => site.origin !== origin)]
    .sort((left, right) => right.lastUsedAt - left.lastUsedAt);
  await storageSet(STORAGE_KEYS.connectedSites, next);
  return next;
}

export async function revokeConnectedSite(origin: string): Promise<ConnectedSite[]> {
  const next = (await loadConnectedSites()).filter((site) => site.origin !== origin);
  await storageSet(STORAGE_KEYS.connectedSites, next);
  return next;
}
