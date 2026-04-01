import type { AppState } from "../../state/app_state";
import { copyText } from "../../shared/clipboard";
import { formatChc } from "../../shared/formatting";

export function Overview(
  { state, isLoading, onRefresh }: { state: AppState; isLoading: boolean; onRefresh(): Promise<void> },
): JSX.Element {
  const summary = state.overview.summary;
  const hasAddress = typeof state.address === "string" && state.address.length > 0;
  const connectedNetwork = isLoading && state.nodeStatus === null
    ? "Loading…"
    : (state.nodeStatus?.network ?? "Unavailable");
  const networkClassName = connectedNetwork === state.expectedNetwork
    ? "pill success"
    : connectedNetwork === "Loading…"
      ? "pill"
      : "pill warning";
  return (
    <section className="panel">
      <h2>Overview</h2>
      <div className="stack">
        <div className="inline-row">
          <p><strong>Address:</strong> <span className="mono">{state.address}</span></p>
          <button className="secondary-button" disabled={!hasAddress} onClick={() => hasAddress ? void copyText(state.address) : undefined}>
            Copy address
          </button>
        </div>
        <p><strong>Node API:</strong> <span className="mono">{state.nodeApiBaseUrl}</span></p>
        <p><strong>Expected network:</strong> <span className="pill">{state.expectedNetwork}</span></p>
        <p><strong>Connected network:</strong> <span className={networkClassName}>{connectedNetwork}</span></p>
        <p><strong>Height:</strong> {isLoading && state.nodeStatus === null ? "Loading…" : (state.nodeStatus?.height ?? "Unknown")}</p>
      </div>
      <button className="primary-button" onClick={() => void onRefresh()} disabled={isLoading}>
        {isLoading ? "Refreshing…" : "Refresh"}
      </button>
      <h3>Balances</h3>
      {summary ? (
        <div className="metric-grid">
          <div className="metric-card highlight">
            <span className="metric-label">Spendable</span>
            <strong>{formatChc(summary.spendable_balance_chipbits)}</strong>
          </div>
          <div className="metric-card">
            <span className="metric-label">Immature</span>
            <strong>{formatChc(summary.immature_balance_chipbits)}</strong>
          </div>
          <div className="metric-card">
            <span className="metric-label">Confirmed</span>
            <strong>{formatChc(summary.confirmed_balance_chipbits)}</strong>
          </div>
        </div>
      ) : isLoading ? (
        <p className="message">Loading balance data…</p>
      ) : (
        <p className="message">Balance data is unavailable.</p>
      )}
    </section>
  );
}
