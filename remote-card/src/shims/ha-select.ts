// `ha-select` + `mwc-list-item` for the web remote (docs/internal/
// web-remote-plan.md, R4). The activity row renders
// <ha-select .label .value .disabled><mwc-list-item .value>label</...>
// and listens for "selected" / "change" plus the open ("opened") and close
// ("closed") events remote-card-compat resolves for the mwc generation
// (ha-dropdown-item is never defined on the page, so that branch is the
// one the card takes). This is the harness stub promoted to a real
// element: a labelled trigger, a dropdown list, keyboard support, and the
// same token contract as HA's field so the card's styling rules apply.

interface SelectOption {
  value: string;
  label: string;
}

export class SbMwcListItem extends HTMLElement {
  private _value: string | null = null;

  get value(): string {
    return this._value ?? this.getAttribute("value") ?? "";
  }

  set value(next: unknown) {
    this._value = next == null ? "" : String(next);
    this.setAttribute("value", this._value);
  }
}

export class SbHaSelect extends HTMLElement {
  static get observedAttributes(): string[] {
    return ["label", "disabled"];
  }

  private readonly _shadow: ShadowRoot;
  private readonly _observer: MutationObserver;
  private _labelEl: HTMLElement | null = null;
  private _valueEl: HTMLElement | null = null;
  private _trigger: HTMLButtonElement | null = null;
  private _menu: HTMLElement | null = null;
  private _label = "";
  private _value = "";
  private _options: SelectOption[] = [];
  private _connected = false;

  constructor() {
    super();
    this._observer = new MutationObserver(() => this._syncOptions());
    this._shadow = this.attachShadow({ mode: "open" });
    this._shadow.innerHTML = `
      <style>
        :host { display: block; position: relative; }
        .label {
          font-size: 12px;
          color: var(--mdc-select-label-ink-color, rgba(0, 0, 0, 0.6));
          line-height: 1.2;
        }
        .trigger {
          width: 100%;
          border: 0;
          background: var(--ha-color-form-background, #f3f3f3);
          border-radius: var(--mdc-shape-small, 4px);
          min-height: 56px;
          padding: 10px 14px 8px 16px;
          color: var(--primary-text-color, #141414);
          font: inherit;
          text-align: left;
          display: grid;
          grid-template-columns: minmax(0, 1fr) auto;
          grid-template-rows: auto auto;
          gap: 2px 10px;
          cursor: pointer;
          box-shadow: inset 0 -1px 0 var(--ha-color-border-neutral-loud, rgba(0, 0, 0, 0.55));
          transition: box-shadow 180ms ease-in-out;
        }
        .trigger:focus-visible {
          outline: none;
          box-shadow: inset 0 -2px 0 var(--mdc-theme-primary, var(--primary-color, #009ac7));
        }
        .trigger:hover:not([disabled]) {
          background: color-mix(in srgb, var(--primary-text-color, #141414) 8%, var(--ha-color-form-background, #f3f3f3));
        }
        .trigger:active:not([disabled]) {
          background: color-mix(in srgb, var(--primary-text-color, #141414) 12%, var(--ha-color-form-background, #f3f3f3));
        }
        .trigger[disabled] { cursor: default; opacity: 0.6; }
        .value {
          font-size: 16px;
          line-height: 1.3;
          color: var(--primary-text-color, #141414);
          white-space: nowrap;
          overflow: hidden;
          text-overflow: ellipsis;
        }
        .caret {
          grid-column: 2;
          grid-row: 1 / span 2;
          align-self: center;
          width: 24px;
          height: 24px;
          color: var(--secondary-text-color, #5e5e5e);
        }
        .caret svg { width: 100%; height: 100%; fill: currentColor; }
        .menu {
          position: absolute;
          left: 0;
          right: 0;
          top: calc(100% + 4px);
          display: none;
          max-height: 60vh;
          overflow-y: auto;
          background: var(--card-background-color, var(--mdc-theme-surface, #fff));
          border-radius: 12px;
          border: 1px solid var(--ha-color-border-neutral-quiet, var(--divider-color, #e6e6e6));
          box-shadow: 0 2px 4px rgba(0, 0, 0, 0.08), 0 12px 28px rgba(0, 0, 0, 0.16);
          padding: 6px;
          z-index: 40;
        }
        :host([open]) .menu { display: block; }
        .option {
          width: 100%;
          border: 0;
          background: transparent;
          color: var(--primary-text-color, #141414);
          text-align: left;
          font: inherit;
          font-size: 16px;
          line-height: 1.3;
          padding: 12px 14px;
          border-radius: 8px;
          cursor: pointer;
        }
        .option:hover, .option:focus-visible {
          outline: none;
          background: var(--wa-color-neutral-fill-normal, var(--ha-color-fill-neutral-normal-resting, #e6e6e6));
        }
        .option[data-selected="true"] {
          background: var(--ha-color-fill-primary-quiet-resting, #eff9fe);
          color: var(--sb-select-selected-text, var(--primary-color, inherit));
        }
        .option + .option { margin-top: 2px; }
      </style>
      <button class="trigger" type="button" aria-haspopup="listbox" aria-expanded="false">
        <span class="label"></span>
        <span class="value"></span>
        <span class="caret"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M7 10l5 5 5-5z"></path></svg></span>
      </button>
      <div class="menu" role="listbox"></div>
    `;
    this._labelEl = this._shadow.querySelector(".label");
    this._valueEl = this._shadow.querySelector(".value");
    this._trigger = this._shadow.querySelector(".trigger");
    this._menu = this._shadow.querySelector(".menu");
  }

