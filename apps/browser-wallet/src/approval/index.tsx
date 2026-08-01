import React, { useEffect, useState } from "react";
import { createRoot } from "react-dom/client";

import type { ProviderApprovalSummary } from "../state/actions";
import type { AppState } from "../state/app_state";
import { sendWalletMessage } from "../shared/messages";
import "../popup/styles.css";

function ApprovalApp(): JSX.Element {
  const [approval, setApproval] = useState<ProviderApprovalSummary | null>(null);
  const [walletState, setWalletState] = useState<AppState | null>(null);
  const [password, setPassword] = useState("");
  const [isUnlocking, setIsUnlocking] = useState(false);
  const [message, setMessage] = useState<string>("Loading approval request...");
  const approvalId = new URLSearchParams(window.location.search).get("id") ?? "";

  useEffect(() => {
    async function load(): Promise<void> {
      try {
        const next = await sendWalletMessage<ProviderApprovalSummary>({
          type: "provider:approval:get",
          approvalId,
        });
        const state = await sendWalletMessage<AppState>({ type: "wallet:getState" });
        setApproval(next);
        setWalletState(state);
        setMessage("");
      } catch (error) {
        setMessage(error instanceof Error ? error.message : "Unable to load approval request.");
      }
    }
    void load();
  }, [approvalId]);

  const requiresUnlock = approval?.kind === "signMessage" && walletState?.isLocked === true;

  async function unlockWallet(): Promise<void> {
    setIsUnlocking(true);
    setMessage("");
    try {
      const state = await sendWalletMessage<AppState>({ type: "wallet:unlock", password });
      setWalletState(state);
      setPassword("");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Unable to unlock wallet.");
    } finally {
      setIsUnlocking(false);
    }
  }

  async function respond(approved: boolean): Promise<void> {
    if (approved && requiresUnlock) {
      setMessage("Unlock the wallet before approving this signing request.");
      return;
    }
    await sendWalletMessage<{ approved: boolean }>({
      type: "provider:approval:respond",
      approvalId,
      approved,
    });
    window.close();
  }

  return (
    <main className="app-shell">
      <h1>Chipcoin Wallet</h1>
      <section className="panel stack">
        <h2>{approval?.kind === "signMessage" ? "Sign login message" : "Connect site"}</h2>
        {message ? <p className="message">{message}</p> : null}
        {approval ? (
          <>
            <dl className="details-list">
              <dt>Requesting origin</dt>
              <dd className="mono">{approval.origin}</dd>
              <dt>Domain</dt>
              <dd className="mono">{approval.domain}</dd>
              <dt>Selected address</dt>
              <dd className="mono">{approval.address}</dd>
              {approval.kind === "signMessage" ? (
                <>
                  <dt>Network</dt>
                  <dd>{approval.network}</dd>
                  <dt>Scheme</dt>
                  <dd>{approval.scheme}</dd>
                  <dt>Issued at</dt>
                  <dd>{approval.issued_at}</dd>
                  <dt>Expires at</dt>
                  <dd>{approval.expires_at}</dd>
                  <dt>Statement</dt>
                  <dd>{approval.statement}</dd>
                </>
              ) : null}
            </dl>
            {approval.kind === "signMessage" ? (
              <p className="warning-panel">
                This signs a login message. It is not a transaction and does not move funds.
              </p>
            ) : (
              <p className="message">This allows the site to read your selected Chipcoin address.</p>
            )}
            {requiresUnlock ? (
              <form
                className="stack"
                onSubmit={(event) => {
                  event.preventDefault();
                  void unlockWallet();
                }}
              >
                <label className="stack">
                  <span>Unlock wallet</span>
                  <input
                    autoFocus
                    type="password"
                    value={password}
                    placeholder="Password"
                    onChange={(event) => setPassword(event.target.value)}
                  />
                </label>
                <button className="primary-button" disabled={isUnlocking} type="submit">
                  {isUnlocking ? "Unlocking..." : "Unlock"}
                </button>
              </form>
            ) : null}
            <div className="button-row">
              <button className="primary-button" disabled={requiresUnlock} onClick={() => void respond(true)}>Approve</button>
              <button className="danger-button" onClick={() => void respond(false)}>Reject</button>
            </div>
          </>
        ) : null}
      </section>
    </main>
  );
}

createRoot(document.getElementById("root") as HTMLElement).render(
  <React.StrictMode>
    <ApprovalApp />
  </React.StrictMode>,
);
