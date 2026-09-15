// The server adapter (docs/internal/web-remote-plan.md, R3): REST + one
// WebSocket stream in, the remote entity's attribute contract out. Fixture
// bodies follow sofabaton-x-server/openapi.json component shapes.

import assert from "node:assert/strict";
import test from "node:test";

import {
  SERVER_API_PREFIX,
  ServerRemoteBackend,
  type WebSocketLike,
} from "../../remote-card/src/backend/server-backend";
import { RemoteCardStore } from "../../remote-card/src/state/remote-card-store";

const HUB = "E2:6A:44:86:1B:45";
const BASE = "http://server.test";
const PREFIX = `${BASE}${SERVER_API_PREFIX}/hubs/${encodeURIComponent(HUB)}`;

type Body = unknown | ((init?: RequestInit) => unknown);

interface Rig {
  backend: ServerRemoteBackend;
  requests: Array<{ url: string; method: string; body: unknown }>;
  routes: Record<string, Body>;
  sockets: FakeSocket[];
}

class FakeSocket implements WebSocketLike {
  onopen: ((event: unknown) => void) | null = null;
  onmessage: ((event: { data: unknown }) => void) | null = null;
  onclose: ((event: unknown) => void) | null = null;
  onerror: ((event: unknown) => void) | null = null;
  closed = false;
  constructor(public readonly url: string) {}
  close(): void {
    this.closed = true;
  }
  open(): void {
    this.onopen?.({});
  }
  push(message: unknown): void {
    this.onmessage?.({ data: JSON.stringify(message) });
  }
  drop(): void {
    this.onclose?.({});
  }
}

const STATUS = {
  hub_id: HUB,
  enabled: true,
  status: {
    hub_connected: true,
    app_connected: false,
    controllable: true,
    mode: "control",
    hub_version: "x1s",
    proxy_enabled: true,
    running_activity: { activity_id: 101, name: "Watch TV" },
    activities_cached: 2,
    devices_cached: 2,
    catalog_ready: true,
  },
};

function defaultRoutes(): Record<string, Body> {
  return {
    "GET /status": STATUS,
    "GET /activities": [
      { activity_id: 101, name: "Watch TV", active: true, needs_confirm: false },
      { activity_id: 102, name: "Listen", active: false, needs_confirm: false },
    ],
    "GET /devices": [
      { device_id: 1, name: "TV", brand: "Sony", device_class: "ir", device_class_code: 1, power_state: 0, idle_behavior: 2 },
      { device_id: 2, name: "Amp", brand: "Denon", device_class: "ir", device_class_code: 1, power_state: 1, idle_behavior: null },
    ],
    "GET /activity": { activity_id: 101, name: "Watch TV" },
    "GET /entities/101/buttons": [
      { button_code: 174, name: "UP", device_id: 1, command_id: 17, long_press_device_id: null, long_press_command_id: null },
      { button_code: 175, name: "DOWN", device_id: 1, command_id: 18, long_press_device_id: 2, long_press_command_id: 5 },
    ],
    "GET /activities/101/macros": [{ command_id: 200, label: "All On" }],
    "GET /activities/101/favorites": [{ device_id: 1, command_id: 1, label: "Power" }],
    "GET /entities/102/buttons": [{ button_code: 151, name: "OK", device_id: 2, command_id: 3 }],
    "GET /activities/102/macros": [],
    "GET /activities/102/favorites": [],
    "GET /entities/1/buttons": [
      { button_code: 151, name: "OK", device_id: 1, command_id: 9, long_press_device_id: 1, long_press_command_id: 10 },
    ],
    "GET /devices/1/commands": [
      { command_id: 9, label: "Select" },
      { command_id: 10, label: "Menu" },
    ],
    "GET /devices/1/power-state": { device_id: 1, power_state: 0 },
    "GET /devices/2/power-state": { device_id: 2, power_state: null },
    "POST /send": {},
    "POST /activities/102/start": { accepted: true, mode: "control" },
    "POST /activities/101/stop": { accepted: true, mode: "control" },
  };
}

