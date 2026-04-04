import { useEffect, useMemo, useState } from "react";

import {
  ExplorerApiClient,
  ExplorerApiError,
  readRuntimeExplorerApiBase,
  resolveExplorerApiBaseUrl,
} from "./api";
import type {
  AddressHistoryEntry,
  AddressSummary,
  AddressUtxo,
  BlockDetail,
  BlockSummary,
  MempoolEntry,
  NodeStatus,
  PeerDetail,
  PeerSummary,
  TxView,
} from "./types";

type Route =
  | { name: "dashboard" }
  | { name: "blocks" }
  | { name: "block"; hash?: string; height?: number }
  | { name: "tx"; txid: string }
  | { name: "address"; address: string }
  | { name: "mempool" }
  | { name: "peers" };

const NODE_URL_KEY = "chipcoin.explorer.nodeUrl";

type NodeConnectionState =
  | { state: "connected"; message: string }
  | { state: "unreachable"; message: string };

function formatUnix(value: number | null | undefined): string {
  if (typeof value !== "number") {
    return "Unknown";
  }
  return new Date(value * 1000).toLocaleString();
}

function shortHash(value: string, visible = 12): string {
  return value.length <= visible * 2 ? value : `${value.slice(0, visible)}…${value.slice(-visible)}`;
}

function formatJson(value: unknown): string {
  return JSON.stringify(value, null, 2);
}

function readRoute(): Route {
  const raw = window.location.hash.replace(/^#/, "") || "/dashboard";
  const [path, queryString] = raw.split("?", 2);
  const query = new URLSearchParams(queryString ?? "");
  if (path === "/blocks") {
    return { name: "blocks" };
  }
  if (path === "/block") {
    const hash = query.get("hash") ?? undefined;
    const height = query.get("height");
    return { name: "block", hash, height: height === null ? undefined : Number(height) };
  }
  if (path.startsWith("/tx/")) {
    return { name: "tx", txid: decodeURIComponent(path.slice("/tx/".length)) };
  }
  if (path.startsWith("/address/")) {
    return { name: "address", address: decodeURIComponent(path.slice("/address/".length)) };
  }
  if (path === "/mempool") {
    return { name: "mempool" };
  }
  if (path === "/peers") {
    return { name: "peers" };
  }
  return { name: "dashboard" };
}

function navigate(route: Route): void {
  if (route.name === "dashboard") {
    window.location.hash = "/dashboard";
    return;
  }
  if (route.name === "blocks") {
    window.location.hash = "/blocks";
    return;
  }
  if (route.name === "block") {
    const query = new URLSearchParams();
    if (route.hash) {
      query.set("hash", route.hash);
    }
    if (typeof route.height === "number") {
      query.set("height", String(route.height));
    }
    window.location.hash = `/block?${query.toString()}`;
    return;
  }
  if (route.name === "tx") {
    window.location.hash = `/tx/${encodeURIComponent(route.txid)}`;
    return;
  }
  if (route.name === "address") {
    window.location.hash = `/address/${encodeURIComponent(route.address)}`;
    return;
  }
  window.location.hash = `/${route.name}`;
}

function useRoute(): Route {
  const [route, setRoute] = useState<Route>(() => readRoute());
  useEffect(() => {
    const handler = () => setRoute(readRoute());
    window.addEventListener("hashchange", handler);
    return () => window.removeEventListener("hashchange", handler);
  }, []);
  return route;
}

function useNodeUrl(): [string, (value: string) => void] {
  const [nodeUrl, setNodeUrl] = useState(() => resolveExplorerApiBaseUrl({
    search: window.location.search,
    storedValue: localStorage.getItem(NODE_URL_KEY),
    envValue: import.meta.env.VITE_NODE_API_BASE_URL,
    runtimeValue: readRuntimeExplorerApiBase(window),
    protocol: window.location.protocol || "http:",
    hostname: window.location.hostname || "127.0.0.1",
  }).value);
  useEffect(() => {
    localStorage.setItem(NODE_URL_KEY, nodeUrl);
  }, [nodeUrl]);
  return [nodeUrl, setNodeUrl];
}

function SearchBar(): JSX.Element {
  const [blockInput, setBlockInput] = useState("");
  const [txInput, setTxInput] = useState("");
  const [addressInput, setAddressInput] = useState("");

  function openBlock(): void {
    if (!blockInput.trim()) {
      return;
    }
    const trimmed = blockInput.trim();
    const numeric = Number(trimmed);
    if (Number.isInteger(numeric) && numeric >= 0) {
      navigate({ name: "block", height: numeric });
      return;
    }
    navigate({ name: "block", hash: trimmed });
  }

  return (
    <section className="panel">
      <h2>Lookup</h2>
      <div className="search-grid">
        <div className="field-row">
          <input value={blockInput} onChange={(event) => setBlockInput(event.target.value)} placeholder="Block hash or height" />
          <button onClick={openBlock}>Open block</button>
        </div>
        <div className="field-row">
          <input value={txInput} onChange={(event) => setTxInput(event.target.value)} placeholder="Transaction id" />
          <button onClick={() => txInput.trim() ? navigate({ name: "tx", txid: txInput.trim() }) : undefined}>Open tx</button>
        </div>
        <div className="field-row">
          <input value={addressInput} onChange={(event) => setAddressInput(event.target.value)} placeholder="Address" />
          <button onClick={() => addressInput.trim() ? navigate({ name: "address", address: addressInput.trim() }) : undefined}>Open address</button>
        </div>
      </div>
    </section>
  );
}

function QueryPanel<T>({
  title,
  loader,
  deps,
  children,
}: {
  title: string;
  loader(): Promise<T>;
  deps: unknown[];
  children(value: T): JSX.Element;
}): JSX.Element {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    void loader()
      .then((value) => {
        if (!cancelled) {
          setData(value);
        }
      })
      .catch((nextError) => {
        if (!cancelled) {
          setError(nextError instanceof Error ? nextError.message : "Request failed.");
          setData(null);
        }
      })
      .finally(() => {
        if (!cancelled) {
          setLoading(false);
        }
      });
    return () => {
      cancelled = true;
    };
  }, deps);

  return (
    <section className="panel">
      <h2>{title}</h2>
      {loading ? <p className="message">Loading…</p> : error ? <p className="message error">{error}</p> : data === null ? (
        <p className="message">No data.</p>
      ) : children(data)}
    </section>
  );
}

