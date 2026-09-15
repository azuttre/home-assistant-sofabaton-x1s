// Entry for the web remote (docs/internal/web-remote-plan.md, R5): the
// remote card served by sofabaton-x-server at /ui/remote/. Installs the
// platform shims, then mounts <sofabaton-remote-web>, a thin host that
// resolves the hub from the URL, loads the per-hub configuration document,
// and hands the card a ServerRemoteBackend. The HA build never sees this
// file; the card element itself is shared unchanged.
//
// URL parameters: hub=<hub id> (required), lang=<bcp47>, device=<id>
// (open in device mode), zoom=<factor>, theme=light|dark.

import { ServerRemoteBackend, SERVER_API_PREFIX } from "./backend/server-backend";
import { SofabatonRemoteCard } from "./remote-card-element";
import { CARD_VERSION, TYPE, logPillsOnce } from "./remote-card-shared";
import { cardConfigForWebRemote, parseWebRemoteParams, type WebRemoteParams } from "./remote-web-config";
import { installRemoteWebShims } from "./shims/index";
import "./remote-card-translations";

export const WEB_REMOTE_TAG = "sofabaton-remote-web";

interface HubSummary {
  hub_id: string;
  enabled: boolean;
  config?: { host?: string; name?: string | null };
  status?: { hub_version?: string | null; mode?: string } | null;
}

interface UiDocumentResponse {
  hub_id: string;
  document: Record<string, unknown> | null;
  updated_at: string | null;
}

const HOST_CSS = `
  :host {
    display: block;
    min-height: 100vh;
    box-sizing: border-box;
    padding: env(safe-area-inset-top) env(safe-area-inset-right) env(safe-area-inset-bottom) env(safe-area-inset-left);
    background: var(--primary-background-color);
    color: var(--primary-text-color);
    font-family: Roboto, system-ui, -apple-system, "Segoe UI", sans-serif;
  }
  .stage {
    max-width: 480px;
    margin: 0 auto;
    padding: 12px;
  }
  .notice {
    max-width: 480px;
    margin: 24px auto;
    padding: 20px;
    border-radius: 12px;
    background: var(--card-background-color);
    border: 1px solid var(--divider-color);
    line-height: 1.5;
  }
  .notice h1 { font-size: 20px; margin: 0 0 8px; }
  .notice code { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
  .notice ul { padding-left: 20px; }
  .notice a { color: var(--primary-color); }
  .banner {
    max-width: 480px;
    margin: 0 auto 8px;
    padding: 8px 12px;
    border-radius: 8px;
    background: rgba(var(--rgb-error-color), 0.12);
    color: var(--error-color);
    font-size: 13px;
  }
  .foot {
    max-width: 480px;
    margin: 8px auto 0;
    text-align: center;
    color: var(--secondary-text-color);
    font-size: 11px;
  }
`;

export class SofabatonRemoteWeb extends HTMLElement {
  private readonly _shadow: ShadowRoot;
  private _backend: ServerRemoteBackend | null = null;
  private _card: SofabatonRemoteCard | null = null;
  private _unsubscribe: (() => void) | null = null;
  private _params: WebRemoteParams | null = null;
  private _lastBanner: string | null = null;

  constructor() {
    super();
    this._shadow = this.attachShadow({ mode: "open" });
  }

  connectedCallback(): void {
    void this._boot();
  }

  disconnectedCallback(): void {
    this._unsubscribe?.();
    this._unsubscribe = null;
    this._card?.setBackend(null);
    this._backend?.stop();
  }

  private async _boot(): Promise<void> {
    const params = parseWebRemoteParams(location.search, navigator.language);
    this._params = params;
    if (params.theme) document.documentElement.dataset.theme = params.theme;

    let hubs: HubSummary[] = [];
    let hubsError: string | null = null;
    try {
      const response = await fetch(`${SERVER_API_PREFIX}/hubs`, { headers: { accept: "application/json" } });
      if (!response.ok) throw new Error(`GET /hubs -> ${response.status}`);
      hubs = (await response.json()) as HubSummary[];
    } catch (err) {
      hubsError = err instanceof Error ? err.message : String(err);
    }

    const known = hubs.find((hub) => hub.hub_id === params.hub);
    if (!params.hub || !known) {
      this._renderInstructions(params.hub, hubs, hubsError);
      return;
    }

    let storedDocument: Record<string, unknown> | null = null;
    try {
      const response = await fetch(`${SERVER_API_PREFIX}/hubs/${encodeURIComponent(params.hub)}/ui/remote-card`, {
        headers: { accept: "application/json" },
      });
      if (response.ok) storedDocument = ((await response.json()) as UiDocumentResponse).document ?? null;
    } catch (_err) {
      storedDocument = null;
    }

    const backend = new ServerRemoteBackend({ baseUrl: "" });
    backend.setTarget(params.hub);
    this._backend = backend;

    const card = document_createCard();
    card.setConfig(cardConfigForWebRemote(params.hub, storedDocument, { openDevice: params.device }));
    card.setLanguage(params.lang);
    card.setBackend(backend);
    this._card = card;

    this._renderStage(card, known);
    this._unsubscribe = backend.subscribe(() => this._syncBanner());
    this._syncBanner();
  }