function createRig(overrides: Record<string, Body> = {}): Rig {
  const routes = { ...defaultRoutes(), ...overrides };
  const requests: Rig["requests"] = [];
  const sockets: FakeSocket[] = [];
  const fetchImpl = (async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const method = String(init?.method ?? "GET").toUpperCase();
    const body = init?.body ? JSON.parse(String(init.body)) : undefined;
    requests.push({ url, method, body });
    assert.ok(url.startsWith(PREFIX), `unexpected origin ${url}`);
    const key = `${method} ${url.slice(PREFIX.length)}`;
    const route = routes[key];
    if (route === undefined) {
      return { ok: false, status: 404, json: async () => ({ type: "not_found" }) } as Response;
    }
    const payload = typeof route === "function" ? (route as (init?: RequestInit) => unknown)(init) : route;
    if (payload instanceof Error) {
      return { ok: false, status: 504, json: async () => ({ type: "timeout" }) } as Response;
    }
    return { ok: true, status: 200, json: async () => payload } as Response;
  }) as typeof fetch;
  const backend = new ServerRemoteBackend({
    baseUrl: BASE,
    fetch: fetchImpl,
    webSocket: (url) => {
      const socket = new FakeSocket(url);
      sockets.push(socket);
      return socket;
    },
    reconnectDelayMs: 100,
  });
  return { backend, requests, routes, sockets };
}

const flush = async (rounds = 4) => {
  for (let i = 0; i < rounds; i++) await new Promise((resolve) => setTimeout(resolve, 0));
};

test("server adapter: initial load produces the entity attribute contract", async () => {
  const { backend, sockets } = createRig();
  backend.setTarget(HUB);
  assert.equal(backend.snapshot()?.state, "unavailable", "nothing loaded yet");
  const changes: number[] = [];
  backend.subscribe(() => changes.push(Date.now()));
  assert.equal(await backend.probeIntegration(), "x1s");
  await flush();

  const snapshot = backend.snapshot();
  assert.equal(snapshot?.state, "on");
  const attrs = snapshot?.attributes ?? {};
  assert.equal(attrs.hub_version, "X1S");
  assert.equal(attrs.current_activity, "Watch TV");
  assert.equal(attrs.current_activity_id, 101);
  assert.equal(attrs.load_state, "ready");
  assert.deepEqual(attrs.activities, [
    { id: 101, name: "Watch TV", state: "on" },
    { id: 102, name: "Listen", state: "off" },
  ]);
  assert.deepEqual(attrs.devices, [
    { id: 1, name: "TV", device_class: "ir" },
    { id: 2, name: "Amp", device_class: "ir" },
  ]);
  assert.deepEqual(attrs.assigned_keys, { "101": [174, 175] });
  assert.deepEqual(attrs.macro_keys, { "101": [{ id: 200, name: "All On" }] });
  assert.deepEqual(attrs.favorite_keys, { "101": [{ id: 1, name: "Power", device_id: 1 }] });
  assert.deepEqual(attrs.long_press_keys, { "101": { "175": { device_id: 2, command_id: 5 } } });
  assert.ok(changes.length >= 1, "subscribers were notified");
  assert.equal(sockets.length, 1);
  assert.equal(
    sockets[0].url,
    `ws://server.test${SERVER_API_PREFIX}/events?hub_id=${encodeURIComponent(HUB)}`,
  );
});

test("server adapter: unavailable when the hub is not controllable or the load fails", async () => {
  const observed = createRig({
    "GET /status": { ...STATUS, status: { ...STATUS.status, controllable: false, mode: "observe", app_connected: true } },
  });
  observed.backend.setTarget(HUB);
  observed.backend.subscribe(() => undefined);
  await flush();
  assert.equal(observed.backend.snapshot()?.state, "unavailable");
  assert.equal(observed.backend.snapshot()?.attributes?.current_activity_id, null);
  assert.equal(observed.backend.snapshot()?.attributes?.activities?.length, 2, "catalog still listed");

  const failing = createRig({ "GET /status": new Error("timeout") });
  failing.backend.setTarget(HUB);
  failing.backend.subscribe(() => undefined);
  await flush();
  assert.equal(failing.backend.snapshot()?.state, "unavailable");
  assert.equal(failing.backend.snapshot()?.attributes?.load_state, "loading");
  assert.match(failing.backend.lastError ?? "", /504/);
  await assert.rejects(() => failing.backend.probeIntegration());
});

