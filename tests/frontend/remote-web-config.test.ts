// The web remote's configuration document and URL parameters
// (docs/internal/web-remote-plan.md, section 7 and R5).

import assert from "node:assert/strict";
import test from "node:test";

import {
  cardConfigForWebRemote,
  parseWebRemoteParams,
  webRemoteConfigFromCardConfig,
} from "../../remote-card/src/remote-web-config";

test("webRemoteConfigFromCardConfig drops what only Home Assistant can act on", () => {
  const document = webRemoteConfigFromCardConfig({
    type: "custom:sofabaton-virtual-remote",
    entity: "remote.living_room",
    theme: "Mushroom",
    show_automation_assist: true,
    preview_activity: "101",
    show_dpad: false,
    group_order: ["activity", "dpad"],
    key_style: "flat",
    device_mode: { enabled: true, open_device: 8 },
    hold_repeat: { enabled: true },
    custom_favorites: [
      { name: "Lights", icon: "mdi:lightbulb", tap_action: { action: "toggle", entity: "light.tv" } },
      { name: "Mute", icon: "mdi:volume-mute", command_id: 5, device_id: 2, action: { action: "none" } },
      "garbage",
    ],
  } as never);
  assert.deepEqual(document, {
    show_dpad: false,
    group_order: ["activity", "dpad"],
    key_style: "flat",
    device_mode: { enabled: true, open_device: 8 },
    hold_repeat: { enabled: true },
    custom_favorites: [{ name: "Mute", icon: "mdi:volume-mute", command_id: 5, device_id: 2 }],
  });
  assert.deepEqual(webRemoteConfigFromCardConfig(null), {});
  assert.deepEqual(webRemoteConfigFromCardConfig({ custom_favorites: [{ tap_action: {} }] } as never), {});
});

test("cardConfigForWebRemote targets the hub and honours the device request", () => {
  const config = cardConfigForWebRemote("E2:6A", { show_nav: false, entity: "remote.x" }, { openDevice: 3 });
  assert.equal(config.entity, "E2:6A");
  assert.equal(config.show_nav, false);
  assert.deepEqual(config.device_mode, { open_device: 3 });
  const plain = cardConfigForWebRemote("E2:6A", { device_mode: { enabled: true, open_device: 9 } });
  assert.deepEqual(plain.device_mode, { enabled: true, open_device: 9 });
  assert.deepEqual(cardConfigForWebRemote("E2:6A", null), { entity: "E2:6A" });
});

test("parseWebRemoteParams reads hub, lang, device, zoom and theme", () => {
  assert.deepEqual(parseWebRemoteParams("?hub=E2%3A6A&lang=nl&device=8&zoom=1.25&theme=dark"), {
    hub: "E2:6A",
    lang: "nl",
    device: 8,
    zoom: 1.25,
    theme: "dark",
  });
  assert.deepEqual(parseWebRemoteParams("", "de-DE"), {
    hub: "",
    lang: "de-DE",
    device: null,
    zoom: null,
    theme: null,
  });
  const junk = parseWebRemoteParams("?hub= AA &device=x&zoom=-2&theme=blue&lang=");
  assert.equal(junk.hub, "AA");
  assert.equal(junk.device, null);
  assert.equal(junk.zoom, null);
  assert.equal(junk.theme, null);
  assert.equal(junk.lang, undefined);
});
