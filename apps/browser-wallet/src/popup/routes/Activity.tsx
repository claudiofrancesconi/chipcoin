import { useEffect, useState } from "react";

import type { HistoryEntry } from "../../api/types";
import type { AppState } from "../../state/app_state";
import { formatChc, shortHash } from "../../shared/formatting";
import { sendWalletMessage } from "../../shared/messages";
import { unixToIso } from "../../shared/time";

export function Activity({ state }: { state: AppState }): JSX.Element {
  const [history, setHistory] = useState<HistoryEntry[]>(state.overview.history);
  const [isLoadingHistory, setIsLoadingHistory] = useState(state.overview.history.length === 0);
  const [historyError, setHistoryError] = useState<string | null>(null);

  useEffect(() => {
    setHistory(state.overview.history);
    setIsLoadingHistory(state.overview.history.length === 0);
    setHistoryError(null);
  }, [state.overview.history]);

  useEffect(() => {
    let cancelled = false;

    async function loadHistory(): Promise<void> {
      setIsLoadingHistory(true);
      try {
        const next = await sendWalletMessage<HistoryEntry[]>({ type: "wallet:getHistory" });
        if (!cancelled) {
          setHistory(next);
          setHistoryError(null);
        }
      } catch (error) {
        if (!cancelled) {
          setHistoryError(error instanceof Error ? error.message : "Unable to load confirmed history.");
        }
      } finally {
        if (!cancelled) {
          setIsLoadingHistory(false);
        }
      }
    }

    void loadHistory();

    return () => {
      cancelled = true;
    };
  }, [state.address, state.nodeApiBaseUrl]);

  function transactionUrl(txid: string): string {
    return `${state.nodeApiBaseUrl}/v1/tx/${txid}`;
  }

  return (
    <section className="panel">
      <h2>Activity</h2>
      <h3>Confirmed history</h3>
      {isLoadingHistory ? <p className="message">Loading confirmed history…</p> : historyError ? (
        <p className="message error">{historyError}</p>
      ) : history.length === 0 ? <p className="message">No confirmed history.</p> : (
        <ul className="activity-list">
          {history.map((entry) => (
            <li key={entry.txid}>
              <a className="tx-link" href={transactionUrl(entry.txid)} target="_blank" rel="noreferrer">
                <strong>{shortHash(entry.txid)}</strong>
              </a>{" "}
              {formatChc(entry.net_chipbits)} at {unixToIso(entry.timestamp)}
            </li>
          ))}
        </ul>
      )}
      <h3>Submitted transactions</h3>
      {state.overview.submittedTransactions.length === 0 ? <p className="message">No submitted transactions.</p> : (
        <ul className="activity-list">
          {state.overview.submittedTransactions.map((entry) => (
            <li key={entry.txid}>
              <a className="tx-link" href={transactionUrl(entry.txid)} target="_blank" rel="noreferrer">
                <strong>{shortHash(entry.txid)}</strong>
              </a>{" "}
              {entry.status} · {formatChc(entry.amountChipbits)} to {entry.recipient}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
