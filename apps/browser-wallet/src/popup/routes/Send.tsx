import { useState } from "react";

import type { AppState } from "../../state/app_state";
import { parseChcToChipbits } from "../../shared/formatting";
import { sendWalletMessage } from "../../shared/messages";

export function Send({ state, onRefresh }: { state: AppState; onRefresh(): Promise<void> }): JSX.Element {
  const [recipient, setRecipient] = useState("");
  const [amountChc, setAmountChc] = useState("");
  const [feeChc, setFeeChc] = useState("0.00001");
  const [result, setResult] = useState<string | null>(null);
  const [isSubmitting, setIsSubmitting] = useState(false);

  let parsedAmountChipbits = 0;
  let parsedFeeChipbits = 0;
  let formError: string | null = null;

  if (!recipient.trim()) {
    formError = "Recipient address is required.";
  } else {
    try {
      parsedAmountChipbits = parseChcToChipbits(amountChc);
    } catch (error) {
      formError = error instanceof Error ? error.message : "Amount must be a valid CHC value.";
    }
  }

  if (!formError) {
    try {
      parsedFeeChipbits = parseChcToChipbits(feeChc);
    } catch (error) {
      formError = error instanceof Error ? error.message.replace("Amount", "Fee") : "Fee must be a valid CHC value.";
    }
  }

  async function handleSubmit(): Promise<void> {
    if (formError || isSubmitting) {
      return;
    }
    setIsSubmitting(true);
    try {
      const response = await sendWalletMessage<{ status: string; txid?: string }>({
        type: "wallet:submit",
        recipient: recipient.trim(),
        amountChipbits: parsedAmountChipbits,
        feeChipbits: parsedFeeChipbits,
      });
      const label = ({
        submitted: "Submitted",
        rejected: "Rejected",
        failed_to_submit: "Failed to submit",
      } as const)[response.status as "submitted" | "rejected" | "failed_to_submit"] ?? response.status;
      setResult(response.txid ? `${label}: ${response.txid}` : label);
      await onRefresh();
      if (response.status === "submitted") {
        setRecipient("");
        setAmountChc("");
      }
    } catch (error) {
      setResult(error instanceof Error ? error.message : "Unable to submit transaction.");
    } finally {
      setIsSubmitting(false);
    }
  }

  return (
    <section className="panel">
      <h2>Send</h2>
      <p className="message">Spending stays client-side. The wallet builds, signs, and serializes transactions locally before submitting raw hex to the node.</p>
      <p><strong>From wallet:</strong> <span className="mono">{state.address}</span></p>
      <div className="stack">
        <label className="stack">
          <span>Recipient address</span>
          <input value={recipient} onChange={(event) => { setRecipient(event.target.value); setResult(null); }} placeholder="CHC recipient address" />
        </label>
        <label className="stack">
          <span>Amount (CHC)</span>
          <input value={amountChc} onChange={(event) => { setAmountChc(event.target.value); setResult(null); }} placeholder="e.g. 50 or 0.25" />
        </label>
        <label className="stack">
          <span>Fee (CHC)</span>
          <input value={feeChc} onChange={(event) => { setFeeChc(event.target.value); setResult(null); }} placeholder="e.g. 0.00001" />
        </label>
        <button className="primary-button" disabled={Boolean(formError) || isSubmitting} onClick={() => void handleSubmit()}>
          {isSubmitting ? "Submitting..." : "Submit transaction"}
        </button>
      </div>
      {formError ? <p className="message error">{formError}</p> : null}
      {result ? <p className="message">{result}</p> : null}
    </section>
  );
}
