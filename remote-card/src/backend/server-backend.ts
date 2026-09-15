// RemoteBackend over sofabaton-x-server's REST + WebSocket API
// (docs/internal/web-remote-plan.md, section 4.2, R3). It produces the same
// entity attribute contract the HA remote entity publishes, so the store
// and every pure derivation behind it run unchanged on the web remote.
//
// Data flow: one initial load (status, activities, devices, running
// activity, then the running activity's buttons / macros / favorites),
// then the `/events` stream keeps it current: `activity_changed` moves the
// running activity and fetches that activity's pages on first sight,
// `snapshot_changed` re-reads the catalog and drops the pages it names,
// connection and status events re-read `/status`, `catalog_ready` and a
// `dropped` notice reload everything. Per-entity pages (buttons, macros,
// favorites, device keymaps) are fetched once and kept, like the HA
// attribute caches; the store never invalidates them either.

import type {
  DeviceKeymapResponse,
  RemoteEntityAttributes,
} from "../remote-card-types";
import type {
  RemoteActivityRef,
  RemoteBackend,
  RemoteIntegration,
  RemoteSnapshot,
} from "./remote-backend";

export const SERVER_API_PREFIX = "/api/v1";

// ---------- server wire shapes (openapi.json components) ----------

interface ServerRunningActivity {
  activity_id: number;
  name: string | null;
}

interface ServerHubStatus {
  hub_connected: boolean;
  app_connected: boolean;
  controllable: boolean;
  mode: "disconnected" | "observe" | "control";
  hub_version: string | null;
  running_activity: ServerRunningActivity | null;
  catalog_ready?: boolean;
}

interface ServerHubStatusView {
  hub_id: string;
  enabled: boolean;
  status: ServerHubStatus | null;
}

interface ServerActivity {
  activity_id: number;
  name: string;
  active: boolean;
}

interface ServerDevice {
  device_id: number;
  name: string;
  device_class: string | null;
  power_state: number | null;
  idle_behavior: number | null;
}

interface ServerCommand {
  command_id: number;
  label: string;
}

interface ServerButton {
  button_code: number;
  name: string | null;
  device_id: number | null;
  command_id: number | null;
  long_press_device_id?: number | null;
  long_press_command_id?: number | null;
}

interface ServerMacro {
  command_id: number;
  label: string | null;
}

interface ServerFavorite {
  device_id: number;
  command_id: number;
  label: string | null;
}

interface ServerHubEvent {
  seq: number;
  kind: string;
  payload: Record<string, unknown> | null;
}

interface ServerWsMessage {
  type: string;
  hub_id?: string;
  kind?: string;
  event?: ServerHubEvent;
  count?: number;
}

// ---------- minimal WebSocket surface (injectable for tests) ----------

export interface WebSocketLike {
  onopen: ((event: unknown) => void) | null;
  onmessage: ((event: { data: unknown }) => void) | null;
  onclose: ((event: unknown) => void) | null;
  onerror: ((event: unknown) => void) | null;
  close(): void;
}

export type WebSocketFactory = (url: string) => WebSocketLike;

export interface ServerRemoteBackendOptions {
  /** Origin of the server, "" for same-origin (the page's default). */
  baseUrl?: string;
  fetch?: typeof fetch;
  webSocket?: WebSocketFactory;
  /** First reconnect delay; doubles up to 30 s. */
  reconnectDelayMs?: number;
}

interface ActivityPages {
  buttons: ServerButton[];
  macros: ServerMacro[];
  favorites: ServerFavorite[];
}

const MAX_RECONNECT_DELAY_MS = 30000;

function toNumber(value: unknown): number | null {
  if (value == null || value === "") return null;
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
}

function longPressPairs(
  buttons: ServerButton[],
): Record<string, { device_id: number; command_id: number }> {
  const out: Record<string, { device_id: number; command_id: number }> = {};
  for (const button of buttons) {
    const device = toNumber(button.long_press_device_id);
    const command = toNumber(button.long_press_command_id);
    // remote.py's rule: a pair needs a (truthy) device and a command.
    if (device && command != null) {
      out[String(button.button_code)] = { device_id: device, command_id: command };
    }
  }
  return out;
}

export class ServerRemoteBackend implements RemoteBackend {
  readonly kind = "server" as const;

