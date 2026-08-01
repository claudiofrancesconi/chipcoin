#!/usr/bin/env node
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";

const distDir = "dist";
const disallowedAssetExtensions = new Set([".wasm"]);
const disallowedBundleNeedles = [
  "@noble/post-quantum",
  "ml_dsa44",
  "internal.sign",
  "internal.verify",
  "ML-DSA-44 backend",
];
const disallowedRuntimeNeedles = [
  "node:",
  "require(",
  "eval(",
  "new Function",
  "https://cdn",
  "http://cdn",
];

function walk(dir) {
  const paths = [];
  for (const name of readdirSync(dir)) {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) {
      paths.push(...walk(path));
    } else {
      paths.push(path);
    }
  }
  return paths;
}

function assert(condition, message) {
  if (!condition) {
    throw new Error(message);
  }
}

const files = walk(distDir);
const assetNames = files.map((path) => path.slice(distDir.length + 1));
for (const asset of assetNames) {
  for (const extension of disallowedAssetExtensions) {
    assert(!asset.endsWith(extension), `unexpected ${extension} asset in production build: ${asset}`);
  }
}

for (const file of files) {
  if (!/\.(js|json|html)$/.test(file)) {
    continue;
  }
  const body = readFileSync(file, "utf8");
  for (const needle of [...disallowedBundleNeedles, ...disallowedRuntimeNeedles]) {
    assert(!body.includes(needle), `production bundle unexpectedly contains ${needle} in ${file}`);
  }
}

const manifest = JSON.parse(readFileSync(join(distDir, "manifest.json"), "utf8"));
const csp = JSON.stringify(manifest.content_security_policy ?? {});
assert(!csp.includes("unsafe-eval"), "manifest CSP contains unsafe-eval");
assert(!csp.includes("wasm-unsafe-eval"), "manifest CSP contains wasm-unsafe-eval");

const contentScript = readFileSync(join(distDir, "assets", "content_script.js"), "utf8");
assert(!/^\s*import\b/m.test(contentScript), "content_script.js must not contain ESM imports");
assert(!/^\s*export\b/m.test(contentScript), "content_script.js must not contain ESM exports");

console.log(JSON.stringify({
  ok: true,
  files: assetNames.length,
  wasm_assets: assetNames.filter((asset) => asset.endsWith(".wasm")).length,
  noble_in_production_bundle: false,
  csp_has_unsafe_eval: false,
}, null, 2));
