import { useState } from "react";

import { copyText } from "../../shared/clipboard";
import { sendWalletMessage } from "../../shared/messages";

const EXPORT_CONFIRMATION_TEXT = "EXPORT";

export function Backup(): JSX.Element {
  const [hasAcknowledgedRisk, setHasAcknowledgedRisk] = useState(false);
  const [confirmationText, setConfirmationText] = useState("");
  const [privateKeyHex, setPrivateKeyHex] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);

  const canReveal = hasAcknowledgedRisk && confirmationText.trim().toUpperCase() === EXPORT_CONFIRMATION_TEXT;

  async function handleReveal(): Promise<void> {
    try {
      const response = await sendWalletMessage<{ privateKeyHex: string }>({
        type: "wallet:exportPrivateKey",
        confirmActiveSession: true,
      });
      setPrivateKeyHex(response.privateKeyHex);
      setMessage("Private key revealed. Store it securely.");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Unable to reveal the private key.");
    }
  }

  function handleHide(): void {
    setPrivateKeyHex(null);
    setMessage("Private key hidden.");
  }

  return (
    <section className="panel">
      <h2>Backup / Export</h2>
      <div className="warning-panel">
        <p><strong>This is your private key. Anyone with it can take your funds.</strong></p>
        <p>Only reveal it if you are backing up or recovering this wallet on a trusted machine.</p>
      </div>
      <div className="stack">
        <label className="checkbox-row">
          <input
            type="checkbox"
            checked={hasAcknowledgedRisk}
            onChange={(event) => setHasAcknowledgedRisk(event.target.checked)}
          />
          <span>I understand that exposing this key gives full control of the wallet.</span>
        </label>
        <label className="stack">
          <span>Type <span className="mono">{EXPORT_CONFIRMATION_TEXT}</span> to enable export.</span>
          <input
            value={confirmationText}
            onChange={(event) => setConfirmationText(event.target.value)}
            placeholder={EXPORT_CONFIRMATION_TEXT}
            autoCapitalize="characters"
            autoCorrect="off"
            spellCheck={false}
          />
        </label>
        {!privateKeyHex ? (
          <button className="danger-button" disabled={!canReveal} onClick={() => void handleReveal()}>
            Reveal private key
          </button>
        ) : (
          <>
            <textarea className="secret-box" readOnly value={privateKeyHex} />
            <div className="button-row">
              <button className="secondary-button" onClick={() => void copyText(privateKeyHex).then(() => setMessage("Private key copied."))}>
                Copy private key
              </button>
              <button onClick={handleHide}>Hide private key</button>
            </div>
          </>
        )}
      </div>
      {message ? <p className="message">{message}</p> : null}
    </section>
  );
}