  private readonly baseUrl: string;
  private readonly fetchImpl: typeof fetch;
  private readonly wsFactory: WebSocketFactory | null;
  private readonly reconnectDelayMs: number;

  private hubId = "";
  private listeners: Array<() => void> = [];

  // Server-side state, in wire shapes
  private hubStatus: ServerHubStatusView | null = null;
  private activities: ServerActivity[] = [];
  private devices: ServerDevice[] = [];
  private running: ServerRunningActivity | null = null;
  private activityPages: Record<string, ActivityPages> = {};
  private devicePages: Record<string, { buttons: ServerButton[]; commands: ServerCommand[] }> = {};
  private loaded = false;
  private loadPromise: Promise<void> | null = null;
  private pagePromises: Record<string, Promise<void>> = {};
  private _lastError: string | null = null;

  // Snapshot cache: rebuilt lazily, invalidated on every mutation
  private snapshotCache: RemoteSnapshot | undefined | null = null;

  // Stream
  private socket: WebSocketLike | null = null;
  private socketGeneration = 0;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private reconnectDelay: number;
  private streaming = false;

  constructor(options: ServerRemoteBackendOptions = {}) {
    this.baseUrl = String(options.baseUrl ?? "").replace(/\/+$/, "");
    this.fetchImpl =
      options.fetch ??
      ((input, init) => globalThis.fetch(input, init));
    this.wsFactory =
      options.webSocket ??
      (typeof WebSocket === "function"
        ? (url) => new WebSocket(url) as unknown as WebSocketLike
        : null);
    this.reconnectDelayMs = Math.max(100, options.reconnectDelayMs ?? 1000);
    this.reconnectDelay = this.reconnectDelayMs;
  }

  // ---------- RemoteBackend ----------

  get target(): string {
    return this.hubId;
  }

  /** The last failed request or stream error, for the page to show. */
  get lastError(): string | null {
    return this._lastError;
  }

  setTarget(target: string): void {
    const next = String(target ?? "");
    if (next === this.hubId) return;
    this.hubId = next;
    this.resetState();
    this.closeSocket();
    if (this.listeners.length) this.start();
  }

  snapshot(): RemoteSnapshot | undefined {
    if (!this.hubId) return undefined;
    if (this.snapshotCache === null) this.snapshotCache = this.buildSnapshot();
    return this.snapshotCache;
  }

  subscribe(listener: () => void): () => void {
    this.listeners.push(listener);
    if (this.listeners.length === 1) this.start();
    return () => {
      this.listeners = this.listeners.filter((entry) => entry !== listener);
      if (!this.listeners.length) this.stop();
    };
  }

  async probeIntegration(): Promise<RemoteIntegration> {
    if (!this.hubId) throw new Error("no hub selected");
    await this.ensureLoaded();
    if (!this.hubStatus) throw new Error(this._lastError ?? "hub status unavailable");
    return "x1s";
  }

  async devicePowerState(deviceId: number): Promise<0 | 1 | null> {
    try {
      const response = await this.get<{ power_state: number | null }>(
        `/devices/${deviceId}/power-state`,
      );
      const raw = response?.power_state;
      return raw === 1 ? 1 : raw === 0 ? 0 : null;
    } catch (_err) {
      return null;
    }
  }

  async deviceKeymap(deviceId: number): Promise<DeviceKeymapResponse | null> {
    if (!this.hubId) return null;
    await this.ensureLoaded();
    const device = this.devices.find((entry) => entry.device_id === deviceId);
    if (!device) return { keymap: null, reason: "cache_miss" };
    const key = String(deviceId);
    if (!this.devicePages[key]) {
      const [buttons, commands] = await Promise.all([
        this.get<ServerButton[]>(`/entities/${deviceId}/buttons`),
        this.get<ServerCommand[]>(`/devices/${deviceId}/commands`),
      ]);
      this.devicePages[key] = { buttons, commands };
      this.invalidate();
      this.notify();
    }
    const page = this.devicePages[key];
    return {
      keymap: {
        device: {
          device_id: device.device_id,
          name: device.name,
          device_class: device.device_class ?? undefined,
        },
        buttons: page.buttons.map((button) => button.button_code),
        bindings: page.buttons
          .filter((button) => button.command_id != null)
          .map((button) => ({
            button_id: button.button_code,
            button_name: button.name,
            command_id: Number(button.command_id),
            long_press_command_id: button.long_press_command_id ?? null,
          })),
        commands: page.commands.map((command) => ({
          command_id: command.command_id,
          name: command.label,
        })),
        // Same gate as the HA projection: idle-behavior byte 1..3.
        power_configured:
          device.idle_behavior != null && [1, 2, 3].includes(Number(device.idle_behavior)),
      },
    };
  }

