import { useState } from "react";

import type { AppState } from "../../state/app_state";
import { sendWalletMessage } from "../../shared/messages";

export function SetPassword({
  mode,
  privateKeyHex,
  onCreated,
}: {
  mode: "create" | "import";
  privateKeyHex: string;
  onCreated(state: AppState): void;
}): JSX.Element {
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);

  async function handleSubmit(): Promise<void> {
    try {
      const state = mode === "create"
        ? await sendWalletMessage<AppState>({ type: "wallet:create", password })
        : await sendWalletMessage<AppState>({ type: "wallet:import", password, privateKeyHex });
      onCreated(state);
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : "Unable to finish wallet setup.");
    }
  }

  return (
    <section>
      <h2>Set password</h2>
      <input type="password" value={password} onChange={(event) => setPassword(event.target.value)} placeholder="Password" />
      <button onClick={() => void handleSubmit()}>Continue</button>
      {error ? <p>{error}</p> : null}
    </section>
  );
}
