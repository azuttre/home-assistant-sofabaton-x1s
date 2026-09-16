// Builds the control panel bundle (docs/internal/server-panel-plan.md, P2)
// straight into the server package, next to the web remote:
// sofabaton_server/ui/panel/panel.js beside its index.html. Committed like
// the other bundles; the frontend CI drift check covers the directory.

import { build } from "esbuild";

await build({
  entryPoints: ["server-panel/src/panel.ts"],
  bundle: true,
  format: "esm",
  platform: "browser",
  target: "es2020",
  outfile: "sofabaton-x-server/src/sofabaton_server/ui/panel/panel.js",
  sourcemap: false,
  legalComments: "none",
});