  connectedCallback(): void {
    if (!this._connected) {
      this._connected = true;
      this._trigger?.addEventListener("click", () => {
        if (this.disabled) return;
        if (this.hasAttribute("open")) this._closeMenu();
        else this._openMenu();
      });
      this._trigger?.addEventListener("keydown", (event) => {
        if (this.disabled) return;
        if (event.key === "ArrowDown" || event.key === "ArrowUp") {
          event.preventDefault();
          if (!this.hasAttribute("open")) this._openMenu();
          const buttons = Array.from(this._menu?.querySelectorAll<HTMLButtonElement>(".option") ?? []);
          const index = Math.max(0, this._options.findIndex((option) => option.value === this._value));
          const next = event.key === "ArrowDown" ? Math.min(buttons.length - 1, index + 1) : Math.max(0, index - 1);
          buttons[next]?.focus();
        } else if (event.key === "Escape" && this.hasAttribute("open")) {
          event.preventDefault();
          this._closeMenu();
        }
      });
      this._shadow.addEventListener("focusout", (event) => {
        const next = (event as FocusEvent).relatedTarget as Node | null;
        if (next && this._shadow.contains(next)) return;
        if (this.hasAttribute("open")) this._closeMenu();
      });
    }
    this._observer.observe(this, { childList: true, subtree: true, characterData: true });
    this._renderLabel();
    this._syncOptions();
  }

  disconnectedCallback(): void {
    this._observer.disconnect();
  }

  attributeChangedCallback(name: string): void {
    if (name === "label") this._renderLabel();
    if (name === "disabled" && this._trigger) this._trigger.disabled = this.disabled;
  }

  get label(): string {
    return this._label || this.getAttribute("label") || "";
  }

  set label(value: unknown) {
    this._label = value == null ? "" : String(value);
    this._renderLabel();
  }

  get value(): string {
    return this._value;
  }

  set value(next: unknown) {
    this._value = next == null ? "" : String(next);
    this._renderValue();
    this._renderOptions();
  }

  get disabled(): boolean {
    return this.hasAttribute("disabled");
  }

  set disabled(next: unknown) {
    if (next) this.setAttribute("disabled", "");
    else this.removeAttribute("disabled");
    if (this._trigger) this._trigger.disabled = Boolean(next);
  }

  private _renderLabel(): void {
    if (this._labelEl) this._labelEl.textContent = this.label;
  }

  private _syncOptions(): void {
    const current = this._value;
    const items = Array.from(this.children) as Array<HTMLElement & { value?: string }>;
    this._options = items.map((item) => ({
      value: String(item.value ?? item.getAttribute("value") ?? item.textContent ?? ""),
      label: (item.textContent ?? "").trim(),
    }));
    if (!this._options.some((option) => option.value === current)) {
      this._value = this._options[0]?.value ?? "";
    }
    this._renderValue();
    this._renderOptions();
  }

  private _renderValue(): void {
    if (!this._valueEl) return;
    const match = this._options.find((option) => option.value === this._value);
    this._valueEl.textContent = match?.label ?? this._value;
  }

  private _renderOptions(): void {
    if (!this._menu) return;
    this._menu.textContent = "";
    for (const option of this._options) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "option";
      button.setAttribute("role", "option");
      button.textContent = option.label;
      button.dataset.selected = String(option.value === this._value);
      button.setAttribute("aria-selected", button.dataset.selected);
      button.addEventListener("click", () => {
        this._value = option.value;
        this._renderValue();
        this._renderOptions();
        this.dispatchEvent(new Event("change", { bubbles: true, composed: true }));
        this.dispatchEvent(
          new CustomEvent("selected", { detail: { value: this._value }, bubbles: true, composed: true }),
        );
        this._closeMenu();
        this._trigger?.focus();
      });
      this._menu.appendChild(button);
    }
  }

  private _openMenu(): void {
    this.setAttribute("open", "");
    this._trigger?.setAttribute("aria-expanded", "true");
    this.dispatchEvent(new Event("opened", { bubbles: true, composed: true }));
  }

  private _closeMenu(): void {
    if (!this.hasAttribute("open")) return;
    this.removeAttribute("open");
    this._trigger?.setAttribute("aria-expanded", "false");
    this.dispatchEvent(new Event("closed", { bubbles: true, composed: true }));
  }
}

export function defineHaSelectShim(): void {
  if (!customElements.get("mwc-list-item")) customElements.define("mwc-list-item", SbMwcListItem);
  if (!customElements.get("ha-select")) customElements.define("ha-select", SbHaSelect);
}
