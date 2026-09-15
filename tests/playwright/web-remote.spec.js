// The web remote page (docs/internal/web-remote-plan.md, R5): the built
// bundle from sofabaton_server/ui served by the fixtures server, with the
// server's REST routes mocked at the same origin and the /events stream
// mocked with routeWebSocket. Fixture bodies follow openapi.json shapes.

import { test, expect } from "@playwright/test";

const HUB = "E2:6A:44:86:1B:45";
const PAGE = "/sofabaton-x-server/src/sofabaton_server/ui/index.html";
const API = "/api/v1";

const STATUS = {
  hub_id: HUB,
  enabled: true,
  config: { host: "192.168.1.50", name: "Living room" },
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
  added_at: "2026-09-15T00:00:00Z",
  last_seen: null,
};

function makeRoutes(state) {
  return {
    "GET /hubs": () => [STATUS],
    [`GET /hubs/${HUB}/ui/remote-card`]: () => ({ hub_id: HUB, document: state.document, updated_at: null }),
    [`GET /hubs/${HUB}/status`]: () => STATUS,
    [`GET /hubs/${HUB}/activities`]: () => [
      { activity_id: 101, name: "Watch TV", active: true, needs_confirm: false },
      { activity_id: 102, name: "Listen", active: false, needs_confirm: false },
    ],
    [`GET /hubs/${HUB}/devices`]: () => [
      { device_id: 1, name: "TV", brand: "Sony", device_class: "ir", device_class_code: 1, power_state: 0, idle_behavior: 2 },
      { device_id: 2, name: "Amp", brand: "Denon", device_class: "ir", device_class_code: 1, power_state: 1, idle_behavior: null },
    ],
    [`GET /hubs/${HUB}/activity`]: () => state.running,
    [`GET /hubs/${HUB}/entities/101/buttons`]: () => [
      { button_code: 151, name: "OK", device_id: 1, command_id: 9, long_press_device_id: null, long_press_command_id: null },
      { button_code: 174, name: "UP", device_id: 1, command_id: 17, long_press_device_id: null, long_press_command_id: null },
      { button_code: 175, name: "DOWN", device_id: 1, command_id: 18, long_press_device_id: 2, long_press_command_id: 5 },
    ],
    [`GET /hubs/${HUB}/activities/101/macros`]: () => [{ command_id: 200, label: "All On" }],
    [`GET /hubs/${HUB}/activities/101/favorites`]: () => [{ device_id: 1, command_id: 1, label: "Power" }],
    [`GET /hubs/${HUB}/entities/102/buttons`]: () => [{ button_code: 151, name: "OK", device_id: 2, command_id: 3 }],
    [`GET /hubs/${HUB}/activities/102/macros`]: () => [],
    [`GET /hubs/${HUB}/activities/102/favorites`]: () => [],
    [`POST /hubs/${HUB}/send`]: () => ({ accepted: true, mode: "control" }),
    [`POST /hubs/${HUB}/activities/102/start`]: () => ({ accepted: true, mode: "control" }),
    [`POST /hubs/${HUB}/activities/101/stop`]: () => ({ accepted: true, mode: "control" }),
  };
}

async function mockServer(page, state) {
  const routes = makeRoutes(state);
  const calls = [];
  await page.route(`**${API}/**`, async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const key = `${request.method()} ${decodeURIComponent(url.pathname.slice(API.length))}`;
    const body = request.postDataJSON ? request.postDataJSON() : null;
    calls.push({ key, body });
    const handler = routes[key];
    if (!handler) {
      await route.fulfill({ status: 404, contentType: "application/json", body: JSON.stringify({ type: "not_found" }) });
      return;
    }
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(handler(body)) });
  });
  const sockets = [];
  await page.routeWebSocket(`**${API}/events**`, (ws) => {
    sockets.push(ws);
    ws.send(JSON.stringify({ type: "hello", server_version: "0.2.0", api_version: "1", hubs: [{ hub_id: HUB, enabled: true }], instance_id: "i1" }));
  });
  return { calls, sockets };
}

function card(page) {
  return page.locator("sofabaton-remote-web sofabaton-virtual-remote");
}

