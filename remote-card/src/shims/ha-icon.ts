// `ha-icon` for the web remote (docs/internal/web-remote-plan.md, R4): the
// card's templates and sb-key-button create <ha-icon icon="mdi:..."> and
// on HA the frontend renders it. The page registers this element under
// the same tag so the card's markup stays untouched. Path data comes from
// the generated mdi-icons module; an unknown name renders a neutral dot,
// which is what a user sees when a favourite names an icon the bundle
// does not carry (documented in the README).

import { MDI_ICON_PATHS } from "./mdi-icons";

const FALLBACK_PATH = "M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8z";

export function mdiPathFor(icon: string | null | undefined): string | null {
  const name = String(icon ?? "").trim().replace(/^mdi:/, "");
  if (!name) return null;
  return MDI_ICON_PATHS[name] ?? null;
}

export class SbHaIcon extends HTMLElement {
  static get observedAttributes(): string[] {
    return ["icon"];
  }

  private readonly _shadow: ShadowRoot;
  private _rendered: string | null = null;

  constructor() {
    super();
    // Shadow DOM on purpose: light-DOM rendering would mutate Lit's cloned
    // template fragments during the upgrade and shift its part indexes.
    this._shadow = this.attachShadow({ mode: "open" });
  }

  get icon(): string {
    return this.getAttribute("icon") ?? "";
  }

  set icon(value: string | null | undefined) {
    if (value == null || value === "") this.removeAttribute("icon");
    else this.setAttribute("icon", String(value));
  }

  connectedCallback(): void {
    this._render();
  }

  attributeChangedCallback(): void {
    this._render();
  }

  private _render(): void {
    const icon = this.icon;
    if (this._rendered === icon) return;
    this._rendered = icon;
    const path = mdiPathFor(icon) ?? FALLBACK_PATH;
    this._shadow.innerHTML = `
      <style>
        :host {
          display: inline-flex;
          align-items: center;
          justify-content: center;
          width: var(--mdc-icon-size, 24px);
          height: var(--mdc-icon-size, 24px);
          color: inherit;
          vertical-align: middle;
        }
        svg { width: 100%; height: 100%; fill: currentColor; display: block; }
      </style>
      <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="${path}"></path></svg>
    `;
  }
}

export function defineHaIconShim(): void {
  if (!customElements.get("ha-icon")) customElements.define("ha-icon", SbHaIcon);
}
