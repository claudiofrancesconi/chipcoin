import { copyFileSync, mkdirSync } from "node:fs";
import { resolve } from "node:path";

import { defineConfig, loadEnv } from "vite";

const targetBrowser = process.env.VITE_TARGET_BROWSER
  ?? (process.env.npm_lifecycle_event?.includes("firefox") ? "firefox" : "chrome");

function avoidInnerHtmlAssignments(code: string): string {
  return code
    .replace(/\.innerHTML\b/g, ".textContent")
    .replace(/dangerouslySetInnerHTML/g, "dangerouslySetTextContent");
}

export default defineConfig(({ mode }) => {
  const repoEnv = loadEnv(mode, resolve(__dirname, "../.."), "");
  const defaultNodeEndpoint = repoEnv.BROWSER_WALLET_DEFAULT_NODE_ENDPOINT
    || repoEnv.DEFAULT_NODE_ENDPOINT
    || "https://api.chipcoinprotocol.com";
  const defaultExplorerUrl = repoEnv.BROWSER_WALLET_DEFAULT_EXPLORER_URL
    || repoEnv.DEFAULT_EXPLORER_URL
    || "https://explorer.chipcoinprotocol.com";

  return {
  plugins: [
    {
      name: "copy-browser-manifest",
      writeBundle(outputOptions) {
        const outDir = outputOptions.dir ?? "dist";
        mkdirSync(outDir, { recursive: true });
        copyFileSync(
          resolve(__dirname, "manifest", `${targetBrowser}.json`),
          resolve(outDir, "manifest.json"),
        );
      },
    },
    {
      name: "avoid-amo-innerhtml-warning",
      renderChunk(code) {
        if (!code.includes("innerHTML") && !code.includes("dangerouslySetInnerHTML")) {
          return null;
        }
        return {
          code: avoidInnerHtmlAssignments(code),
          map: null,
        };
      },
    },
  ],
  define: {
    __CHIPCOIN_DEFAULT_NODE_ENDPOINT__: JSON.stringify(defaultNodeEndpoint),
    __CHIPCOIN_DEFAULT_EXPLORER_URL__: JSON.stringify(defaultExplorerUrl),
  },
  resolve: {
    alias: {
      react: "preact/compat",
      "react-dom": "preact/compat",
      "react-dom/client": "preact/compat/client",
      "react/jsx-runtime": "preact/jsx-runtime",
      "react/jsx-dev-runtime": "preact/jsx-dev-runtime",
    },
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
    rollupOptions: {
      input: {
        popup: resolve(__dirname, "popup.html"),
        onboarding: resolve(__dirname, "onboarding.html"),
        settings: resolve(__dirname, "settings.html"),
        approval: resolve(__dirname, "approval.html"),
        background: resolve(__dirname, "src/background/index.ts"),
        content_script: resolve(__dirname, "src/provider/content_script.ts"),
        page_provider: resolve(__dirname, "src/provider/page_provider.ts"),
      },
      output: {
        entryFileNames: "assets/[name].js",
        chunkFileNames: "assets/[name]-[hash].js",
        assetFileNames: "assets/[name]-[hash][extname]",
      },
    },
  },
  };
});
