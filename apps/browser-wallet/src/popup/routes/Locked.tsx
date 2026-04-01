import { useState } from "react";

import type { AppState } from "../../state/app_state";
import { sendWalletMessage } from "../../shared/messages";

export function Locked({ onUnlocked }: { onUnlocked(state: AppState): void }): JSX.Element {
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);

  async function handleUnlock(): Promise<void> {
    try {
      const state = await sendWalletMessage<AppState>({ type: "wallet:unlock", password });
      onUnlocked(state);
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : "Unable to unlock wallet.");
    }
  }

  return (
    <section className="panel">
      <h2>Unlock wallet</h2>
      <form
        className="stack"
        onSubmit={(event) => {
          event.preventDefault();
          void handleUnlock();
        }}
      >
        <input
          type="password"
          value={password}
          placeholder="Password"
          onChange={(event) => setPassword(event.target.value)}
        />
        <button className="primary-button" type="submit">Unlock</button>
      </form>
      {error ? <p className="message error">{error}</p> : null}
    </section>
  );
}
