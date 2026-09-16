// RemoteBackend over a Lovelace `hass` object: the behaviour the card had
// before the port existed, moved verbatim (docs/internal/web-remote-plan.md,
// section 4.2). The card's `set hass()` feeds it; the store never reads
// `hass` directly any more.

import { remoteSendCommandData } from "../remote-card-actions";
import type { DeviceKeymapResponse, DevicePowerStateResponse } from "../remote-card-types";
import type { HassLike } from "./hass-types";
import type {
  RemoteActivityRef,
  RemoteBackend,
  RemoteIntegration,
  RemoteSnapshot,
} from "./remote-backend";

const INTEGRATION_BY_PLATFORM: Record<string, RemoteIntegration> = {
  sofabaton_x1s: "x1s",
  sofabaton_hub: "hub",
};

export class HaRemoteBackend implements RemoteBackend {
  readonly kind = "ha" as const;

  private _hass: HassLike | null = null;
  private _entityId = "";

  get hass(): HassLike | null {
    return this._hass;
  }

  get entityId(): string {
    return this._entityId;
  }

  setHass(hass: HassLike | null): void {
    this._hass = hass;
  }

  setTarget(target: string): void {
    this._entityId = String(target ?? "");
  }

  snapshot(): RemoteSnapshot | undefined {
    if (!this._entityId) return undefined;
    return this._hass?.states?.[this._entityId] as RemoteSnapshot | undefined;
  }

  async probeIntegration(): Promise<RemoteIntegration> {
    if (!this._hass?.callWS || !this._entityId) {
      throw new Error("hass.callWS unavailable");
    }
    // The entity registry exposes the integration as `platform`.
    const entry = await this._hass.callWS<{ platform?: string }>({
      type: "config/entity_registry/get",
      entity_id: this._entityId,
    });
    return INTEGRATION_BY_PLATFORM[String(entry?.platform || "")] ?? "unknown";
  }

  private entryId(): string {
    return String(this.snapshot()?.attributes?.entry_id ?? "");
  }

  async devicePowerState(deviceId: number): Promise<0 | 1 | null> {
    if (!this._hass?.callWS) return null;
    const entryId = this.entryId();
    if (!entryId) return null;
    try {
      const response = await this._hass.callWS<DevicePowerStateResponse>({
        type: "sofabaton_x1s/device/power_state",
        entry_id: entryId,
        device_id: deviceId,
      });
      // Strict 0/1 only: null (hub could not read the row) must NOT
      // coerce to "off", or a blind fire would desync hub bookkeeping.
      const raw = response?.power_state;
      return raw === 1 ? 1 : raw === 0 ? 0 : null;
    } catch (_err) {
      return null;
    }
  }

  async deviceKeymap(deviceId: number): Promise<DeviceKeymapResponse | null> {
    if (!this._hass?.callWS) return null;
    const entryId = this.entryId();
    if (!entryId) return null;
    return this._hass.callWS<DeviceKeymapResponse>({
      type: "sofabaton_x1s/device/keymap",
      entry_id: entryId,
      device_id: deviceId,
    });
  }

  async sendCommand(commandId: unknown, scopeId: unknown): Promise<void> {
    const serviceData = remoteSendCommandData(this._entityId, commandId, scopeId);
    if (!serviceData) return;
    await this.callService("remote", "send_command", serviceData);
  }

  async sendRawCommandList(list: unknown[]): Promise<void> {
    await this.callService("remote", "send_command", {
      entity_id: this._entityId,
      command: list,
    });
  }

  async startActivity(activity: RemoteActivityRef): Promise<void> {
    await this.callService("remote", "turn_on", {
      entity_id: this._entityId,
      activity: activity.name,
    });
  }

  async stopActivity(): Promise<void> {
    await this.callService("remote", "turn_off", { entity_id: this._entityId });
  }

  async callService(
    domain: string,
    service: string,
    data: Record<string, unknown> = {},
    target: Record<string, unknown> | undefined = undefined,
  ): Promise<unknown> {
    if (!this._hass?.callService) {
      throw new TypeError("hass.callService unavailable");
    }
    return this._hass.callService(domain, service, data, target);
  }
}
