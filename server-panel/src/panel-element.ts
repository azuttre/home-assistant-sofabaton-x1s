// The control panel's shell (docs/internal/server-panel-plan.md, section
// 4): top bar, hub sidebar, view tabs. It owns the API client, the event
// stream, the hub list and the selection, and hands each view what it
// needs. Views report back with events (sb-message, sb-hubs-changed,
// sb-select-hub, sb-open-view).

import { LitElement, html, css, type TemplateResult } from "lit";

import { PanelApi, serverBaseFromPanelUrl, type HubView, type Operation, type SeenHub, type ServerInfo } from "./panel-api";
import {
  hubDisplayName,
  hubState,
  isView,
  loadPrefs,
  nextTheme,
  savePrefs,
  viewFromHash,
  VIEWS,
  type PanelPrefs,
  type ThemeChoice,
  type ViewName,
} from "./panel-state";
import { PanelStream, isHubRefreshTrigger } from "./panel-stream";
import { PANEL_BASE_CSS } from "./panel-styles";
import type { SbPanelHubs } from "./views/hubs-view";

export const PANEL_TAG = "sofabaton-server-panel";

const VIEW_LABELS: Record<ViewName, string> = { hubs: "Hubs", catalog: "Catalog", remote: "Remote", api: "API", events: "Events" };
const REFRESH_TICK_MS = 5000;
const REFRESH_DEBOUNCE_MS = 300;

export class SofabatonServerPanel extends LitElement {
  static properties = {
    _hubs: { state: true },
    _seen: { state: true },
    _selected: { state: true },
    _view: { state: true },
    _server: { state: true },
    _serverError: { state: true },
    _streamOn: { state: true },
    _messageCount: { state: true },
    _message: { state: true },
    _theme: { state: true },
    _operations: { state: true },
    _listUnavailable: { state: true },
  };

  static styles = [
    PANEL_BASE_CSS,
    css`
      :host { display: flex; flex-direction: column; height: 100%; background: var(--sbp-bg); }
      header { display: flex; align-items: center; gap: 14px; padding: 10px 18px; border-bottom: 1px solid var(--sbp-line); background: var(--sbp-panel); }
      header .brand { display: flex; align-items: baseline; gap: 8px; }
      header .brand b { font-size: 16px; font-weight: 650; letter-spacing: 0.01em; }
      header .brand span { color: var(--sbp-muted); font-size: 12px; }
      header .meta { color: var(--sbp-muted); font-size: 12px; margin-left: auto; display: flex; align-items: center; gap: 12px; }
      header .meta .stream { display: inline-flex; align-items: center; gap: 5px; }
      header button.theme { font-size: 12px; padding: 3px 9px; }
      .shell { flex: 1; display: grid; grid-template-columns: 260px minmax(0, 1fr); min-height: 0; }
      aside { border-right: 1px solid var(--sbp-line); background: var(--sbp-panel); display: flex; flex-direction: column; min-height: 0; }
      aside h3 { margin: 0; padding: 12px 14px 6px; font-size: 11px; text-transform: uppercase; letter-spacing: 0.06em; color: var(--sbp-muted); display: flex; align-items: center; gap: 8px; }
      .hub-list { overflow: auto; min-height: 0; padding: 0 8px; }
      .hub-item { display: grid; grid-template-columns: auto 1fr; gap: 4px 10px; align-items: center; padding: 8px 10px; border-radius: var(--sbp-radius); cursor: pointer; border: 1px solid transparent; }
      .hub-item:hover { background: var(--sbp-panel-2); }
      .hub-item.sel { background: rgba(var(--sbp-accent-rgb), 0.12); border-color: rgba(var(--sbp-accent-rgb), 0.35); }
      .hub-item .name { font-weight: 600; font-size: 13px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
      .hub-item .sub { grid-column: 2; color: var(--sbp-muted); font-size: 11px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
      .hub-list .hint { padding: 6px 12px; }
      aside .foot { margin-top: auto; padding: 10px; border-top: 1px solid var(--sbp-line); }
      aside .foot button { width: 100%; }
      .main { display: flex; flex-direction: column; min-height: 0; min-width: 0; }
      nav { display: flex; gap: 2px; padding: 8px 14px 0; border-bottom: 1px solid var(--sbp-line); background: var(--sbp-panel); }
      nav button { background: transparent; border: 0; border-bottom: 2px solid transparent; border-radius: 0; color: var(--sbp-muted); padding: 8px 12px; font-weight: 500; }
      nav button:hover { color: var(--sbp-text); }
      nav button.on { color: var(--sbp-text); border-bottom-color: var(--sbp-accent); }
      nav .badge { display: inline-block; min-width: 18px; padding: 0 5px; border-radius: 9px; background: var(--sbp-panel-2); color: var(--sbp-muted); font-size: 11px; margin-left: 6px; text-align: center; }
      nav .note { align-self: center; font-size: 12px; padding-right: 4px; }
      .view { flex: 1; min-height: 0; overflow: auto; padding: 16px; }
      @media (max-width: 960px) {
        .shell { grid-template-columns: 1fr; grid-template-rows: auto minmax(0, 1fr); }
        aside { border-right: 0; border-bottom: 1px solid var(--sbp-line); max-height: 40vh; }
        header .meta .server { display: none; }
      }
    `,
  ];

