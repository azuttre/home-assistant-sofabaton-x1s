// The control panel (docs/internal/server-panel-plan.md, P3): the built
// bundle from sofabaton_server/ui/panel served by the fixtures server,
// with the server's REST routes mocked at the page's own origin and the
// /events stream mocked with routeWebSocket. Fixture bodies follow
// openapi.json shapes (HubView, SeenHub, Problem, RemoteCardDocument).

import { test, expect } from "@playwright/test";

const PAGE = "/sofabaton-x-server/src/sofabaton_server/ui/panel/index.html";
// The panel derives the server base from its own URL (everything before
// "/ui/"), so the mocked routes sit under the package directory.
const API = "/sofabaton-x-server/src/sofabaton_server/api/v1";

const CONTROL = {
  hub_connected: true, app_connected: false, controllable: true, mode: "control", hub_version: "X1S",
  proxy_enabled: true, running_activity: { activity_id: 101, name: "Watch TV" }, activities_cached: 2, devices_cached: 2, catalog_ready: true,
};
const LIVING = {
  hub_id: "e26a44861b45",
  enabled: true,
  config: { host: "192.168.1.50", name: "Living room", hub_version: "X1S", mac: "E2:6A:44:86:1B:45" },
  status: CONTROL,
  added_at: "2026-09-15T00:00:00Z",
  last_seen: "2026-09-15T10:00:00Z",
};
const OFFICE = {
  hub_id: "192.168.1.60",
  enabled: false,
  config: { host: "192.168.1.60", name: null, hub_version: null, mac: null },
  status: null,
  added_at: "2026-09-15T00:00:00Z",
  last_seen: null,
};
const SEEN_NEW = {
  key: "cb383539684b",
  config: {
    host: "192.168.1.70", port: 8102, name: "Bedroom", mac: "CB:38:35:39:68:4B", txt: { md: "X1" },
    hub_version: "X1", hub_listen_port: 8200, app_discovery_port: 8102, proxy_enabled: true, is_proxy: false, source: "server",
  },
  first_seen: "2026-09-15T09:00:00Z",
  last_seen: "2026-09-15T10:00:00Z",
  present: true,
  registered_hub_id: null,
};
const SEEN_KNOWN = { ...SEEN_NEW, key: "e26a44861b45", config: { ...SEEN_NEW.config, host: "192.168.1.50", name: "Living room", mac: "E2:6A:44:86:1B:45", hub_version: "X1S" }, registered_hub_id: "e26a44861b45" };

function problem(status, type, detail, extra = {}) {
  return { status, body: { type, title: type, status, detail, hub_id: null, mode: null, ...extra } };
}

// What the remote card reads for the Living room hub when the Remote view mounts it.
function cardRoutes(id) {
  return {
    [`GET /hubs/${id}/status`]: () => ({ status: 200, body: { ...LIVING, hub_id: id } }),
    [`GET /hubs/${id}/activities`]: () => ({ status: 200, body: [
      { activity_id: 101, name: "Watch TV", active: true, needs_confirm: false },
      { activity_id: 102, name: "Listen", active: false, needs_confirm: false },
    ] }),
    [`GET /hubs/${id}/devices`]: () => ({ status: 200, body: [
      { device_id: 1, name: "TV", brand: "Sony", device_class: "ir", device_class_code: 1, power_state: 0, idle_behavior: 2 },
    ] }),
    [`GET /hubs/${id}/activity`]: () => ({ status: 200, body: { activity_id: 101, name: "Watch TV" } }),
    [`GET /hubs/${id}/entities/101/buttons`]: () => ({ status: 200, body: [
      { button_code: 151, name: "OK", device_id: 1, command_id: 9, long_press_device_id: null, long_press_command_id: null },
      { button_code: 174, name: "UP", device_id: 1, command_id: 17, long_press_device_id: null, long_press_command_id: null },
    ] }),
    [`GET /hubs/${id}/activities/101/macros`]: () => ({ status: 200, body: [] }),
    [`GET /hubs/${id}/activities/101/favorites`]: () => ({ status: 200, body: [] }),
    [`GET /hubs/${id}/entities/102/buttons`]: () => ({ status: 200, body: [] }),
    [`GET /hubs/${id}/activities/102/macros`]: () => ({ status: 200, body: [] }),
    [`GET /hubs/${id}/activities/102/favorites`]: () => ({ status: 200, body: [] }),
    [`POST /hubs/${id}/send`]: () => ({ status: 200, body: { accepted: true, mode: "control" } }),
  };
}

