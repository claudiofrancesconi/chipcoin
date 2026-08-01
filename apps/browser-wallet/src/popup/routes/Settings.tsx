import { useEffect, useState } from "react";

import type { AppState } from "../../state/app_state";
import { AUTO_LOCK_MINUTES_OPTIONS, SUPPORTED_NETWORKS, getSupportedNetwork, type SupportedNetworkId } from "../../shared/constants";
import { sendWalletMessage } from "../../shared/messages";
import type { ConnectedSite } from "../../provider/types";

export function Settings(
  { state, onUpdated, onOpenBackup }: { state: AppState; onUpdated(state: AppState): void; onOpenBackup(): void },
): JSX.Element {
  const [nodeApiBaseUrl, setNodeApiBaseUrl] = useState(state.nodeApiBaseUrl);
  const [expectedNetwork, setExpectedNetwork] = useState<SupportedNetworkId>(state.expectedNetwork);
  const [autoLockMinutes, setAutoLockMinutes] = useState(state.autoLockMinutes);
  const [message, setMessage] = useState<string | null>(null);
  const [connectedSites, setConnectedSites] = useState<ConnectedSite[]>([]);
  const selectedNetwork = getSupportedNetwork(expectedNetwork);

  useEffect(() => {
    void loadConnectedSites();
  }, []);

  function handleNetworkChange(networkId: SupportedNetworkId): void {
    const network = getSupportedNetwork(networkId);
    setExpectedNetwork(network.id);
    setNodeApiBaseUrl(network.defaultNodeApiBaseUrl);
    setMessage(null);
  }

  async function handleSave(): Promise<void> {
    try {
      const nextState = await sendWalletMessage<AppState>({
        type: "wallet:updateNode",
        nodeApiBaseUrl,
        expectedNetwork,
        autoLockMinutes,
      });
      onUpdated(nextState);
      setMessage(`Settings updated for ${selectedNetwork.label}.`);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Unable to update node endpoint.");
    }
  }

  async function handleLock(): Promise<void> {
    const nextState = await sendWalletMessage<AppState>({ type: "wallet:lock" });
    onUpdated(nextState);
  }

  async function handleRemoveWallet(): Promise<void> {
    if (!globalThis.confirm("Remove this wallet from the extension? You will need the private key to import it again.")) {
      return;
    }
    const nextState = await sendWalletMessage<AppState>({ type: "wallet:remove" });
    setMessage("Wallet removed.");
    onUpdated(nextState);
  }

  async function loadConnectedSites(): Promise<void> {
    try {
      setConnectedSites(await sendWalletMessage<ConnectedSite[]>({ type: "wallet:listConnectedSites" }));
    } catch {
      setConnectedSites([]);
    }
  }

  async function handleRevoke(origin: string): Promise<void> {
    const next = await sendWalletMessage<ConnectedSite[]>({ type: "wallet:revokeConnectedSite", origin });
    setConnectedSites(next);
    setMessage(`Disconnected ${origin}.`);
  }

  return (
    <section className="panel">
      <h2>Settings</h2>
      <div className="stack">
        <label className="stack">
          <span>Network</span>
          <select value={expectedNetwork} onChange={(event) => handleNetworkChange(event.target.value as SupportedNetworkId)}>
            {SUPPORTED_NETWORKS.map((network) => (
              <option key={network.id} value={network.id}>{network.label}</option>
            ))}
          </select>
        </label>
        <p className="message">{selectedNetwork.description}</p>
        <p className="message">Endpoint mode: {selectedNetwork.defaultEndpointLabel}</p>
        <label className="stack">
          <span>Node API endpoint</span>
          <input value={nodeApiBaseUrl} onChange={(event) => setNodeApiBaseUrl(event.target.value)} placeholder="Node API endpoint" />
        </label>
        {selectedNetwork.localNodeApiBaseUrl ? (
          <p className="message">Advanced/operator local node API: <span className="mono">{selectedNetwork.localNodeApiBaseUrl}</span></p>
        ) : null}
        <p className="message">{selectedNetwork.httpSafetyNote}</p>
        <label className="stack">
          <span>Auto-lock</span>
          <select value={autoLockMinutes} onChange={(event) => setAutoLockMinutes(Number(event.target.value))}>
            {AUTO_LOCK_MINUTES_OPTIONS.map((minutes) => (
              <option key={minutes} value={minutes}>{minutes} minutes</option>
            ))}
          </select>
        </label>
        <button className="primary-button" onClick={() => void handleSave()}>Save settings</button>
        <button className="secondary-button" onClick={onOpenBackup}>Open backup / export</button>
        <button onClick={() => void handleLock()}>Lock wallet</button>
        <button className="danger-button" onClick={() => void handleRemoveWallet()}>Remove wallet</button>
      </div>
      <h3>Connected sites</h3>
      {connectedSites.length === 0 ? (
        <p className="message">No connected sites.</p>
      ) : (
        <div className="stack">
          {connectedSites.map((site) => (
            <div className="inline-row" key={site.origin}>
              <p>
                <span className="mono">{site.origin}</span><br />
                <span className="message">Last used {new Date(site.lastUsedAt).toLocaleString()}</span>
              </p>
              <button className="danger-button" onClick={() => void handleRevoke(site.origin)}>Revoke</button>
            </div>
          ))}
        </div>
      )}
      {message ? <p className="message">{message}</p> : null}
    </section>
  );
}
