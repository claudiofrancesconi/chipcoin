import { secp256k1 } from "@noble/curves/secp256k1";

import { buildWalletKeyMaterial, bytesToHex, hexToBytes, type WalletKeyMaterial } from "./keys";

export function walletKeyMaterialFromPrivateKeyHex(privateKeyHex: string): WalletKeyMaterial {
  return buildWalletKeyMaterial(privateKeyHex);
}

export function signDigestHex(privateKeyHex: string, digestHex: string): string {
  const signature = secp256k1.sign(hexToBytes(digestHex), hexToBytes(privateKeyHex), {
    lowS: true,
    prehash: false,
  });
  return bytesToHex(signature.toDERRawBytes());
}