// A tiny in-memory server: the hub list, the lifecycle routes, discovery,
// the remote-card document and what the card itself reads.
function makeRoutes(state) {
  const find = (id) => state.hubs.find((h) => h.hub_id === id);
  return {
    "GET /server": () => ({ status: 200, body: { version: "0.2.0", library_version: "0.2.0", api_version: "1", instance_id: "i1", callback_listener: { wanted: false, bound: false } } }),
    "GET /openapi.json": () => ({ status: 200, body: { paths: { "/api/v1/hubs/{hub_id}/status": { get: { operationId: "getStatus", summary: "Status" } } } } }),
    "GET /hubs": () => ({ status: 200, body: state.hubs }),
    "GET /discovery/hubs": () => ({ status: 200, body: state.seen }),
    "POST /discovery/scan": () => ({ status: 200, body: state.seen }),
    "POST /hubs": (body) => {
      if (state.hubs.some((h) => h.config.host === body.host)) {
        return problem(409, "hub_conflict", "hub already registered as e26a44861b45", { hub_id: "e26a44861b45" });
      }
      if (state.startFails) {
        state.hubs.push({ ...OFFICE, hub_id: body.host, enabled: body.enabled, config: { host: body.host, name: body.name || null }, status: null });
        return problem(503, "hub_start_failed", "port 8200 in use", { hub_id: body.host, mode: "disconnected" });
      }
      const hub = {
        hub_id: body.mac ? body.mac.toLowerCase().replace(/:/g, "") : body.host,
        enabled: body.enabled,
        config: { host: body.host, name: body.name || null, hub_version: body.hub_version || null, mac: body.mac || null },
        status: body.enabled ? { ...CONTROL, hub_connected: false, mode: "disconnected", catalog_ready: false, running_activity: null } : null,
        added_at: "2026-09-16T00:00:00Z",
        last_seen: null,
      };
      state.hubs.push(hub);
      for (const s of state.seen) if (s.config.host === body.host) s.registered_hub_id = hub.hub_id;
      return { status: 201, body: hub };
    },
    "POST /hubs/{id}/enable": (_body, id) => {
      const hub = find(id); if (!hub) return problem(404, "hub_not_found", null, { hub_id: id });
      hub.enabled = true; hub.status = { ...CONTROL };
      return { status: 200, body: hub };
    },
    "POST /hubs/{id}/disable": (_body, id) => {
      const hub = find(id); if (!hub) return problem(404, "hub_not_found", null, { hub_id: id });
      if (state.jobRuns) return problem(409, "hub_job_running", "job j1 (restore) is running; wait for it to finish", { hub_id: id });
      hub.enabled = false; hub.status = null;
      return { status: 200, body: hub };
    },
    "DELETE /hubs/{id}": (_body, id) => {
      if (!find(id)) return problem(404, "hub_not_found", null, { hub_id: id });
      state.hubs = state.hubs.filter((h) => h.hub_id !== id);
      return { status: 204, body: null };
    },
    "GET /hubs/{id}/ui/remote-card": (_body, id) => ({ status: 200, body: { hub_id: id, document: state.document || null, updated_at: state.document ? "2026-09-16T00:00:00Z" : null } }),
    "PUT /hubs/{id}/ui/remote-card": (body, id) => {
      state.document = body.document;
      return { status: 200, body: { hub_id: id, document: body.document, updated_at: "2026-09-16T01:00:00Z" } };
    },
    "DELETE /hubs/{id}/ui/remote-card": () => { state.document = null; return { status: 204, body: null }; },
    ...cardRoutes(LIVING.hub_id),
  };
}

