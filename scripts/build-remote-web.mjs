// Builds the web remote bundle (docs/internal/web-remote-plan.md, R5 / S1)
// straight into the server package, so the wheel always carries the card
// built from the same commit: sofabaton_server/ui/remote-web.js next to
// index.html and the manifest. Committed like the HA card bundles; the
// frontend CI drift check covers the directory.

import { build } from "esbuild";

await build({
  entryPoints: ["remote-card/src/remote-web.ts"],
  bundle: true,
  format: "esm",
  platform: "browser",
  target: "es2020",
  outfile: "sofabaton-x-server/src/sofabaton_server/ui/remote-web.js",
  sourcemap: false,
  legalComments: "none",
});