  async sendCommand(commandId: unknown, scopeId: unknown): Promise<void> {
    const command = toNumber(commandId);
    if (command == null) return;
    // HA coerced a missing scope to device 0; the server needs a real
    // entity, so a missing scope means the running activity.
    let scope = toNumber(scopeId);
    if (!scope) scope = this.running?.activity_id ?? null;
    if (scope == null) return;
    await this.post(`/send`, { entity_id: scope, command_id: command });
  }

  async startActivity(activity: RemoteActivityRef): Promise<void> {
    const id =
      activity.id ??
      this.activities.find((entry) => entry.name === activity.name)?.activity_id ??
      null;
    if (id == null) return;
    await this.post(`/activities/${id}/start`);
  }

  async stopActivity(): Promise<void> {
    const id = this.running?.activity_id;
    if (id == null) return;
    await this.post(`/activities/${id}/stop`);
  }

  // ---------- lifecycle ----------

  /** Begin loading and streaming; idempotent. subscribe() calls it. */
  start(): void {
    if (!this.hubId) return;
    void this.ensureLoaded();
    this.openSocket();
  }

  stop(): void {
    this.closeSocket();
  }

  private resetState(): void {
    this.hubStatus = null;
    this.activities = [];
    this.devices = [];
    this.running = null;
    this.activityPages = {};
    this.devicePages = {};
    this.loaded = false;
    this.loadPromise = null;
    this.pagePromises = {};
    this._lastError = null;
    this.invalidate();
  }

  private invalidate(): void {
    this.snapshotCache = null;
  }

  private notify(): void {
    for (const listener of [...this.listeners]) listener();
  }

  // ---------- HTTP ----------

  private url(path: string): string {
    return `${this.baseUrl}${SERVER_API_PREFIX}/hubs/${encodeURIComponent(this.hubId)}${path}`;
  }

  private async get<T>(path: string): Promise<T> {
    const response = await this.fetchImpl(this.url(path), {
      headers: { accept: "application/json" },
    });
    if (!response.ok) throw new Error(`GET ${path} -> ${response.status}`);
    return (await response.json()) as T;
  }

  private async post(path: string, body?: Record<string, unknown>): Promise<void> {
    const response = await this.fetchImpl(this.url(path), {
      method: "POST",
      headers: body
        ? { accept: "application/json", "content-type": "application/json" }
        : { accept: "application/json" },
      body: body ? JSON.stringify(body) : undefined,
    });
    if (!response.ok) throw new Error(`POST ${path} -> ${response.status}`);
  }

  // ---------- loading ----------

  private ensureLoaded(): Promise<void> {
    if (this.loaded) return Promise.resolve();
    if (!this.loadPromise) {
      this.loadPromise = this.loadAll().finally(() => {
        this.loadPromise = null;
      });
    }
    return this.loadPromise;
  }

  /** Full reload: status, catalog, running activity, then its pages. */
  private async loadAll(): Promise<void> {
    if (!this.hubId) return;
    const hubId = this.hubId;
    try {
      const [status, activities, devices, running] = await Promise.all([
        this.get<ServerHubStatusView>(`/status`),
        this.get<ServerActivity[]>(`/activities`),
        this.get<ServerDevice[]>(`/devices`),
        this.get<ServerRunningActivity | null>(`/activity`),
      ]);
      if (hubId !== this.hubId) return; // target moved during the load
      this.hubStatus = status;
      this.activities = activities;
      this.devices = devices;
      this.running = running;
      this.loaded = true;
      this._lastError = null;
    } catch (err) {
      if (hubId !== this.hubId) return;
      this._lastError = err instanceof Error ? err.message : String(err);
      this.hubStatus = null;
      this.loaded = false;
    }
    this.invalidate();
    this.notify();
    if (this.running) await this.ensureActivityPages(this.running.activity_id);
  }

