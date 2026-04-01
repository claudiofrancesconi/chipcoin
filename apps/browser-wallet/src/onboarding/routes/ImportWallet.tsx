import { useState } from "react";

export function ImportWallet({ onContinue }: { onContinue(privateKeyHex: string): void }): JSX.Element {
  const [privateKeyHex, setPrivateKeyHex] = useState("");
  return (
    <section>
      <h2>Import wallet</h2>
      <textarea value={privateKeyHex} onChange={(event) => setPrivateKeyHex(event.target.value)} placeholder="Private key hex" />
      <button onClick={() => onContinue(privateKeyHex)}>Continue</button>
    </section>
  );
}