async function mockServer(page, state) {
  // The handlers mutate the records; never the module's fixtures.
  state.hubs = JSON.parse(JSON.stringify(state.hubs));
  state.seen = JSON.parse(JSON.stringify(state.seen));
  const routes = makeRoutes(state);
  const calls = [];
  await page.route(`**${API}/**`, async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const rel = decodeURIComponent(url.pathname.slice(url.pathname.indexOf("/api/v1") + "/api/v1".length));
    const method = request.method();
    const body = request.postData() ? JSON.parse(request.postData()) : null;
    let handler = routes[`${method} ${rel}`];
    let id = null;
    if (!handler) {
      const m = rel.match(/^\/hubs\/([^/]+)(\/enable|\/disable|\/ui\/remote-card)?$/);
      if (m) { id = m[1]; handler = routes[`${method} /hubs/{id}${m[2] || ""}`]; }
    }
    calls.push({ key: `${method} ${rel}`, body });
    if (!handler) {
      await route.fulfill({ status: 404, contentType: "application/json", body: JSON.stringify({ type: "not_found" }) });
      return;
    }
    const answer = handler(body, id);
    if (answer.status === 204) { await route.fulfill({ status: 204 }); return; }
    await route.fulfill({ status: answer.status, contentType: "application/json", body: JSON.stringify(answer.body) });
  });
  const sockets = [];
  await page.routeWebSocket(`**${API}/events**`, (ws) => {
    sockets.push(ws);
    ws.send(JSON.stringify({ type: "hello", server_version: "0.2.0", api_version: "1", hubs: state.hubs.map((h) => ({ hub_id: h.hub_id, enabled: h.enabled })), instance_id: "i1" }));
  });
  return { calls, sockets };
}

const items = (page) => page.locator("#hub-list .hub-item");
const detail = (page) => page.locator("#hub-detail");
const actions = (page) => page.locator("#hub-actions");
const msg = (page) => page.locator("#hubs-msg");