  private async refreshStatus(): Promise<void> {
    try {
      const [status, running] = await Promise.all([
        this.get<ServerHubStatusView>(`/status`),
        this.get<ServerRunningActivity | null>(`/activity`),
      ]);
      this.hubStatus = status;
      this.running = running;
      this._lastError = null;
    } catch (err) {
      this._lastError = err instanceof Error ? err.message : String(err);
      this.hubStatus = null;
    }
    this.invalidate();
    this.notify();
  }

  private ensureActivityPages(activityId: number): Promise<void> {
    const key = String(activityId);
    if (this.activityPages[key]) return Promise.resolve();
    if (!this.pagePromises[key]) {
      this.pagePromises[key] = this.loadActivityPages(activityId).finally(() => {
        delete this.pagePromises[key];
      });
    }
    return this.pagePromises[key];
  }

  private async loadActivityPages(activityId: number): Promise<void> {
    const hubId = this.hubId;
    try {
      const [buttons, macros, favorites] = await Promise.all([
        this.get<ServerButton[]>(`/entities/${activityId}/buttons`),
        this.get<ServerMacro[]>(`/activities/${activityId}/macros`),
        this.get<ServerFavorite[]>(`/activities/${activityId}/favorites`),
      ]);
      if (hubId !== this.hubId) return;
      this.activityPages[String(activityId)] = { buttons, macros, favorites };
    } catch (err) {
      if (hubId !== this.hubId) return;
      this._lastError = err instanceof Error ? err.message : String(err);
      return;
    }
    this.invalidate();
    this.notify();
  }

  // ---------- stream ----------

  private wsUrl(): string {
    let origin = this.baseUrl;
    if (!origin && typeof location !== "undefined") origin = location.origin;
    const ws = origin.replace(/^http/, "ws");
    return `${ws}${SERVER_API_PREFIX}/events?hub_id=${encodeURIComponent(this.hubId)}`;
  }

  private openSocket(): void {
    if (!this.wsFactory || !this.hubId || this.socket) return;
    this.streaming = true;
    const generation = ++this.socketGeneration;
    let socket: WebSocketLike;
    try {
      socket = this.wsFactory(this.wsUrl());
    } catch (err) {
      this._lastError = err instanceof Error ? err.message : String(err);
      this.scheduleReconnect();
      return;
    }
    this.socket = socket;
    socket.onopen = () => {
      if (generation !== this.socketGeneration) return;
      this.reconnectDelay = this.reconnectDelayMs;
      // Anything that happened while we were away is unknown: reload.
      if (this.loaded) {
        this.loaded = false;
        void this.ensureLoaded();
      }
    };
    socket.onmessage = (event) => {
      if (generation !== this.socketGeneration) return;
      this.handleMessage(event.data);
    };
    socket.onerror = () => {
      /* onclose follows; nothing to do here */
    };
    socket.onclose = () => {
      if (generation !== this.socketGeneration) return;
      this.socket = null;
      if (this.streaming) this.scheduleReconnect();
    };
  }

  private closeSocket(): void {
    this.streaming = false;
    this.socketGeneration += 1;
    if (this.reconnectTimer) clearTimeout(this.reconnectTimer);
    this.reconnectTimer = null;
    const socket = this.socket;
    this.socket = null;
    if (socket) {
      try {
        socket.close();
      } catch (_err) {
        /* already closed */
      }
    }
  }