test("server adapter: sends, activity start/stop use the hub routes", async () => {
  const { backend, requests } = createRig();
  backend.setTarget(HUB);
  backend.subscribe(() => undefined);
  await flush();
  requests.length = 0;

  await backend.sendCommand(174, 101);
  await backend.sendCommand(17, 1);
  await backend.sendCommand(174, null); // no scope: the running activity
  await backend.sendCommand("x", 101); // never sent
  await backend.startActivity({ id: null, name: "Listen" });
  await backend.stopActivity();
  assert.deepEqual(
    requests.map((request) => [request.method, request.url.slice(PREFIX.length), request.body]),
    [
      ["POST", "/send", { entity_id: 101, command_id: 174 }],
      ["POST", "/send", { entity_id: 1, command_id: 17 }],
      ["POST", "/send", { entity_id: 101, command_id: 174 }],
      ["POST", "/activities/102/start", undefined],
      ["POST", "/activities/101/stop", undefined],
    ],
  );
});

test("server adapter: device keymap and power state mirror the HA projections", async () => {
  const { backend, requests } = createRig();
  backend.setTarget(HUB);
  backend.subscribe(() => undefined);
  await flush();

  const keymap = await backend.deviceKeymap(1);
  assert.deepEqual(keymap, {
    keymap: {
      device: { device_id: 1, name: "TV", device_class: "ir" },
      buttons: [151],
      bindings: [{ button_id: 151, button_name: "OK", command_id: 9, long_press_command_id: 10 }],
      commands: [
        { command_id: 9, name: "Select" },
        { command_id: 10, name: "Menu" },
      ],
      power_configured: true,
    },
  });
  // The device page's long-press pair joins the attribute contract.
  assert.deepEqual(backend.snapshot()?.attributes?.long_press_keys?.["1"], {
    "151": { device_id: 1, command_id: 10 },
  });
  const before = requests.length;
  await backend.deviceKeymap(1);
  assert.equal(requests.length, before, "second read served from the page cache");
  assert.deepEqual(await backend.deviceKeymap(99), { keymap: null, reason: "cache_miss" });

  assert.equal(await backend.devicePowerState(1), 0);
  assert.equal(await backend.devicePowerState(2), null);
  assert.equal(await backend.devicePowerState(99), null, "404 reads as unreadable");
});

test("server adapter: stream events move the running activity and reload on catalog changes", async () => {
  const { backend, sockets, routes, requests } = createRig();
  backend.setTarget(HUB);
  const changes: string[] = [];
  backend.subscribe(() => changes.push(String(backend.snapshot()?.attributes?.current_activity_id)));
  await flush();
  const socket = sockets[0];
  socket.open();
  await flush();

  socket.push({
    type: "hub_event",
    hub_id: HUB,
    event: { seq: 7, kind: "activity_changed", payload: { activity_id: 102, previous_activity_id: 101, name: "Listen" } },
  });
  await flush();
  assert.equal(backend.snapshot()?.attributes?.current_activity_id, 102);
  assert.equal(backend.snapshot()?.attributes?.current_activity, "Listen");
  assert.deepEqual(backend.snapshot()?.attributes?.assigned_keys, { "101": [174, 175], "102": [151] });
  assert.equal(backend.snapshot()?.attributes?.activities?.[1]?.state, "on");

  socket.push({
    type: "hub_event",
    hub_id: HUB,
    event: { seq: 8, kind: "activity_changed", payload: { activity_id: null, previous_activity_id: 102, name: null } },
  });
  await flush();
  assert.equal(backend.snapshot()?.state, "off");
  assert.equal(backend.snapshot()?.attributes?.current_activity_id, null);

  // Another hub's events are ignored.
  socket.push({ type: "hub_event", hub_id: "other", event: { seq: 1, kind: "activity_changed", payload: { activity_id: 101 } } });
  await flush();
  assert.equal(backend.snapshot()?.attributes?.current_activity_id, null);

  // A snapshot change naming activity 101 drops its page and re-reads the catalog.
  routes["GET /activities"] = [
    { activity_id: 101, name: "Watch Movies", active: false, needs_confirm: false },
    { activity_id: 102, name: "Listen", active: false, needs_confirm: false },
  ];
  routes["GET /entities/101/buttons"] = [{ button_code: 190, name: "MENU", device_id: 1, command_id: 30 }];
  routes["GET /activity"] = { activity_id: 101, name: "Watch Movies" };
  requests.length = 0;
  socket.push({
    type: "hub_event",
    hub_id: HUB,
    event: { seq: 9, kind: "snapshot_changed", payload: { snapshot_id: "s2", engine_generation: 3, device_ids: [], activity_ids: [101] } },
  });
  await flush();
  assert.equal(backend.snapshot()?.attributes?.activities?.[0]?.name, "Watch Movies");
  assert.deepEqual(backend.snapshot()?.attributes?.assigned_keys?.["101"], [190]);
  assert.deepEqual(backend.snapshot()?.attributes?.assigned_keys?.["102"], [151], "untouched page kept");

  // hub_state down: status re-read; the fixture now says disconnected.
  routes["GET /status"] = { ...STATUS, status: { ...STATUS.status, hub_connected: false, controllable: false, mode: "disconnected" } };
  socket.push({ type: "hub_event", hub_id: HUB, event: { seq: 10, kind: "hub_state", payload: { connected: false } } });
  await flush();
  assert.equal(backend.snapshot()?.state, "unavailable");
  assert.ok(changes.length >= 4);
});

