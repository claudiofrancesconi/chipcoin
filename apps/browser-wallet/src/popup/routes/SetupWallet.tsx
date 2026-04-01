import { useState } from "react";

import type { AppState } from "../../state/app_state";
import { DEFAULT_NODE_ENDPOINT } from "../../shared/constants";
import { sendWalletMessage } from "../../shared/messages";

type SetupMode = "create" | "import";

export function SetupWallet({ onCreated }: { onCreated(state: AppState): void }): JSX.Element {
  const [mode, setMode] = useState<SetupMode>("create");
  const [password, setPassword] = useState("");
  const [privateKeyHex, setPrivateKeyHex] = useState("");
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const canSubmit = password.trim().length > 0 && (mode === "create" || privateKeyHex.trim().length > 0);

  async function handleSubmit(): Promise<void> {
    if (!canSubmit || isSubmitting) {
      return;
    }
    setIsSubmitting(true);
    setError(null);
    try {
      const state = mode === "create"
        ? await sendWalletMessage<AppState>({ type: "wallet:create", password })
        : await sendWalletMessage<AppState>({ type: "wallet:import", password, privateKeyHex });
      onCreated(state);
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : "Unable to set up the wallet.");
    } finally {
      setIsSubmitting(false);
    }
  }

  return (
    <section className="panel">
      <h2>Set Up Wallet</h2>
      <p className="message">Create or import a Chipcoin devnet wallet directly from the popup. The default node is <span className="mono">{DEFAULT_NODE_ENDPOINT}</span>.</p>
      <div className="nav-tabs two-up">
        <button className={mode === "create" ? "is-active" : ""} onClick={() => { setMode("create"); setError(null); }}>
          Create
        </button>
        <button className={mode === "import" ? "is-active" : ""} onClick={() => { setMode("import"); setError(null); }}>
          Import
        </button>
      </div>
      <div className="stack">
        {mode === "create" ? (
          <p className="message">A new secp256k1 private key will be generated locally and encrypted in extension storage.</p>
        ) : (
          <textarea
            value={privateKeyHex}
            onChange={(event) => { setPrivateKeyHex(event.target.value); setError(null); }}
            placeholder="Private key hex"
          />
        )}
        <input
          type="password"
          value={password}
          onChange={(event) => { setPassword(event.target.value); setError(null); }}
          placeholder="Password"
        />
        <button className="primary-button" disabled={!canSubmit || isSubmitting} onClick={() => void handleSubmit()}>
          {isSubmitting ? "Setting up..." : mode === "create" ? "Create wallet" : "Import wallet"}
        </button>
      </div>
      {error ? <p className="message error">{error}</p> : null}
    </section>
  );
}
