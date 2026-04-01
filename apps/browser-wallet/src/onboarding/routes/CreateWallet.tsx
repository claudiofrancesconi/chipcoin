export function CreateWallet({ onContinue }: { onContinue(): void }): JSX.Element {
  return (
    <section>
      <h2>Create wallet</h2>
      <p>The wallet will generate a new secp256k1 private key locally after you set a password.</p>
      <button onClick={onContinue}>Continue</button>
    </section>
  );
}