test("server adapter: a dropped socket reconnects with backoff and reloads; unsubscribe stops it", async () => {
  const { backend, sockets, requests } = createRig();
  backend.setTarget(HUB);
  const unsubscribe = backend.subscribe(() => undefined);
  await flush();
  sockets[0].open();
  await flush();
  requests.length = 0;
  sockets[0].drop();
  await new Promise((resolve) => setTimeout(resolve, 150));
  assert.equal(sockets.length, 2, "reconnected after the first delay");
  sockets[1].open();
  await flush();
  assert.ok(
    requests.some((request) => request.url.endsWith("/status")),
    "reconnect reloads the state",
  );
  unsubscribe();
  assert.equal(sockets[1].closed, true);
  sockets[1].drop();
  await new Promise((resolve) => setTimeout(resolve, 150));
  assert.equal(sockets.length, 2, "no reconnect once stopped");
});

test("server adapter: retargeting resets state and reconnects", async () => {
  const { backend, sockets } = createRig();
  backend.setTarget(HUB);
  backend.subscribe(() => undefined);
  await flush();
  assert.equal(backend.snapshot()?.state, "on");
  backend.setTarget("AA:BB");
  assert.equal(backend.snapshot()?.state, "unavailable");
  assert.equal(sockets[0].closed, true);
  await flush();
  assert.equal(sockets.length, 2);
  assert.match(sockets[1].url, /hub_id=AA%3ABB$/);
});

test("store over the server adapter: the card's derivations run unchanged", async () => {
  const { backend, requests } = createRig();
  let changes = 0;
  const store = new RemoteCardStore(() => (changes += 1), { fireEvent: () => undefined });
  store.setConfig({ entity: HUB });
  store.setBackend(backend);
  await flush();

  assert.equal(store.hass, null);
  assert.equal(store.hubVersion(), "X1S");
  assert.equal(store.isX2(), false);
  assert.equal(store.currentActivityId(), 101);
  assert.equal(store.currentActivityLabel(), "Watch TV");
  assert.deepEqual(store.activities().map((activity) => activity.id), [101, 102]);
  assert.equal(store.deviceModeAvailable(), true);
  const derived = store.deriveRuntimeState();
  assert.equal(derived.isUnavailable, false);
  assert.deepEqual(derived.rawAssignedKeys, [174, 175]);
  assert.equal(store.isEnabled(174), true);
  assert.equal(store.isEnabled(151), false, "not on the activity's page");
  assert.deepEqual(store.longPressBindingForButton(175, 101), { device_id: 2, command_id: 5 });
  assert.equal(store.longPressBindingForButton(174, 101), null);

  requests.length = 0;
  await store.sendCommand(174);
  await store.sendLongPress(175, 101);
  await store.setActivity("Listen");
  assert.deepEqual(
    requests.map((request) => [request.url.slice(PREFIX.length), request.body]),
    [
      ["/send", { entity_id: 101, command_id: 174 }],
      ["/send", { entity_id: 2, command_id: 5 }],
      ["/activities/102/start", undefined],
    ],
  );
  assert.ok(changes > 0);
  store.disconnected();
});
