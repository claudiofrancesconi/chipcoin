import { copyFileSync, mkdirSync } from "node:fs";
import { resolve } from "node:path";

import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

const targetBrowser = process.env.VITE_TARGET_BROWSER
  ?? (process.env.npm_lifecycle_event?.includes("firefox") ? "firefox" : "chrome");

export default defineConfig({
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
});