  private _renderStage(card: HTMLElement, hub: HubSummary): void {
    const zoom = this._params?.zoom;
    this._shadow.innerHTML = `<style>${HOST_CSS}</style>
      <div class="banner" id="banner" hidden></div>
      <div class="stage" id="stage"></div>
      <div class="foot">${escapeHtml(hub.config?.name || hub.hub_id)} · sofabaton-x-server · remote card ${CARD_VERSION}</div>`;
    const stage = this._shadow.getElementById("stage") as HTMLElement;
    if (zoom) stage.style.zoom = String(zoom);
    stage.appendChild(card);
  }

  private _syncBanner(): void {
    const banner = this._shadow.getElementById("banner") as HTMLElement | null;
    if (!banner || !this._backend) return;
    const snapshot = this._backend.snapshot();
    const unavailable = !snapshot || snapshot.state === "unavailable";
    const text = unavailable
      ? this._backend.lastError
        ? `The server cannot reach the hub (${this._backend.lastError}).`
        : "The hub is not controllable right now (offline, disabled, or the Sofabaton app is connected)."
      : null;
    if (text === this._lastBanner) return;
    this._lastBanner = text;
    banner.hidden = !text;
    banner.textContent = text ?? "";
  }

  private _renderInstructions(requested: string, hubs: HubSummary[], error: string | null): void {
    const list = hubs.length
      ? `<ul>${hubs
          .map((hub) => {
            const href = `?hub=${encodeURIComponent(hub.hub_id)}`;
            const label = `${escapeHtml(hub.config?.name || hub.hub_id)} (${escapeHtml(hub.status?.hub_version || "?")}, ${hub.enabled ? escapeHtml(hub.status?.mode || "starting") : "disabled"})`;
            return `<li><a href="${href}">${label}</a> <code>${escapeHtml(hub.hub_id)}</code></li>`;
          })
          .join("")}</ul>`
      : error
        ? `<p>The server did not answer <code>${SERVER_API_PREFIX}/hubs</code>: ${escapeHtml(error)}.</p>`
        : `<p>This server has no hubs registered yet. Add one with <code>POST ${SERVER_API_PREFIX}/hubs</code> or from the <a href="/harness">console</a>.</p>`;
    const why = requested
      ? `<p>No hub with id <code>${escapeHtml(requested)}</code> is registered on this server.</p>`
      : `<p>Open this page with <code>?hub=&lt;hub id&gt;</code>. The id is the hub's MAC as the server lists it.</p>`;
    this._shadow.innerHTML = `<style>${HOST_CSS}</style>
      <div class="notice">
        <h1>Sofabaton web remote</h1>
        ${why}
        ${list}
        <p>Optional parameters: <code>lang=</code>, <code>device=&lt;device id&gt;</code> to open in device mode, <code>zoom=</code>, <code>theme=light|dark</code>.</p>
      </div>`;
  }
}

function document_createCard(): SofabatonRemoteCard {
  return document.createElement(TYPE) as SofabatonRemoteCard;
}

function escapeHtml(value: unknown): string {
  return String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c] as string);
}

export function bootstrapWebRemote(): void {
  installRemoteWebShims();
  logPillsOnce();
  if (!customElements.get(TYPE)) customElements.define(TYPE, SofabatonRemoteCard);
  if (!customElements.get(WEB_REMOTE_TAG)) customElements.define(WEB_REMOTE_TAG, SofabatonRemoteWeb);
}

if (typeof window !== "undefined" && typeof customElements !== "undefined") {
  bootstrapWebRemote();
}