  readonly api: PanelApi;
  readonly stream: PanelStream;
  private _hubs: HubView[] = [];
  private _seen: SeenHub[] = [];
  private _selected: string | null = null;
  private _view: ViewName = "hubs";
  private _server: ServerInfo | null = null;
  private _serverError: string | null = null;
  private _streamOn = false;
  private _messageCount = 0;
  private _message: { text: string; ok: boolean } | null = null;
  private _theme: ThemeChoice = "auto";
  private _operations: Operation[] = [];
  private _listUnavailable = false;
  private _hubsKey = "";
  private _storage: Storage | null = null;
  private _tickTimer: ReturnType<typeof setInterval> | null = null;
  private _debounceTimer: ReturnType<typeof setTimeout> | null = null;
  private _offStream: (() => void)[] = [];
  private readonly _onHashChange = () => this._setView(viewFromHash(location.hash, this._view), { fromHash: true });

  constructor() {
    super();
    this.api = new PanelApi(serverBaseFromPanelUrl(location.href));
    this.stream = new PanelStream({ apiRoot: this.api.apiRoot });
  }

  connectedCallback(): void {
    super.connectedCallback();
    try {
      this._storage = window.localStorage;
    } catch {
      this._storage = null;
    }
    const prefs = loadPrefs(this._storage);
    this._selected = prefs.hub;
    this._theme = prefs.theme;
    this._applyTheme();
    this._view = location.hash ? viewFromHash(location.hash, prefs.view) : prefs.view;
    window.addEventListener("hashchange", this._onHashChange);
    this._offStream = [
      this.stream.onState((connected) => {
        this._streamOn = connected;
      }),
      this.stream.onMessage((message) => {
        this._messageCount = this.stream.messages.length;
        if (isHubRefreshTrigger(message.data)) this._refreshSoon();
      }),
    ];
    this.stream.start();
    void this._loadServer();
    void this._loadHubs();
    void this._loadSeen();
    void this.api.operations().then((ops) => {
      this._operations = ops;
    });
    this._tickTimer = setInterval(() => {
      if (document.visibilityState !== "visible") return;
      void this._loadHubs();
      if (this._view === "hubs") void this._loadSeen();
    }, REFRESH_TICK_MS);
  }

  disconnectedCallback(): void {
    super.disconnectedCallback();
    window.removeEventListener("hashchange", this._onHashChange);
    for (const off of this._offStream) off();
    this._offStream = [];
    this.stream.stop();
    if (this._tickTimer !== null) clearInterval(this._tickTimer);
    this._tickTimer = null;
    if (this._debounceTimer !== null) clearTimeout(this._debounceTimer);
    this._debounceTimer = null;
  }

  // -- state --------------------------------------------------------------------

  get selectedHub(): HubView | null {
    return this._hubs.find((h) => h.hub_id === this._selected) ?? null;
  }

