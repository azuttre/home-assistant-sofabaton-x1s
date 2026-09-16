// `ha-card` for the web remote (docs/internal/web-remote-plan.md, R4). The
// card wraps itself in <ha-card>; on HA that element paints the card
// surface from the theme tokens. Same tokens here (the literals are HA's
// ha-card defaults), so the remote card's own theming code, which sets
// --ha-card-background and friends on this host, keeps working.

export class SbHaCard extends HTMLElement {
  constructor() {
    super();
    const shadow = this.attachShadow({ mode: "open" });
    shadow.innerHTML = `
      <style>
        :host {
          display: block;
          position: relative;
          box-sizing: border-box;
          background: var(--ha-card-background, var(--card-background-color, #fff));
          -webkit-backdrop-filter: var(--ha-card-backdrop-filter, none);
          backdrop-filter: var(--ha-card-backdrop-filter, none);
          border-radius: var(--ha-card-border-radius, 12px);
          border-width: var(--ha-card-border-width, 1px);
          border-style: solid;
          border-color: var(--ha-card-border-color, var(--divider-color, #e0e0e0));
          box-shadow: var(--ha-card-box-shadow, none);
          color: var(--primary-text-color);
          transition: all 0.3s ease-out;
        }
        :host([raised]) {
          border: none;
          box-shadow: var(--ha-card-box-shadow, 0px 2px 1px -1px rgba(0, 0, 0, 0.2), 0px 1px 1px 0px rgba(0, 0, 0, 0.14), 0px 1px 3px 0px rgba(0, 0, 0, 0.12));
        }
      </style>
      <slot></slot>
    `;
  }
}

export function defineHaCardShim(): void {
  if (!customElements.get("ha-card")) customElements.define("ha-card", SbHaCard);
}