  private scheduleReconnect(): void {
    if (!this.streaming || this.reconnectTimer) return;
    const delay = this.reconnectDelay;
    this.reconnectDelay = Math.min(this.reconnectDelay * 2, MAX_RECONNECT_DELAY_MS);
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null;
      if (this.streaming) this.openSocket();
    }, delay);
  }

  /** Exposed for tests and the page host; routes one stream message. */
  handleMessage(raw: unknown): void {
    let message: ServerWsMessage;
    try {
      message = (typeof raw === "string" ? JSON.parse(raw) : raw) as ServerWsMessage;
    } catch (_err) {
      return;
    }
    if (!message || typeof message !== "object") return;
    switch (message.type) {
      case "hello":
        return;
      case "dropped":
        this.loaded = false;
        void this.ensureLoaded();
        return;
      case "server_event":
        if (message.hub_id !== this.hubId) return;
        if (message.kind === "hub_removed") {
          this.hubStatus = null;
          this.invalidate();
          this.notify();
        } else {
          void this.refreshStatus();
        }
        return;
      case "hub_event":
        if (message.hub_id !== this.hubId || !message.event) return;
        this.handleHubEvent(message.event);
        return;
      default:
        return;
    }
  }

  private handleHubEvent(event: ServerHubEvent): void {
    const payload = event.payload ?? {};
    switch (event.kind) {
      case "activity_changed": {
        const id = toNumber(payload.activity_id);
        this.running =
          id == null
            ? null
            : { activity_id: id, name: (payload.name as string | null) ?? null };
        if (this.hubStatus?.status) {
          this.hubStatus = {
            ...this.hubStatus,
            status: { ...this.hubStatus.status, running_activity: this.running },
          };
        }
        this.invalidate();
        this.notify();
        if (id != null) void this.ensureActivityPages(id);
        return;
      }
      case "hub_state":
      case "app_state":
      case "status_changed":
        void this.refreshStatus();
        return;
      case "catalog_ready":
        if (payload.ready) {
          this.loaded = false;
          void this.ensureLoaded();
        } else {
          void this.refreshStatus();
        }
        return;
      case "snapshot_changed": {
        const deviceIds = Array.isArray(payload.device_ids) ? payload.device_ids : [];
        const activityIds = Array.isArray(payload.activity_ids) ? payload.activity_ids : [];
        if (!deviceIds.length && !activityIds.length) {
          this.activityPages = {};
          this.devicePages = {};
        } else {
          for (const id of deviceIds) delete this.devicePages[String(id)];
          for (const id of activityIds) delete this.activityPages[String(id)];
          // A device edit changes the bindings on every page that maps to
          // it; the per-activity pages are cheap, drop them all.
          if (deviceIds.length) this.activityPages = {};
        }
        this.loaded = false;
        void this.ensureLoaded();
        return;
      }
      default:
        return;
    }
  }

  // ---------- the attribute contract ----------

  private buildSnapshot(): RemoteSnapshot | undefined {
    const status = this.hubStatus?.status ?? null;
    const enabled = this.hubStatus?.enabled ?? false;
    const available = Boolean(this.hubStatus && enabled && status?.controllable);
    const runningId = this.running?.activity_id ?? null;

    const activities = this.activities.map((activity) => ({
      id: activity.activity_id,
      name: activity.name,
      state: activity.activity_id === runningId ? "on" : "off",
    }));
    const devices = this.devices.map((device) => ({
      id: device.device_id,
      name: device.name,
      device_class: device.device_class ?? undefined,
    }));

    const assignedKeys: Record<string, number[]> = {};
    const macroKeys: Record<string, Array<{ id: number; name: string }>> = {};
    const favoriteKeys: Record<string, Array<{ id: number; name: string; device_id: number }>> = {};
    const longPressKeys: Record<string, Record<string, { device_id: number; command_id: number }>> = {};

    for (const [key, page] of Object.entries(this.activityPages)) {
      assignedKeys[key] = page.buttons.map((button) => button.button_code);
      macroKeys[key] = page.macros.map((macro) => ({
        id: macro.command_id,
        name: macro.label ?? "",
      }));
      favoriteKeys[key] = page.favorites.map((favorite) => ({
        id: favorite.command_id,
        name: favorite.label ?? "",
        device_id: favorite.device_id,
      }));
      const pairs = longPressPairs(page.buttons);
      if (Object.keys(pairs).length) longPressKeys[key] = pairs;
    }
    for (const [key, page] of Object.entries(this.devicePages)) {
      const pairs = longPressPairs(page.buttons);
      if (Object.keys(pairs).length) longPressKeys[key] = pairs;
    }

    const currentName =
      this.running?.name ??
      activities.find((activity) => activity.id === runningId)?.name ??
      undefined;

    const attributes: RemoteEntityAttributes & Record<string, unknown> = {
      hub_version: String(status?.hub_version ?? "").toUpperCase(),
      current_activity: available ? currentName : undefined,
      current_activity_id: available ? runningId : null,
      load_state: this.loaded ? "ready" : "loading",
      activities,
      devices,
      assigned_keys: assignedKeys,
      macro_keys: macroKeys,
      favorite_keys: favoriteKeys,
      long_press_keys: longPressKeys,
      hub_id: this.hubId,
    };

    return {
      state: !available ? "unavailable" : runningId != null ? "on" : "off",
      attributes,
    };
  }
}
