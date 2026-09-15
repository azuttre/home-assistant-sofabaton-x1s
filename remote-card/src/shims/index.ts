// The web remote's platform shims (docs/internal/web-remote-plan.md, R4):
// everything the remote card expects Home Assistant's frontend to provide,
// registered under the same tags so the card's own markup and styling stay
// untouched. The HA build never imports this module.

import { defineHaCardShim } from "./ha-card";
import { defineHaIconShim } from "./ha-icon";
import { defineHaSelectShim } from "./ha-select";
import { REMOTE_WEB_PALETTE_CSS } from "./palette";

export { mdiPathFor } from "./ha-icon";
export { CARD_ICON_NAMES, MDI_ICON_PATHS } from "./mdi-icons";
export { REMOTE_WEB_PALETTE_CSS } from "./palette";

const PALETTE_STYLE_ID = "sofabaton-remote-web-palette";

/** Install the HA default palette on the document (once). */
export function installRemoteWebPalette(doc: Document = document): void {
  if (doc.getElementById(PALETTE_STYLE_ID)) return;
  const style = doc.createElement("style");
  style.id = PALETTE_STYLE_ID;
  style.textContent = REMOTE_WEB_PALETTE_CSS;
  doc.head.appendChild(style);
}

/** Define every element the card needs; idempotent, HA-safe (skips defined tags). */
export function defineRemoteWebElements(): void {
  defineHaCardShim();
  defineHaIconShim();
  defineHaSelectShim();
}

/** Palette plus elements: what the page calls before the card is created. */
export function installRemoteWebShims(doc: Document = document): void {
  installRemoteWebPalette(doc);
  defineRemoteWebElements();
}