function DashboardPage({ client }: { client: ExplorerApiClient }): JSX.Element {
  const [status, setStatus] = useState<NodeStatus | null>(null);
  const [blocks, setBlocks] = useState<BlockSummary[]>([]);
  const [statusError, setStatusError] = useState<string | null>(null);
  const [blocksError, setBlocksError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setStatusError(null);
    setBlocksError(null);
    void Promise.allSettled([client.status(), client.blocks({ limit: 10 })])
      .then(([statusResult, blocksResult]) => {
        if (cancelled) {
          return;
        }
        if (statusResult.status === "fulfilled") {
          setStatus(statusResult.value);
        } else {
          setStatus(null);
          setStatusError(statusResult.reason instanceof Error ? statusResult.reason.message : "Unable to load node status.");
        }
        if (blocksResult.status === "fulfilled") {
          setBlocks(blocksResult.value);
        } else {
          setBlocks([]);
          setBlocksError(blocksResult.reason instanceof Error ? blocksResult.reason.message : "Unable to load recent blocks.");
        }
      })
      .finally(() => {
        if (!cancelled) {
          setLoading(false);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [client]);

  return (
    <section className="panel">
      <h2>Network Status</h2>
      {loading ? <p className="message">Loading…</p> : statusError ? <p className="message error">{statusError}</p> : status ? (
        <>
          <div className="kv-grid">
            <div><span>Network</span><strong>{status.network}</strong></div>
            <div><span>Height</span><strong>{status.height ?? "Unknown"}</strong></div>
            <div><span>Tip hash</span><strong className="mono kv-value-break">{status.tip_hash ?? "Unknown"}</strong></div>
            <div><span>Mempool size</span><strong>{status.mempool_size}</strong></div>
            <div><span>Peers</span><strong>{status.peer_count}</strong></div>
            <div><span>Handshaken peers</span><strong>{status.handshaken_peer_count}</strong></div>
          </div>
          <h3>Next Block Reward Winners</h3>
          {status.next_block_reward_winners.length === 0 ? <p className="message">No node reward winners for the next block.</p> : (
            <table>
              <thead>
                <tr><th>Node</th><th>Payout address</th><th>Reward</th></tr>
              </thead>
              <tbody>
                {status.next_block_reward_winners.map((winner) => (
                  <tr key={`${winner.node_id}-${winner.payout_address}`}>
                    <td className="mono">{winner.node_id}</td>
                    <td><button className="linkish" onClick={() => navigate({ name: "address", address: winner.payout_address })}>{winner.payout_address}</button></td>
                    <td>{winner.reward_chipbits}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          <h3>Latest Blocks</h3>
          {blocksError ? <p className="message error">{blocksError}</p> : blocks.length === 0 ? <p className="message">No blocks returned by the node API.</p> : (
            <table>
              <thead>
                <tr><th>Height</th><th>Hash</th><th>Txs</th><th>Weight</th><th>Time</th></tr>
              </thead>
              <tbody>
                {blocks.map((block) => (
                  <tr key={block.block_hash}>
                    <td><button className="linkish" onClick={() => navigate({ name: "block", height: block.height })}>{block.height}</button></td>
                    <td><button className="linkish mono" onClick={() => navigate({ name: "block", hash: block.block_hash })}>{shortHash(block.block_hash)}</button></td>
                    <td>{block.transaction_count}</td>
                    <td>{block.weight_units}</td>
                    <td>{formatUnix(block.timestamp)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </>
      ) : <p className="message">No status data.</p>}
    </section>
  );
}

function BlocksPage({ client }: { client: ExplorerApiClient }): JSX.Element {
  const [fromHeight, setFromHeight] = useState<number | undefined>(undefined);
  const [pageSize] = useState(20);
  return (
    <QueryPanel title="Blocks" loader={() => client.blocks({ fromHeight, limit: pageSize })} deps={[client, fromHeight, pageSize]}>
      {(blocks: BlockSummary[]) => {
        const oldest = blocks.length === 0 ? undefined : blocks[blocks.length - 1].height;
        const newest = blocks.length === 0 ? undefined : blocks[0].height;
        return (
          <>
            <div className="field-row">
              <button onClick={() => setFromHeight(undefined)}>Latest</button>
              <button disabled={typeof newest !== "number"} onClick={() => setFromHeight(typeof newest === "number" ? newest + pageSize : undefined)}>Newer</button>
              <button disabled={typeof oldest !== "number" || oldest <= 0} onClick={() => setFromHeight(typeof oldest === "number" ? Math.max(0, oldest - 1) : undefined)}>Older</button>
            </div>
            <table>
              <thead>
                <tr><th>Height</th><th>Hash</th><th>Txs</th><th>Weight</th><th>Time</th></tr>
              </thead>
              <tbody>
                {blocks.map((block) => (
                  <tr key={block.block_hash}>
                    <td><button className="linkish" onClick={() => navigate({ name: "block", height: block.height })}>{block.height}</button></td>
                    <td className="mono">{shortHash(block.block_hash)}</td>
                    <td>{block.transaction_count}</td>
                    <td>{block.weight_units}</td>
                    <td>{formatUnix(block.timestamp)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </>
        );
      }}
    </QueryPanel>
  );
}

function BlockPage({ client, route }: { client: ExplorerApiClient; route: Extract<Route, { name: "block" }> }): JSX.Element {
  const descriptor = route.hash ?? (typeof route.height === "number" ? `height ${route.height}` : "block");
  return (
    <QueryPanel
      title={`Block: ${descriptor}`}
      loader={() => route.hash ? client.blockByHash(route.hash) : client.blockByHeight(route.height ?? 0)}
      deps={[client, route.hash, route.height]}
    >
      {(block: BlockDetail) => (
        <>
          <div className="kv-grid">
            <div><span>Block hash</span><strong className="mono kv-value-break">{block.block_hash}</strong></div>
            <div><span>Height</span><strong>{block.height ?? "Unknown"}</strong></div>
            <div><span>Miner payout</span><strong>{block.miner_payout_chipbits}</strong></div>
            <div><span>Fees</span><strong>{block.fees_chipbits ?? "Unknown"}</strong></div>
            <div><span>Transactions</span><strong>{block.transaction_count}</strong></div>
            <div><span>Weight</span><strong>{block.weight_units}</strong></div>
          </div>
          <h3>Header</h3>
          <pre>{formatJson(block.header)}</pre>
          <h3>Node reward payouts</h3>
          {block.node_reward_payouts.length === 0 ? <p className="message">No node rewards in this block.</p> : (
            <table>
              <thead>
                <tr><th>Recipient</th><th>Amount</th></tr>
              </thead>
              <tbody>
                {block.node_reward_payouts.map((row) => (
                  <tr key={`${row.recipient}-${row.amount_chipbits}`}>
                    <td><button className="linkish" onClick={() => navigate({ name: "address", address: row.recipient })}>{row.recipient}</button></td>
                    <td>{row.amount_chipbits}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          <h3>Transactions</h3>
          <table>
            <thead>
              <tr><th>Txid</th><th>Weight</th></tr>
            </thead>
            <tbody>
              {block.transactions.map((transaction) => (
                <tr key={transaction.txid}>
                  <td><button className="linkish mono" onClick={() => navigate({ name: "tx", txid: transaction.txid })}>{shortHash(transaction.txid)}</button></td>
                  <td>{transaction.weight_units}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
    </QueryPanel>
  );
}

function TxPage({ client, txid }: { client: ExplorerApiClient; txid: string }): JSX.Element {
  return (
    <QueryPanel title={`Transaction: ${txid}`} loader={() => client.tx(txid)} deps={[client, txid]}>
      {(tx: TxView) => (
        <>
          <div className="kv-grid">
            <div><span>Location</span><strong>{tx.location}</strong></div>
            <div><span>Height</span><strong>{tx.height ?? "Unconfirmed"}</strong></div>
            <div><span>Block hash</span><strong className="mono kv-value-break">{tx.block_hash ?? "Unconfirmed"}</strong></div>
            <div><span>Locktime</span><strong>{tx.transaction.locktime}</strong></div>
          </div>
          <h3>Inputs</h3>
          <pre>{formatJson(tx.transaction.inputs)}</pre>
          <h3>Outputs</h3>
          <table>
            <thead>
              <tr><th>Recipient</th><th>Value</th></tr>
            </thead>
            <tbody>
              {tx.transaction.outputs.map((output, index) => (
                <tr key={`${output.recipient}-${index}`}>
                  <td><button className="linkish" onClick={() => navigate({ name: "address", address: output.recipient })}>{output.recipient}</button></td>
                  <td>{output.value}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {Object.keys(tx.transaction.metadata).length > 0 ? (
            <>
              <h3>Metadata</h3>
              <pre>{formatJson(tx.transaction.metadata)}</pre>
            </>
          ) : null}
        </>
      )}
    </QueryPanel>
  );
}

function AddressPage({ client, address }: { client: ExplorerApiClient; address: string }): JSX.Element {
  const [summary, setSummary] = useState<AddressSummary | null>(null);
  const [utxos, setUtxos] = useState<AddressUtxo[]>([]);
  const [history, setHistory] = useState<AddressHistoryEntry[]>([]);
  const [summaryError, setSummaryError] = useState<string | null>(null);
  const [utxosError, setUtxosError] = useState<string | null>(null);
  const [historyError, setHistoryError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setSummaryError(null);
    setUtxosError(null);
    setHistoryError(null);
    void Promise.allSettled([client.address(address), client.utxos(address), client.history(address)])
      .then(([summaryResult, utxosResult, historyResult]) => {
        if (cancelled) {
          return;
        }
        if (summaryResult.status === "fulfilled") {
          setSummary(summaryResult.value);
        } else {
          setSummary(null);
          setSummaryError(summaryResult.reason instanceof Error ? summaryResult.reason.message : "Unable to load address summary.");
        }
        if (utxosResult.status === "fulfilled") {
          setUtxos(utxosResult.value);
        } else {
          setUtxos([]);
          setUtxosError(utxosResult.reason instanceof Error ? utxosResult.reason.message : "Unable to load UTXOs.");
        }
        if (historyResult.status === "fulfilled") {
          setHistory(historyResult.value);
        } else {
          setHistory([]);
          setHistoryError(historyResult.reason instanceof Error ? historyResult.reason.message : "Unable to load address history.");
        }
      })
      .finally(() => {
        if (!cancelled) {
          setLoading(false);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [client, address]);

  return (
    <section className="panel">
      <h2>Address: {address}</h2>
      {loading ? <p className="message">Loading…</p> : summaryError ? <p className="message error">{summaryError}</p> : summary ? (
        <>
          <div className="kv-grid">
            <div><span>Confirmed balance</span><strong>{summary.confirmed_balance_chipbits}</strong></div>
            <div><span>Spendable balance</span><strong>{summary.spendable_balance_chipbits}</strong></div>
            <div><span>Immature balance</span><strong>{summary.immature_balance_chipbits}</strong></div>
            <div><span>UTXO count</span><strong>{summary.utxo_count}</strong></div>
          </div>
          <h3>UTXOs</h3>
          {utxosError ? <p className="message error">{utxosError}</p> : <table>
            <thead>
              <tr><th>Txid</th><th>Vout</th><th>Amount</th><th>Coinbase</th><th>Mature</th><th>Origin height</th></tr>
            </thead>
            <tbody>
              {utxos.map((utxo) => (
                <tr key={`${utxo.txid}:${utxo.vout}`}>
                  <td><button className="linkish mono" onClick={() => navigate({ name: "tx", txid: utxo.txid })}>{shortHash(utxo.txid)}</button></td>
                  <td>{utxo.vout}</td>
                  <td>{utxo.amount_chipbits}</td>
                  <td>{String(utxo.coinbase)}</td>
                  <td>{String(utxo.mature)}</td>
                  <td>{utxo.origin_height}</td>
                </tr>
                ))}
              </tbody>
          </table>}
          <h3>Confirmed history</h3>
          {historyError ? <p className="message error">{historyError}</p> : <table>
            <thead>
              <tr><th>Height</th><th>Txid</th><th>Incoming</th><th>Outgoing</th><th>Net</th><th>Timestamp</th></tr>
            </thead>
            <tbody>
              {history.map((entry) => (
                <tr key={`${entry.block_height}-${entry.txid}`}>
                  <td>{entry.block_height}</td>
                  <td><button className="linkish mono" onClick={() => navigate({ name: "tx", txid: entry.txid })}>{shortHash(entry.txid)}</button></td>
                  <td>{entry.incoming_chipbits}</td>
                  <td>{entry.outgoing_chipbits}</td>
                  <td>{entry.net_chipbits}</td>
                  <td>{formatUnix(entry.timestamp)}</td>
                </tr>
                ))}
              </tbody>
          </table>}
        </>
      ) : null}
    </section>
  );
}

function MempoolPage({ client }: { client: ExplorerApiClient }): JSX.Element {
  return (
    <QueryPanel title="Mempool" loader={() => client.mempool()} deps={[client]}>
      {(entries: MempoolEntry[]) => entries.length === 0 ? <p className="message">Mempool is empty.</p> : (
        <table>
          <thead>
            <tr><th>Txid</th><th>Fee</th><th>Fee rate</th><th>Weight</th><th>Dependencies</th></tr>
          </thead>
          <tbody>
            {entries.map((entry) => (
              <tr key={entry.txid}>
                <td><button className="linkish mono" onClick={() => navigate({ name: "tx", txid: entry.txid })}>{shortHash(entry.txid)}</button></td>
                <td>{entry.fee_chipbits}</td>
                <td>{entry.fee_rate}</td>
                <td>{entry.weight_units}</td>
                <td className="mono">{entry.depends_on.length === 0 ? "-" : entry.depends_on.join(", ")}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </QueryPanel>
  );
}

function PeersPage({ client }: { client: ExplorerApiClient }): JSX.Element {
  const [showPeers, setShowPeers] = useState(false);
  const peerLoader = useMemo(() => (showPeers ? () => client.peers() : async () => [] as PeerDetail[]), [client, showPeers]);
  return (
    <>
      <QueryPanel title="Peer summary" loader={() => client.peerSummary()} deps={[client]}>
        {(summary: PeerSummary) => (
          <>
            <div className="kv-grid">
              <div><span>Peer count</span><strong>{summary.peer_count}</strong></div>
              <div><span>Backoff peers</span><strong>{summary.backoff_peer_count}</strong></div>
            </div>
            <pre>{formatJson(summary)}</pre>
          </>
        )}
      </QueryPanel>
      <section className="panel">
        <h2>Peers</h2>
        <button onClick={() => setShowPeers((value) => !value)}>{showPeers ? "Hide peers" : "Load peers"}</button>
      </section>
      {showPeers ? (
        <QueryPanel title="Peer list" loader={peerLoader} deps={[peerLoader]}>
          {(peers: PeerDetail[]) => <pre>{formatJson(peers)}</pre>}
        </QueryPanel>
      ) : null}
    </>
  );
}

export function App(): JSX.Element {
  const route = useRoute();
  const resolvedNodeConfig = useMemo(() => resolveExplorerApiBaseUrl({
    search: window.location.search,
    storedValue: localStorage.getItem(NODE_URL_KEY),
    envValue: import.meta.env.VITE_NODE_API_BASE_URL,
    runtimeValue: readRuntimeExplorerApiBase(window),
    protocol: window.location.protocol || "http:",
    hostname: window.location.hostname || "127.0.0.1",
  }), []);
  const [nodeUrl, setNodeUrl] = useNodeUrl();
  const [draftNodeUrl, setDraftNodeUrl] = useState(nodeUrl);
  const [globalError, setGlobalError] = useState<string | null>(null);
  const [connection, setConnection] = useState<NodeConnectionState | null>(null);
  const client = useMemo(() => new ExplorerApiClient(ExplorerApiClient.normalizeBaseUrl(nodeUrl)), [nodeUrl]);

  useEffect(() => {
    setDraftNodeUrl(nodeUrl);
  }, [nodeUrl]);

  useEffect(() => {
    if (resolvedNodeConfig.warning) {
      setGlobalError(resolvedNodeConfig.warning);
    }
  }, [resolvedNodeConfig.warning]);

  useEffect(() => {
    let cancelled = false;
    void client.status()
      .then((status) => {
        if (!cancelled) {
          setConnection({
            state: "connected",
            message: `Connected to ${nodeUrl} on ${status.network} at height ${status.height ?? "unknown"}.`,
          });
        }
      })
      .catch((error) => {
        if (!cancelled) {
          setConnection({
            state: "unreachable",
            message: error instanceof Error ? error.message : `Unable to reach ${nodeUrl}.`,
          });
        }
      });
    return () => {
      cancelled = true;
    };
  }, [client, nodeUrl]);

  function applyNodeUrl(): void {
    try {
      setNodeUrl(ExplorerApiClient.normalizeBaseUrl(draftNodeUrl));
      setGlobalError(null);
    } catch (error) {
      setGlobalError(error instanceof Error ? error.message : "Invalid node URL.");
    }
  }

  return (
    <main className="app-shell">
      <header className="panel">
        <h1>Chipcoin Explorer</h1>
        <p className="message">Read-only devnet explorer over the public Chipcoin node API.</p>
        <div className="field-row">
          <input value={draftNodeUrl} onChange={(event) => setDraftNodeUrl(event.target.value)} placeholder="Node API base URL" />
          <button onClick={applyNodeUrl}>Apply</button>
        </div>
        <p><strong>Node API:</strong> <span className="mono">{nodeUrl}</span></p>
        <p><strong>Node API source:</strong> {resolvedNodeConfig.source}</p>
        {connection ? <p className={connection.state === "connected" ? "message" : "message error"}>{connection.message}</p> : null}
        <p className="message">Runtime priority: `?api=...` query parameter, saved browser override, runtime global, build-time `VITE_NODE_API_BASE_URL`, then current host on port `8081`.</p>
        {globalError ? <p className="message error">{globalError}</p> : null}
        <nav className="nav-tabs">
          <button onClick={() => navigate({ name: "dashboard" })}>Dashboard</button>
          <button onClick={() => navigate({ name: "blocks" })}>Blocks</button>
          <button onClick={() => navigate({ name: "mempool" })}>Mempool</button>
          <button onClick={() => navigate({ name: "peers" })}>Peers</button>
        </nav>
      </header>
      <SearchBar />
      {route.name === "dashboard" ? <DashboardPage client={client} /> : null}
      {route.name === "blocks" ? <BlocksPage client={client} /> : null}
      {route.name === "block" ? <BlockPage client={client} route={route} /> : null}
      {route.name === "tx" ? <TxPage client={client} txid={route.txid} /> : null}
      {route.name === "address" ? <AddressPage client={client} address={route.address} /> : null}
      {route.name === "mempool" ? <MempoolPage client={client} /> : null}
      {route.name === "peers" ? <PeersPage client={client} /> : null}
    </main>
  );
}
