import { useState } from "react";

import { ConfigureNode } from "./ConfigureNode";
import { CreateWallet } from "./CreateWallet";
import { ImportWallet } from "./ImportWallet";
import { SetPassword } from "./SetPassword";

type Step = "welcome" | "create" | "import" | "password" | "node";

export function OnboardingApp(): JSX.Element {
  const [step, setStep] = useState<Step>("welcome");
  const [mode, setMode] = useState<"create" | "import">("create");
  const [privateKeyHex, setPrivateKeyHex] = useState("");

  return (
    <main>
      <h1>Chipcoin Wallet Onboarding</h1>
      {step === "welcome" && (
        <section>
          <p>Create or import a devnet wallet. Private keys stay client-side.</p>
          <button onClick={() => { setMode("create"); setStep("password"); }}>Create wallet</button>
          <button onClick={() => { setMode("import"); setStep("import"); }}>Import wallet</button>
        </section>
      )}
      {step === "create" && <CreateWallet onContinue={() => setStep("password")} />}
      {step === "import" && <ImportWallet onContinue={(value) => { setPrivateKeyHex(value); setStep("password"); }} />}
      {step === "password" && (
        <SetPassword
          mode={mode}
          privateKeyHex={privateKeyHex}
          onCreated={() => setStep("node")}
        />
      )}
      {step === "node" && <ConfigureNode />}
    </main>
  );
}
