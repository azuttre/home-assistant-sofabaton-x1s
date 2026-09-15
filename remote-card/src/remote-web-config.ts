// The web remote's configuration document (docs/internal/web-remote-plan.md,
// section 7): the HA card's config minus what only Home Assistant can act
// on. Shared by the HA editor's "Copy config for the web remote" action
// and the page host, so both sides agree on what the document holds.

import type { RemoteCardConfig } from "./remote-card-types";

/** Keys that mean nothing on the web remote. */
const DROPPED_KEYS = new Set(["type", "entity", "theme", "show_automation_assist", "preview_activity"]);

/** Per-favourite keys that carry Home Assistant actions. */
const DROPPED_FAVORITE_KEYS = new Set(["action", "tap_action", "hold_action", "double_tap_action"]);

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

/**
 * Strip a card config down to the web remote's document. Custom favourites
 * keep only entries that name a hub command (device_id + command_id);
 * Lovelace-action favourites are dropped because the page cannot run them.
 */
export function webRemoteConfigFromCardConfig(
  config: Partial<RemoteCardConfig> | Record<string, unknown> | null | undefined,
): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  if (!isPlainObject(config)) return out;
  for (const [key, value] of Object.entries(config)) {
    if (DROPPED_KEYS.has(key) || value === undefined) continue;
    if (key === "custom_favorites" && Array.isArray(value)) {
      const kept = value
        .filter((item) => isPlainObject(item) && item.command_id != null && item.device_id != null)
        .map((item) => {
          const favorite: Record<string, unknown> = {};
          for (const [k, v] of Object.entries(item as Record<string, unknown>)) {
            if (!DROPPED_FAVORITE_KEYS.has(k)) favorite[k] = v;
          }
          return favorite;
        });
      if (kept.length) out.custom_favorites = kept;
      continue;
    }
    out[key] = value;
  }
  return out;
}

export interface WebRemoteParams {
  hub: string;
  lang: string | undefined;
  device: number | null;
  zoom: number | null;
  theme: "light" | "dark" | null;
}

/** The page's URL parameters: hub (required), lang, device, zoom, theme. */
export function parseWebRemoteParams(search: string, navigatorLanguage?: string): WebRemoteParams {
  const params = new URLSearchParams(search);
  const device = Number(params.get("device"));
  const zoom = Number(params.get("zoom"));
  const theme = params.get("theme");
  return {
    hub: (params.get("hub") ?? "").trim(),
    lang: (params.get("lang") ?? navigatorLanguage ?? "").trim() || undefined,
    device: params.has("device") && Number.isFinite(device) ? device : null,
    zoom: params.has("zoom") && Number.isFinite(zoom) && zoom > 0 ? zoom : null,
    theme: theme === "light" || theme === "dark" ? theme : null,
  };
}

/**
 * Build the config the card element takes on the page: the stored
 * document over the defaults, the hub id as the target, and the URL's
 * device request as the opening view.
 */
export function cardConfigForWebRemote(
  hubId: string,
  document: Record<string, unknown> | null | undefined,
  options: { openDevice?: number | null } = {},
): RemoteCardConfig {
  const base = webRemoteConfigFromCardConfig(document);
  const config = { ...base, entity: hubId } as RemoteCardConfig;
  if (options.openDevice != null) {
    const deviceMode = isPlainObject(config.device_mode) ? { ...config.device_mode } : {};
    deviceMode.open_device = options.openDevice;
    config.device_mode = deviceMode;
  }
  return config;
}