test.describe("control panel, hubs", () => {
  test("lists the registered hubs, selects the first, shows its detail", async ({ page }) => {
    await mockServer(page, { hubs: [LIVING, OFFICE], seen: [SEEN_KNOWN, SEEN_NEW] });
    await page.goto(PAGE);
    await expect(items(page)).toHaveCount(2);
    const living = items(page).nth(0);
    await expect(living).toContainText("Living room");
    await expect(living).toContainText("192.168.1.50 · connected, in control");
    await expect(living).toHaveClass(/sel/);
    await expect(detail(page)).toContainText("Living room");
    await expect(detail(page)).toContainText("e26a44861b45");
    await expect(detail(page)).toContainText("2 devices · 2 activities");
    await expect(detail(page)).toContainText("Watch TV");
    await expect(actions(page).getByRole("button", { name: "Disable" })).toBeVisible();
    await expect(actions(page).getByRole("button", { name: "Enable" })).toHaveCount(0);
    await expect(page.locator("#hubs-summary")).toHaveText("1/2 connected");
    await expect(page.locator("#server-meta")).toContainText("server 0.2.0 · library 0.2.0");
    await expect(page.locator("#ws-state")).toHaveText("stream live");

    const office = items(page).nth(1);
    await expect(office).toContainText("192.168.1.60");
    await expect(office).toContainText("disabled");
    await office.click();
    await expect(office).toHaveClass(/sel/);
    await expect(detail(page)).toContainText("192.168.1.60");
    await expect(detail(page)).toContainText("no proxy running");
    await expect(actions(page).getByRole("button", { name: "Enable" })).toBeVisible();
    await expect(actions(page).getByRole("button", { name: "Disable" })).toHaveCount(0);

    // The discovered list marks the registered hub and offers the other.
    const seen = page.locator("#seen-table tbody tr");
    await expect(seen).toHaveCount(2);
    await expect(seen.nth(0)).toContainText("registered as e26a44861b45");
    await expect(seen.nth(1).getByRole("button", { name: "Add" })).toBeVisible();
    await page.screenshot({ path: "test-results/server-panel-hubs.png", fullPage: true });
  });

  test("with nothing registered the panel says so", async ({ page }) => {
    await mockServer(page, { hubs: [], seen: [] });
    await page.goto(PAGE);
    await expect(page.locator("#hub-list")).toContainText("No hubs registered yet");
    await expect(detail(page)).toContainText("No hub selected");
    await expect(page.locator("#seen-empty")).toBeVisible();
    await expect(page.locator("#hubs-summary")).toHaveText("");
  });

  test("adds a hub by address and selects it", async ({ page }) => {
    const state = { hubs: [LIVING], seen: [] };
    const { calls } = await mockServer(page, state);
    await page.goto(PAGE);
    await expect(items(page)).toHaveCount(1);
    await page.fill("#add-host", " 192.168.1.60 ");
    await page.fill("#add-name", "Office");
    await page.click("#add-send");
    await expect.poll(() => calls.filter((c) => c.key === "POST /hubs").map((c) => c.body)).toEqual([
      { host: "192.168.1.60", name: "Office", enabled: true },
    ]);
    await expect(msg(page)).toHaveText("added 192.168.1.60");
    await expect(items(page)).toHaveCount(2);
    await expect(items(page).nth(1)).toContainText("Office");
    await expect(items(page).nth(1)).toContainText("waiting for the hub to connect");
    await expect(items(page).nth(1)).toHaveClass(/sel/);
    await expect(detail(page)).toContainText("Office");
    await expect(page.locator("#add-host")).toHaveValue("");
  });

  test("start disabled registers without a proxy", async ({ page }) => {
    const { calls } = await mockServer(page, { hubs: [], seen: [] });
    await page.goto(PAGE);
    await page.fill("#add-host", "192.168.1.60");
    await page.check("#add-disabled");
    await page.press("#add-host", "Enter");
    await expect.poll(() => calls.filter((c) => c.key === "POST /hubs").map((c) => c.body)).toEqual([
      { host: "192.168.1.60", enabled: false },
    ]);
    await expect(msg(page)).toHaveText("added 192.168.1.60 (disabled)");
    await expect(items(page).nth(0)).toContainText("disabled");
    await expect(actions(page).getByRole("button", { name: "Enable" })).toBeVisible();
  });

  test("a conflict and a failed start are shown as the problem", async ({ page }) => {
    const state = { hubs: [LIVING], seen: [] };
    await mockServer(page, state);
    await page.goto(PAGE);
    await page.fill("#add-host", "192.168.1.50");
    await page.click("#add-send");
    await expect(msg(page)).toHaveText("hub_conflict: hub already registered as e26a44861b45");
    await expect(msg(page)).toHaveClass(/msg-err/);
    await expect(items(page)).toHaveCount(1);

    state.startFails = true;
    await page.fill("#add-host", "192.168.1.61");
    await page.click("#add-send");
    await expect(msg(page)).toContainText("192.168.1.61 is registered but its proxy did not start: port 8200 in use");
    await expect(items(page)).toHaveCount(2);
    await expect(items(page).nth(1)).toContainText("not running");
    await expect(items(page).nth(1)).toHaveClass(/sel/);
    await expect(actions(page).getByRole("button", { name: "Retry start" })).toBeVisible();
  });

  test("disable, enable and remove call the lifecycle routes; remove asks first", async ({ page }) => {
    const state = { hubs: [LIVING, OFFICE], seen: [] };
    const { calls } = await mockServer(page, state);
    await page.goto(PAGE);
    await expect(items(page)).toHaveCount(2);

    await actions(page).getByRole("button", { name: "Disable" }).click();
    await expect.poll(() => calls.some((c) => c.key === "POST /hubs/e26a44861b45/disable")).toBe(true);
    await expect(msg(page)).toHaveText("e26a44861b45: disabled");
    await expect(items(page).nth(0)).toContainText("disabled");
    await expect(actions(page).getByRole("button", { name: "Enable" })).toBeVisible();

    await items(page).nth(1).click();
    await actions(page).getByRole("button", { name: "Enable" }).click();
    await expect.poll(() => calls.some((c) => c.key === "POST /hubs/192.168.1.60/enable")).toBe(true);
    await expect(msg(page)).toHaveText("192.168.1.60: enabled");
    await expect(items(page).nth(1)).toContainText("connected, in control");

    // Playwright dismisses dialogs unless a handler accepts them: the
    // first Remove is cancelled and nothing is sent.
    await actions(page).getByRole("button", { name: "Remove" }).click();
    await page.waitForTimeout(200);
    expect(calls.some((c) => c.key === "DELETE /hubs/192.168.1.60")).toBe(false);
    await expect(items(page)).toHaveCount(2);

    page.once("dialog", (dialog) => {
      expect(dialog.message()).toContain("Remove hub 192.168.1.60?");
      dialog.accept();
    });
    await actions(page).getByRole("button", { name: "Remove" }).click();
    await expect.poll(() => calls.some((c) => c.key === "DELETE /hubs/192.168.1.60")).toBe(true);
    await expect(msg(page)).toHaveText("192.168.1.60: removed");
    await expect(items(page)).toHaveCount(1);
    // The selection falls back to the hub that is left.
    await expect(items(page).nth(0)).toHaveClass(/sel/);
    await expect(detail(page)).toContainText("Living room");
  });

  test("a refused disable shows the server's reason", async ({ page }) => {
    await mockServer(page, { hubs: [LIVING], seen: [], jobRuns: true });
    await page.goto(PAGE);
    await actions(page).getByRole("button", { name: "Disable" }).click();
    await expect(msg(page)).toHaveText("e26a44861b45: hub_job_running: job j1 (restore) is running; wait for it to finish");
    await expect(actions(page).getByRole("button", { name: "Disable" })).toBeEnabled();
  });

  test("a discovered hub is added with its advertised configuration", async ({ page }) => {
    const state = { hubs: [LIVING], seen: [SEEN_NEW] };
    const { calls } = await mockServer(page, state);
    await page.goto(PAGE);
    const seen = page.locator("#seen-table tbody tr");
    await expect(seen.nth(0)).toContainText("Bedroom");
    await expect(seen.nth(0)).toContainText("present");
    await seen.nth(0).getByRole("button", { name: "Add" }).click();
    await expect.poll(() => calls.filter((c) => c.key === "POST /hubs").map((c) => c.body)).toEqual([
      { ...SEEN_NEW.config, enabled: true },
    ]);
    await expect(msg(page)).toHaveText("added cb383539684b");
    await expect(items(page)).toHaveCount(2);
    await expect(items(page).nth(1)).toContainText("Bedroom");
    await expect(seen.nth(0)).toContainText("registered as cb383539684b");
    await expect(seen.nth(0).getByRole("button", { name: "Add" })).toHaveCount(0);
  });

  test("the scan button asks the server to listen", async ({ page }) => {
    const state = { hubs: [], seen: [] };
    const { calls } = await mockServer(page, state);
    await page.goto(PAGE);
    state.seen = [SEEN_NEW];
    await page.click("#seen-scan");
    await expect.poll(() => calls.filter((c) => c.key === "POST /discovery/scan").map((c) => c.body)).toEqual([{ timeout: 5 }]);
    await expect(page.locator("#seen-table tbody tr")).toHaveCount(1);
    await expect(page.locator("#seen-note")).toHaveText("(1 present)");
  });

  test("a lifecycle event on the stream refreshes the sidebar", async ({ page }) => {
    const state = { hubs: [LIVING], seen: [] };
    const { sockets } = await mockServer(page, state);
    await page.goto(PAGE);
    await expect(items(page)).toHaveCount(1);
    await expect.poll(() => sockets.length).toBe(1);
    state.hubs.push(JSON.parse(JSON.stringify(OFFICE)));
    sockets[0].send(JSON.stringify({ type: "server_event", hub_id: OFFICE.hub_id, kind: "hub_added" }));
    await expect(items(page)).toHaveCount(2);
    await expect(page.locator("#ws-badge")).toHaveText("2");
  });
});

