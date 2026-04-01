import { useEffect, useState } from "react";

import type { AppState } from "../state/app_state";
import { sendWalletMessage } from "../shared/messages";
import { Activity } from "./routes/Activity";
import { Backup } from "./routes/Backup";
import { Locked } from "./routes/Locked";
import { Overview } from "./routes/Overview";
import { Send } from "./routes/Send";
import { Settings } from "./routes/Settings";
import { SetupWallet } from "./routes/SetupWallet";

type Route = "overview" | "activity" | "send" | "backup" | "settings";

export function App(): JSX.Element {
  const [route, setRoute] = useState<Route>("overview");
  const [state, setState] = useState<AppState | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [isLoadingState, setIsLoadingState] = useState(true);

  async function loadState(): Promise<void> {
    setIsLoadingState(true);
    try {
      setState(await sendWalletMessage<AppState>({ type: "wallet:getState" }));
      setError(null);
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : "Unable to load wallet state.");
    } finally {
      setIsLoadingState(false);
    }
  }

  useEffect(() => {
    void loadState();
  }, []);

  if (error) {
    return <main className="app-shell"><h1>Chipcoin Wallet</h1><p className="message error">{error}</p></main>;
  }

  if (!state) {
    return <main className="app-shell"><h1>Chipcoin Wallet</h1><p className="message">Loading wallet state…</p></main>;
  }

  if (!state.hasWallet) {
    return (
      <main className="app-shell">
        <h1>Chipcoin Wallet</h1>
        <SetupWallet onCreated={setState} />
      </main>
    );
  }

  if (state.isLocked) {
    return <Locked onUnlocked={setState} />;
  }

  return (
    <main className="app-shell">
      <h1>Chipcoin Wallet</h1>
      <nav className="nav-tabs">
        <button className={route === "overview" ? "is-active" : ""} onClick={() => setRoute("overview")}>Overview</button>
        <button className={route === "activity" ? "is-active" : ""} onClick={() => setRoute("activity")}>Activity</button>
        <button className={route === "send" ? "is-active" : ""} onClick={() => setRoute("send")}>Send</button>
        <button className={route === "backup" ? "is-active" : ""} onClick={() => setRoute("backup")}>Backup</button>
        <button className={route === "settings" ? "is-active" : ""} onClick={() => setRoute("settings")}>Settings</button>
      </nav>
      {route === "overview" && <Overview state={state} isLoading={isLoadingState} onRefresh={loadState} />}
      {route === "activity" && <Activity state={state} />}
      {route === "send" && <Send state={state} onRefresh={loadState} />}
      {route === "backup" && <Backup />}
      {route === "settings" && <Settings state={state} onUpdated={setState} onOpenBackup={() => setRoute("backup")} />}
    </main>
  );
}
