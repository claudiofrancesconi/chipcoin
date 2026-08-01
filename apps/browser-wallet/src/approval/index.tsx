import React, { useEffect, useState } from "react";
import { createRoot } from "react-dom/client";

import type { ProviderApprovalSummary } from "../state/actions";
import { sendWalletMessage } from "../shared/messages";
import "../popup/styles.css";

function ApprovalApp(): JSX.Element {
  const [approval, setApproval] = useState<ProviderApprovalSummary | null>(null);
  const [message, setMessage] = useState<string>("Loading approval request...");
  const approvalId = new URLSearchParams(window.location.search).get("id") ?? "";

  useEffect(() => {
    async function load(): Promise<void> {
      try {
        const next = await sendWalletMessage<ProviderApprovalSummary>({
          type: "provider:approval:get",
          approvalId,
        });
        setApproval(next);
        setMessage("");
      } catch (error) {
        setMessage(error instanceof Error ? error.message : "Unable to load approval request.");
      }
    }
    void load();
  }, [approvalId]);

  async function respond(approved: boolean): Promise<void> {
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
            <div className="button-row">
              <button className="primary-button" onClick={() => void respond(true)}>Approve</button>
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
