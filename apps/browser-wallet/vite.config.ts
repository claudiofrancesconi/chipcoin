import { copyFileSync, mkdirSync } from "node:fs";
import { resolve } from "node:path";

import react from "@vitejs/plugin-react";
import { defineConfig, loadEnv } from "vite";

const targetBrowser = process.env.VITE_TARGET_BROWSER
  ?? (process.env.npm_lifecycle_event?.includes("firefox") ? "firefox" : "chrome");

export default defineConfig(({ mode }) => {
  const repoEnv = loadEnv(mode, resolve(__dirname, "../.."), "");
  const defaultNodeEndpoint = repoEnv.BROWSER_WALLET_DEFAULT_NODE_ENDPOINT
    || repoEnv.DEFAULT_NODE_ENDPOINT
    || "https://api.chipcoinprotocol.com";

  return {
  plugins: [
    react(),
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
  ],
  define: {
    __CHIPCOIN_DEFAULT_NODE_ENDPOINT__: JSON.stringify(defaultNodeEndpoint),
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
    rollupOptions: {
      input: {
        popup: resolve(__dirname, "popup.html"),
        onboarding: resolve(__dirname, "onboarding.html"),
        settings: resolve(__dirname, "settings.html"),
        background: resolve(__dirname, "src/background/index.ts"),
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