  private _savePrefs(): void {
    const prefs: PanelPrefs = { hub: this._selected, view: this._view, theme: this._theme };
    savePrefs(this._storage, prefs);
  }

  private _setView(view: ViewName, { fromHash = false } = {}): void {
    if (!isView(view)) view = "hubs";
    this._view = view;
    this._savePrefs();
    if (!fromHash && location.hash !== `#${view}`) history.replaceState(null, "", `#${view}`);
  }

  private _select(hubId: string | null): void {
    this._selected = hubId;
    this._savePrefs();
  }

  private _cycleTheme(): void {
    this._theme = nextTheme(this._theme);
    this._applyTheme();
    this._savePrefs();
  }

  private _applyTheme(): void {
    // The palette pins light or dark with data-theme on <html>; "auto" removes it.
    if (this._theme === "auto") delete document.documentElement.dataset.theme;
    else document.documentElement.dataset.theme = this._theme;
  }

  private _say(text: string, ok = true): void {
    this._message = { text, ok };
  }

  // -- loading --------------------------------------------------------------------

  private async _loadServer(): Promise<void> {
    try {
      const response = await this.api.serverInfo();
      if (response.ok && response.body) {
        this._server = response.body;
        this._serverError = null;
      } else {
        this._serverError = `server answered ${response.status}`;
      }
    } catch (err) {
      this._serverError = `server unreachable: ${String(err)}`;
    }
  }

  private async _loadHubs(): Promise<void> {
    let hubs: HubView[] | null = null;
    try {
      const response = await this.api.listHubs();
      hubs = response.ok && Array.isArray(response.body) ? response.body : null;
    } catch {
      hubs = null;
    }
    if (!hubs) {
      this._listUnavailable = true;
      return;
    }
    this._listUnavailable = false;
    // Re-assign only when something changed: an identical list must not
    // re-render the sidebar under a click.
    const key = JSON.stringify(hubs);
    if (key !== this._hubsKey) {
      this._hubsKey = key;
      this._hubs = hubs;
    }
    // The selection follows a re-key (host id to MAC) and a removal.
    if (hubs.length && !hubs.some((h) => h.hub_id === this._selected)) this._select(hubs[0].hub_id);
    if (!hubs.length && this._selected !== null) this._select(null);
  }

  private async _loadSeen(): Promise<void> {
    try {
      const response = await this.api.discoveredHubs();
      if (response.ok && Array.isArray(response.body)) {
        const key = JSON.stringify(response.body);
        if (key !== JSON.stringify(this._seen)) this._seen = response.body;
      }
    } catch {
      // the table keeps its last answer
    }
  }

  private _refreshSoon(): void {
    if (this._debounceTimer !== null) clearTimeout(this._debounceTimer);
    this._debounceTimer = setTimeout(() => {
      this._debounceTimer = null;
      void this._loadHubs();
      void this._loadSeen();
    }, REFRESH_DEBOUNCE_MS);
  }

  private _refreshAll(): void {
    void this._loadHubs();
    void this._loadSeen();
    void this._loadServer();
  }

  // -- events from the views -----------------------------------------------------------

  private _onMessage(event: CustomEvent<{ text: string; ok: boolean }>): void {
    this._say(event.detail.text, event.detail.ok);
  }

  private _onHubsChanged(): void {
    this._refreshAll();
  }

  private _onSelectHub(event: CustomEvent<{ hubId: string }>): void {
    this._select(event.detail.hubId);
    void this._loadHubs();
  }

  private _onOpenView(event: CustomEvent<{ view: ViewName }>): void {
    this._setView(event.detail.view);
  }

  private _goAdd(): void {
    this._setView("hubs");
    void this.updateComplete.then(() => {
      const view = this.renderRoot.querySelector<SbPanelHubs>("sb-panel-hubs");
      view?.focusAddress();
    });
  }

  // -- render ---------------------------------------------------------------------------

  private _serverMeta(): string {
    if (this._serverError) return this._serverError;
    const info = this._server;
    if (!info) return "connecting…";
    const l = info.callback_listener ?? {};
    const listener = l.bound ? `on :${l.bound_port}` : l.wanted ? "wanted, not bound" : "idle";
    return `server ${info.version} · library ${info.library_version} · api ${info.api_version} · callback listener ${listener}`;
  }

