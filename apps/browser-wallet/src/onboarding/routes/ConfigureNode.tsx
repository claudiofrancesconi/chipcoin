import { useState } from "react";

import type { AppState } from "../../state/app_state";
import { DEFAULT_NODE_ENDPOINT } from "../../shared/constants";
import { sendWalletMessage } from "../../shared/messages";

export function ConfigureNode(): JSX.Element {
  const [nodeApiBaseUrl, setNodeApiBaseUrl] = useState(DEFAULT_NODE_ENDPOINT);
  const [message, setMessage] = useState<string | null>(null);

  async function handleSave(): Promise<void> {
    try {
      const state = await sendWalletMessage<AppState>({ type: "wallet:updateNode", nodeApiBaseUrl });
      setMessage(`Node configured for ${state.expectedNetwork}. You can now open the wallet popup.`);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Unable to configure node endpoint.");
    }
  }

  return (
    <section>
      <h2>Configure node</h2>
      <input value={nodeApiBaseUrl} onChange={(event) => setNodeApiBaseUrl(event.target.value)} placeholder="Node API endpoint" />
      <button onClick={() => void handleSave()}>Save node endpoint</button>
      {message ? <p>{message}</p> : null}
    </section>
  );
}