test.describe("web remote page", () => {
  test("without a hub parameter it lists the server's hubs", async ({ page }) => {
    await mockServer(page, { document: null, running: STATUS.status.running_activity });
    await page.goto(PAGE);
    const notice = page.locator("sofabaton-remote-web .notice");
    await expect(notice).toContainText("Open this page with ?hub=");
    await expect(notice.locator("a")).toHaveAttribute("href", `?hub=${encodeURIComponent(HUB)}`);
    await expect(notice).toContainText("Living room");
  });

  test("an unknown hub id names the known ones", async ({ page }) => {
    await mockServer(page, { document: null, running: null });
    await page.goto(`${PAGE}?hub=nope`);
    await expect(page.locator("sofabaton-remote-web .notice")).toContainText("No hub with id nope");
  });

  test("renders the card from the server, sends keys, follows the stream", async ({ page }) => {
    const state = { document: { show_dvr: false }, running: STATUS.status.running_activity };
    const { calls, sockets } = await mockServer(page, state);
    await page.goto(`${PAGE}?hub=${encodeURIComponent(HUB)}`);
    const remote = card(page);
    await expect(remote).toBeVisible();
    await expect(page.locator("sofabaton-remote-web .foot")).toContainText("Living room");

    // The activity row shows the running activity through the shimmed ha-select.
    // The card keeps a second, hidden activity row for layout transitions.
    const select = remote.locator("ha-select.sb-activity-select >> visible=true").first();
    await expect(select).toBeVisible();
    await expect(select.locator(".value")).toHaveText("Watch TV");
    // The mdi shim renders SVG paths for the card's own icons.
    await expect(remote.locator("ha-icon svg path").first()).toHaveAttribute("d", /^M/);

    // A key press goes to POST /send in the running activity's scope
    // (the dpad UP key is command 174 on the activity page).
    await remote.locator(".dpad .area-up >> visible=true").first().click();
    await expect.poll(() => calls.filter((c) => c.key === `POST /hubs/${HUB}/send`).map((c) => c.body)).toEqual([
      { entity_id: 101, command_id: 174 },
    ]);

    // The stream moves the running activity; the select follows.
    await expect.poll(() => sockets.length).toBe(1);
    state.running = { activity_id: 102, name: "Listen" };
    sockets[0].send(JSON.stringify({
      type: "hub_event",
      hub_id: HUB,
      event: { seq: 2, kind: "activity_changed", payload: { activity_id: 102, previous_activity_id: 101, name: "Listen" } },
    }));
    await expect(select.locator(".value")).toHaveText("Listen");

    // Choosing an activity from the shim's menu starts it on the server.
    await select.locator(".trigger").click();
    // The card clips the select host (overflow: hidden); the menu must
    // float clear of it or it is painted nowhere (IntersectionObserver
    // honours ancestor clipping, a plain visibility check does not).
    const option = select.locator(".option", { hasText: "Watch TV" });
    await expect(option).toBeInViewport({ ratio: 1 });
    await option.click();
    await expect.poll(() => calls.some((c) => c.key === `POST /hubs/${HUB}/activities/101/start`)).toBe(true);
    // A reference capture of the page for review (not a baseline).
    await page.screenshot({ path: "test-results/web-remote-page.png", fullPage: true });
  });

  test("the select's menu lines up under the trigger when the page is zoomed", async ({ page }) => {
    await mockServer(page, { document: null, running: STATUS.status.running_activity });
    await page.goto(`${PAGE}?hub=${encodeURIComponent(HUB)}&zoom=1.5`);
    const select = card(page).locator("ha-select.sb-activity-select >> visible=true").first();
    await expect(select).toBeVisible();
    await select.locator(".trigger").click();
    const option = select.locator(".option", { hasText: "Listen" });
    await expect(option).toBeInViewport({ ratio: 1 });
    // The menu is fixed-positioned and placed by measurement, so a zoomed
    // ancestor must not skew it: same left edge and width as the trigger,
    // hanging just below it.
    const trigger = await select.locator(".trigger").boundingBox();
    const menu = await select.locator(".menu").boundingBox();
    expect(Math.abs(menu.x - trigger.x)).toBeLessThan(2);
    expect(Math.abs(menu.width - trigger.width)).toBeLessThan(2);
    expect(menu.y - (trigger.y + trigger.height)).toBeGreaterThan(2);
    expect(menu.y - (trigger.y + trigger.height)).toBeLessThan(12);
  });

  test("a stored background override paints the card without Home Assistant", async ({ page }) => {
    await mockServer(page, {
      document: { use_background_override: true, background_override: [20, 20, 20] },
      running: STATUS.status.running_activity,
    });
    await page.goto(`${PAGE}?hub=${encodeURIComponent(HUB)}`);
    const remote = card(page);
    await expect(remote).toBeVisible();
    await expect.poll(async () =>
      remote.evaluate((el) => {
        const root = el.shadowRoot?.querySelector("ha-card");
        return root ? getComputedStyle(root).backgroundColor : null;
      }),
    ).toBe("rgb(20, 20, 20)");
  });

  test("the hub id is matched the way the server spells it", async ({ page }) => {
    const { calls } = await mockServer(page, { document: null, running: STATUS.status.running_activity });
    // The mock lists the hub in colon form; the URL uses the compact form.
    await page.goto(`${PAGE}?hub=e26a44861b45`);
    await expect(card(page)).toBeVisible();
    await expect.poll(() => calls.some((c) => c.key === `GET /hubs/${HUB}/status`)).toBe(true);
  });

  test("the hub going away shows the banner and dark theme applies", async ({ page }) => {
    const state = { document: null, running: STATUS.status.running_activity };
    const { sockets } = await mockServer(page, state);
    await page.goto(`${PAGE}?hub=${encodeURIComponent(HUB)}&theme=dark`);
    await expect(card(page)).toBeVisible();
    await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
    const bg = await page.evaluate(() => getComputedStyle(document.documentElement).getPropertyValue("--card-background-color").trim());
    expect(bg).toBe("#1c1c1c");

    await expect.poll(() => sockets.length).toBe(1);
    STATUS.status.controllable = false;
    STATUS.status.mode = "observe";
    sockets[0].send(JSON.stringify({ type: "hub_event", hub_id: HUB, event: { seq: 3, kind: "status_changed", payload: { mode: "observe", previous_mode: "control" } } }));
    await expect(page.locator("sofabaton-remote-web .banner")).toBeVisible();
    await expect(page.locator("sofabaton-remote-web .banner")).toContainText("not controllable");
    STATUS.status.controllable = true;
    STATUS.status.mode = "control";
  });
});