  private _renderSidebar(): TemplateResult {
    const connected = this._hubs.filter((h) => h.status?.hub_connected).length;
    const summary = this._listUnavailable ? "list unavailable" : this._hubs.length ? `${connected}/${this._hubs.length} connected` : "";
    return html`
      <aside>
        <h3>Hubs <span class="hint" id="hubs-summary">${summary}</span><span class="spacer"></span>
          <button class="small" id="hubs-refresh" title="reload the hub list" @click=${this._refreshAll}>↻</button></h3>
        <div class="hub-list" id="hub-list">
          ${this._hubs.length
            ? this._hubs.map((h) => {
                const { text, tone } = hubState(h);
                return html`<div class="hub-item ${this._selected === h.hub_id ? "sel" : ""}" data-hub=${h.hub_id} @click=${() => this._select(h.hub_id)}>
                  <span class="dot ${tone}"></span><span class="name">${hubDisplayName(h)}</span><span class="sub">${h.config.host} · ${text}</span>
                </div>`;
              })
            : html`<div class="hint">No hubs registered yet. Add one by address, or pick one from the discovered list.</div>`}
        </div>
        <div class="foot"><button class="primary" id="go-add" @click=${this._goAdd}>Add a hub</button></div>
      </aside>
    `;
  }

  private _renderView(): TemplateResult {
    const hub = this.selectedHub;
    switch (this._view) {
      case "catalog":
        return html`<sb-panel-catalog .api=${this.api} .hub=${hub}></sb-panel-catalog>`;
      case "remote":
        return html`<sb-panel-remote .api=${this.api} .hub=${hub}></sb-panel-remote>`;
      case "api":
        return html`<sb-panel-api .api=${this.api} .hub=${hub} .operations=${this._operations} @sb-request-sent=${() => void this._loadServer()}></sb-panel-api>`;
      case "events":
        return html`<sb-panel-events .stream=${this.stream}></sb-panel-events>`;
      default:
        return html`<sb-panel-hubs .api=${this.api} .hubs=${this._hubs} .hub=${hub} .seen=${this._seen}></sb-panel-hubs>`;
    }
  }

  render(): TemplateResult {
    return html`
      <header>
        <div class="brand"><b>Sofabaton X</b><span>control panel</span></div>
        <div class="meta">
          <span class="server" id="server-meta" title=${this._server?.instance_id ? `instance ${this._server.instance_id}` : ""}>${this._serverMeta()}</span>
          <span class="stream" title="event stream"><span class="dot ${this._streamOn ? "ok" : "off"}" id="ws-dot"></span><span id="ws-state">${this._streamOn ? "stream live" : "stream off"}</span></span>
          <button class="theme" id="theme-toggle" title="theme: ${this._theme}" @click=${this._cycleTheme}>${this._theme === "auto" ? "auto" : this._theme}</button>
        </div>
      </header>
      <div class="shell">
        ${this._renderSidebar()}
        <div class="main" @sb-message=${this._onMessage} @sb-hubs-changed=${this._onHubsChanged} @sb-select-hub=${this._onSelectHub} @sb-open-view=${this._onOpenView}>
          <nav id="views">
            ${VIEWS.map((v) => html`<button data-view=${v} class=${this._view === v ? "on" : ""} @click=${() => this._setView(v)}>${VIEW_LABELS[v]}${v === "events" ? html`<span class="badge" id="ws-badge">${this._messageCount}</span>` : ""}</button>`)}
            <span class="spacer"></span>
            <span class="note hint ${this._message ? (this._message.ok ? "msg-ok" : "msg-err") : ""}" id="hubs-msg">${this._message?.text ?? ""}</span>
          </nav>
          <div class="view" id="view-${this._view}">${this._renderView()}</div>
        </div>
      </div>
    `;
  }
}

export function definePanel(): void {
  if (!customElements.get(PANEL_TAG)) customElements.define(PANEL_TAG, SofabatonServerPanel);
}
