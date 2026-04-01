import { useState } from "react";

import type { AppState } from "../../state/app_state";
import { sendWalletMessage } from "../../shared/messages";

export function Settings(
  { state, onUpdated, onOpenBackup }: { state: AppState; onUpdated(state: AppState): void; onOpenBackup(): void },
): JSX.Element {
  const [nodeApiBaseUrl, setNodeApiBaseUrl] = useState(state.nodeApiBaseUrl);
  const [message, setMessage] = useState<string | null>(null);

  async function handleSave(): Promise<void> {
    try {
      const nextState = await sendWalletMessage<AppState>({ type: "wallet:updateNode", nodeApiBaseUrl });
      onUpdated(nextState);
      setMessage("Node endpoint updated.");
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

  return (
    <section className="panel">
      <h2>Settings</h2>
      <div className="stack">
        <input value={nodeApiBaseUrl} onChange={(event) => setNodeApiBaseUrl(event.target.value)} placeholder="Node API endpoint" />
        <button className="primary-button" onClick={() => void handleSave()}>Save endpoint</button>
        <button className="secondary-button" onClick={onOpenBackup}>Open backup / export</button>
        <button onClick={() => void handleLock()}>Lock wallet</button>
        <button className="danger-button" onClick={() => void handleRemoveWallet()}>Remove wallet</button>
      </div>
      {message ? <p className="message">{message}</p> : null}
    </section>
  );
}
