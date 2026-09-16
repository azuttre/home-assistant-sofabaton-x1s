// The Events view (docs/internal/server-panel-plan.md, decision 6): the
// stream the shell keeps open, listed with a hub filter (applied on
// reconnect), a text filter and expand-all; press messages highlighted.

import { LitElement, html, css, type TemplateResult } from "lit";

import { summarizeMessage, type PanelStream } from "../panel-stream";
import { prettyJson } from "../panel-state";
import { PANEL_BASE_CSS } from "../panel-styles";

export const EVENTS_VIEW_TAG = "sb-panel-events";

export class SbPanelEvents extends LitElement {
  static properties = {
    stream: { attribute: false },
    _grep: { state: true },
    _expand: { state: true },
    _tick: { state: true },
  };

  static styles = [
    PANEL_BASE_CSS,
    css`
      :host { display: block; }
      h2 input { max-width: 320px; font-size: 12px; padding: 3px 8px; }
      .list { display: flex; flex-direction: column; gap: 4px; }
      details { border: 1px solid var(--sbp-line); border-radius: 6px; background: var(--sbp-bg); }
      summary { padding: 5px 10px; cursor: pointer; font-family: var(--sbp-mono); font-size: 12px; display: flex; gap: 10px; }
      summary .t { color: var(--sbp-muted); }
      details pre { border: 0; border-top: 1px solid var(--sbp-line); border-radius: 0; }
      .k-press summary { border-left: 3px solid var(--sbp-press); }
      .k-job_event summary { border-left: 3px solid var(--sbp-warn); }
      .k-server_event summary { border-left: 3px solid var(--sbp-accent); }
      .k-hub_event summary { border-left: 3px solid var(--sbp-ok); }
      .k-dropped summary { border-left: 3px solid var(--sbp-err); }
    `,
  ];

  stream!: PanelStream;
  private _grep = "";
  private _expand = false;
  private _tick = 0;
  private _unsubscribe: (() => void)[] = [];

  connectedCallback(): void {
    super.connectedCallback();
    this._subscribe();
  }

  disconnectedCallback(): void {
    super.disconnectedCallback();
    for (const off of this._unsubscribe) off();
    this._unsubscribe = [];
  }

  private _subscribe(): void {
    if (!this.stream || this._unsubscribe.length) return;
    this._unsubscribe = [this.stream.onMessage(() => this._bump()), this.stream.onState(() => this._bump())];
  }

  private _bump(): void {
    this._tick++;
  }

  protected updated(): void {
    this._subscribe();
    const list = this.renderRoot.querySelector<HTMLElement>("#ws-list");
    list?.lastElementChild?.scrollIntoView({ block: "nearest" });
  }

  private _applyFilter(): void {
    const raw = this.renderRoot.querySelector<HTMLInputElement>("#ws-filter")?.value ?? "";
    this.stream.hubFilter = raw.split(",").map((s) => s.trim()).filter(Boolean);
    this.stream.restart();
    this._bump();
  }

  private _toggle(): void {
    if (this.stream.wanted) this.stream.stop();
    else this.stream.start();
    this._bump();
  }

  render(): TemplateResult {
    const stream = this.stream;
    const grep = this._grep.trim().toLowerCase();
    const rows = stream ? stream.messages : [];
    const shown = rows.filter((row) => !grep || (summarizeMessage(row.data) + " " + row.text).toLowerCase().includes(grep));
    return html`
      <div class="panel">
        <h2>Event stream <span class="hint mono">/events</span><span class="spacer"></span>
          <input id="ws-filter" placeholder="hub_id filter (optional, comma separated)" @change=${this._applyFilter}>
          <button class="small" id="ws-toggle" @click=${this._toggle}>${stream?.wanted ? "disconnect" : "connect"}</button>
          <button class="small" id="ws-clear" @click=${() => { stream?.clear(); this._bump(); }}>clear</button></h2>
        <div class="row" style="margin-bottom: 8px; align-items: center">
          <input id="ws-grep" placeholder="show only messages containing… (type, kind, hub, label)" @input=${(e: Event) => { this._grep = (e.target as HTMLInputElement).value; }}>
          <label class="inline fixed"><input type="checkbox" id="ws-expand" .checked=${this._expand} @change=${(e: Event) => { this._expand = (e.target as HTMLInputElement).checked; }}> expand all</label>
        </div>
        <div class="hint" id="ws-count">${shown.length} of ${rows.length} messages${stream?.connected ? "" : " · not connected"}</div>
        <div class="list" id="ws-list">
          ${shown.map((row) => html`<details class="k-${String(row.data.type ?? "raw")}" ?open=${this._expand}>
            <summary><span class="t">${row.at}</span><span>${summarizeMessage(row.data)}</span></summary>
            <pre>${prettyJson(row.text)}</pre>
          </details>`)}
        </div>
      </div>
    `;
  }
}

export function defineEventsView(): void {
  if (!customElements.get(EVENTS_VIEW_TAG)) customElements.define(EVENTS_VIEW_TAG, SbPanelEvents);
}