test.describe("control panel, views", () => {
  test("the tabs switch views, the hash follows, the theme toggle pins a palette", async ({ page }) => {
    await mockServer(page, { hubs: [LIVING], seen: [] });
    await page.goto(PAGE);
    await expect(page.locator("#view-hubs")).toBeVisible();
    await page.click('nav button[data-view="api"]');
    await expect(page.locator("#view-api")).toBeVisible();
    await expect(page.locator("#view-hubs")).toHaveCount(0);
    await expect(page).toHaveURL(/#api$/);
    await expect(page.locator("#path")).toHaveValue("/hubs/{hub_id}/status");
    await expect(page.locator("#op option")).toHaveCount(2);
    await expect(page.locator("#op option").nth(1)).toContainText("GET    /hubs/{hub_id}/status");
    await page.click('nav button[data-view="events"]');
    await expect(page.locator("#view-events")).toBeVisible();
    await expect(page.locator("#ws-list details")).toHaveCount(1);
    await expect(page.locator("#ws-list summary")).toContainText("hello v0.2.0");
    // A hash in the URL opens that view directly.
    await page.goto(`${PAGE}#remote`);
    await expect(page.locator("#view-remote")).toBeVisible();
    await expect(page.locator('nav button[data-view="remote"]')).toHaveClass(/on/);
    // Theme: auto -> light -> dark, pinned with data-theme on <html>.
    await expect(page.locator("html")).not.toHaveAttribute("data-theme", /./);
    await page.click("#theme-toggle");
    await expect(page.locator("html")).toHaveAttribute("data-theme", "light");
    await page.click("#theme-toggle");
    await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
    await page.click("#theme-toggle");
    await expect(page.locator("html")).not.toHaveAttribute("data-theme", /./);
  });

  test("the API view sends a request against the selected hub and shows the raw exchange", async ({ page }) => {
    const { calls } = await mockServer(page, { hubs: [LIVING], seen: [] });
    await page.goto(`${PAGE}#api`);
    await expect(page.locator("#path")).toHaveValue("/hubs/{hub_id}/status");
    await page.click("#send");
    await expect.poll(() => calls.some((c) => c.key === "GET /hubs/e26a44861b45/status")).toBe(true);
    await expect(page.locator("#rawreq")).toContainText("GET /sofabaton-x-server/src/sofabaton_server/api/v1/hubs/e26a44861b45/status HTTP/1.1");
    await expect(page.locator("#rawres")).toContainText("HTTP/1.1 200");
    await expect(page.locator("#rawres")).toContainText('"hub_id": "e26a44861b45"');
    await expect(page.locator("#history button")).toHaveCount(1);
  });

  test("the remote view mounts the card for the selected hub and applies a saved layout live", async ({ page }) => {
    const state = { hubs: [LIVING, OFFICE], seen: [], document: { show_dpad: false } };
    const { calls } = await mockServer(page, state);
    await page.goto(PAGE);
    await actions(page).getByRole("button", { name: "Open remote" }).click();
    await expect(page.locator("#view-remote")).toBeVisible();
    await expect(page.locator("#remote-title")).toHaveText("Living room");
    await expect(page.locator("#remote-link")).toHaveAttribute("href", /\/ui\/remote\/\?hub=e26a44861b45$/);
    await expect(page.locator("#remote-doc")).toHaveValue(/"show_dpad": false/);
    await expect(page.locator("#remote-status")).toContainText("stored document");
    // The real card is mounted over the mocked server, with the stored document applied.
    const card = page.locator("#stage sofabaton-virtual-remote");
    await expect(card).toBeVisible();
    await expect(card.locator("ha-select.sb-activity-select >> visible=true").first().locator(".value")).toHaveText("Watch TV");
    await expect(card.locator(".dpad >> visible=true")).toHaveCount(0);

    // Saving a document applies it to the mounted card at once.
    await page.fill("#remote-doc", '{"show_dpad": true}');
    await page.click("#remote-save");
    await expect.poll(() => calls.filter((c) => c.key === "PUT /hubs/e26a44861b45/ui/remote-card").map((c) => c.body)).toEqual([
      { document: { show_dpad: true } },
    ]);
    await expect(page.locator("#remote-status")).toContainText("saved");
    await expect(card.locator(".dpad .area-up >> visible=true").first()).toBeVisible();

    // A key press goes to the server through the card's backend.
    await card.locator(".dpad .area-up >> visible=true").first().click();
    await expect.poll(() => calls.filter((c) => c.key === "POST /hubs/e26a44861b45/send").map((c) => c.body)).toEqual([
      { entity_id: 101, command_id: 174 },
    ]);

    await page.click("#remote-delete");
    await expect.poll(() => calls.some((c) => c.key === "DELETE /hubs/e26a44861b45/ui/remote-card")).toBe(true);
    await expect(page.locator("#remote-status")).toContainText("reset");
    await expect(page.locator("#remote-doc")).toHaveValue("");

    // Selecting another hub in the sidebar re-targets the card and the editor.
    await items(page).nth(1).click();
    await expect(page.locator("#remote-title")).toHaveText("192.168.1.60");
    await expect(page.locator("#remote-link")).toHaveAttribute("href", /\/ui\/remote\/\?hub=192\.168\.1\.60$/);
    await expect.poll(() => calls.some((c) => c.key === "GET /hubs/192.168.1.60/ui/remote-card")).toBe(true);
    await expect(page.locator("#remote-banner")).toBeVisible();
    await page.screenshot({ path: "test-results/server-panel-remote.png", fullPage: true });
  });

  test("the catalog view lists entities with ids, shows their rows, and refreshes through a job", async ({ page }) => {
    const state = { hubs: [LIVING], seen: [] };
    const { calls } = await mockServer(page, state);
    // The catalog's own routes: the snapshot header with provenance, a
    // device's commands, and a refresh job that finishes on the third poll.
    let polls = 0;
    const snapshot = {
      snapshot_id: "abc123", captured_at: "2026-09-16T10:00:00Z", engine_generation: 3, complete: false, payload_profile: "x1s",
      devices: [{ kind: "device", device: { device_id: 1, name: "TV" }, complete: true, editable: true, fetched_at: "2026-09-16T09:00:00Z" }],
      activities: [{ kind: "activity", device: { device_id: 101, name: "Watch TV" }, complete: false, editable: false, fetched_at: null }],
    };
    await page.route(`**${API}/hubs/${LIVING.hub_id}/snapshot`, (route) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(snapshot) }));
    await page.route(`**${API}/hubs/${LIVING.hub_id}/devices/1/commands`, (route) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify([{ command_id: 1, label: "Power" }, { command_id: 17, label: "Up" }]) }));
    await page.route(`**${API}/hubs/${LIVING.hub_id}/snapshot/refresh`, (route) => {
      calls.push({ key: `POST /hubs/${LIVING.hub_id}/snapshot/refresh`, body: route.request().postDataJSON() });
      snapshot.activities[0].fetched_at = "2026-09-16T11:00:00Z";
      snapshot.activities[0].complete = true;
      route.fulfill({ status: 202, contentType: "application/json", body: JSON.stringify({ job_id: "j1", hub_id: LIVING.hub_id, kind: "refresh_entity", status: "queued", cancellable: false, created_at: "t", started_at: null, finished_at: null, progress: null, result: null, error: null }) });
    });
    await page.route(`**${API}/hubs/${LIVING.hub_id}/jobs/j1`, (route) => {
      polls++;
      const status = polls < 3 ? "running" : "done";
      route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ job_id: "j1", hub_id: LIVING.hub_id, kind: "refresh_entity", status, cancellable: false, created_at: "t", started_at: "t", finished_at: null, progress: { completed_steps: polls, total_steps: 3 }, result: null, error: null }) });
    });

    await page.goto(`${PAGE}#catalog`);
    await expect(page.locator("#catalog-status")).toContainText("partial");
    const entries = page.locator("#catalog-list .ent");
    // One device and the card fixture's two activities (101 fetched per the snapshot, 102 never).
    await expect(entries).toHaveCount(3);
    await expect(entries.nth(0)).toContainText("1");
    await expect(entries.nth(0)).toContainText("TV");
    await expect(entries.nth(0)).toContainText("ir · power off");
    await expect(entries.nth(0)).not.toContainText("Sony");
    await expect(entries.nth(0).locator(".dot")).toHaveClass(/ok/);
    await expect(entries.nth(1)).toContainText("101");
    await expect(entries.nth(1)).toContainText("Watch TV");
    await expect(entries.nth(1).locator(".dot")).toHaveClass(/off/);
    await expect(entries.nth(2)).toContainText("102");
    await expect(page.locator("#catalog-detail")).toContainText("Select a device or an activity");

    // A device: its facts and its commands with ids.
    await entries.nth(0).click();
    await expect(page.locator("#catalog-detail")).toContainText("entity id");
    await expect(page.locator("#catalog-commands tbody tr")).toHaveCount(2);
    await expect(page.locator("#catalog-commands tbody tr").nth(1)).toContainText("17");
    await expect(page.locator("#catalog-commands tbody tr").nth(1)).toContainText("Up");
    await expect(page.locator("#catalog-detail")).toContainText('"entity_id": 1');

    // An activity: buttons (bound ones counted), macros, favourites, from the card's fixtures.
    await entries.nth(1).click();
    await expect(page.locator("#catalog-buttons tbody tr")).toHaveCount(2);
    await expect(page.locator("#catalog-buttons tbody tr").nth(1)).toContainText("174");
    await expect(page.locator("#catalog-detail")).toContainText("2 bound of 2");
    await expect(page.locator("#catalog-detail")).toContainText("never (rows come from the server's cache)");
    await expect(page.locator("#catalog-detail")).not.toContainText("brand");

    // Refresh this activity: the job is followed to done, then the rows and provenance reload.
    await page.click("#catalog-refresh-entity");
    await expect.poll(() => calls.filter((c) => c.key === `POST /hubs/${LIVING.hub_id}/snapshot/refresh`).map((c) => c.body)).toEqual([{ activity_id: 101 }]);
    await expect.poll(() => polls).toBeGreaterThanOrEqual(3);
    await expect(page.locator("#catalog-refresh-entity")).toHaveText("Refresh activity");
    await expect(entries.nth(1).locator(".dot")).toHaveClass(/ok/);
    await expect(page.locator("#catalog-detail")).not.toContainText("never (rows");

    // Refresh all sends an empty scope.
    await page.click("#catalog-refresh-all");
    await expect.poll(() => calls.filter((c) => c.key === `POST /hubs/${LIVING.hub_id}/snapshot/refresh`).map((c) => c.body)).toEqual([{ activity_id: 101 }, {}]);
    await expect(page.locator("#catalog-refresh-all")).toHaveText("Refresh all");
    await page.screenshot({ path: "test-results/server-panel-catalog.png", fullPage: true });
  });
});
