import { describe, expect, it } from "vitest";

import chromeManifest from "../../manifest/chrome.json";
import firefoxManifest from "../../manifest/firefox.json";

describe("provider manifest injection", () => {
  for (const [name, manifest] of Object.entries({ chrome: chromeManifest, firefox: firefoxManifest })) {
    it(`${name} declares content script and page provider resources for chipcoinprotocol.com`, () => {
      expect(manifest.content_scripts).toEqual([
        expect.objectContaining({
          matches: ["https://chipcoinprotocol.com/*"],
          js: ["assets/content_script.js"],
          run_at: "document_start",
          all_frames: false,
        }),
      ]);
      expect(manifest.web_accessible_resources).toEqual([
        expect.objectContaining({
          resources: ["assets/page_provider.js"],
          matches: ["https://chipcoinprotocol.com/*"],
        }),
      ]);
    });
  }
});
