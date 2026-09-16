/**
 * Codegen smoke: the committed openapi.json must be consumable by a
 * TypeScript generator, and the generated types must be usable for a
 * client. CI generates `schema.d.ts` from the document with
 * openapi-typescript and type-checks this file against it; nothing here
 * runs. If a rename in the API breaks this file, the document changed in
 * a way generated clients would feel, which is exactly what the check is
 * for.
 */
import type { components, operations, paths } from "./schema";

type HubView = components["schemas"]["HubView"];
type HubStatus = components["schemas"]["HubStatus"];
type Activity = components["schemas"]["Activity"];
type Device = components["schemas"]["Device"];
type Problem = components["schemas"]["Problem"];
type Accepted = components["schemas"]["Accepted"];
type SeenHub = components["schemas"]["SeenHub"];
type WsHello = components["schemas"]["WsHello"];
type WsHubEvent = components["schemas"]["WsHubEvent"];
type WsServerEvent = components["schemas"]["WsServerEvent"];
type WsDropped = components["schemas"]["WsDropped"];

// Every operation the platform guide relies on must exist under its stable id.
type _RequiredOperations = [
  operations["getServerInfo"],
  operations["listHubs"],
  operations["addHub"],
  operations["getHub"],
  operations["removeHub"],
  operations["enableHub"],
  operations["disableHub"],
  operations["getHubStatus"],
  operations["getHubInfo"],
  operations["listActivities"],
  operations["listDevices"],
  operations["listDeviceCommands"],
  operations["listEntityButtons"],
  operations["listActivityMacros"],
  operations["listActivityFavorites"],
  operations["getRunningActivity"],
  operations["startActivity"],
  operations["stopActivity"],
  operations["sendCommand"],
  operations["findRemote"],
  operations["listDiscoveredHubs"],
  operations["scanForHubs"],
];

// The paths a hand-written client would hit.
type _RequiredPaths = [
  paths["/api/v1/server"]["get"],
  paths["/api/v1/hubs"]["get"],
  paths["/api/v1/hubs"]["post"],
  paths["/api/v1/hubs/{hub_id}/activities/{activity_id}/start"]["post"],
  paths["/api/v1/hubs/{hub_id}/send"]["post"],
  paths["/api/v1/discovery/hubs"]["get"],
];

// A minimal typed client surface, the shape a platform integration wraps.
type WsMessage = WsHello | WsHubEvent | WsServerEvent | WsDropped;

function describeHub(hub: HubView): string {
  const status: HubStatus | null | undefined = hub.status;
  const mode = status ? status.mode : "disabled";
  return `${hub.hub_id} (${hub.config.host}) enabled=${hub.enabled} mode=${mode}`;
}

function powerOfDevice(device: Device): number | null | undefined {
  return device.power_state;
}

function activityLabel(activity: Activity): string {
  return `${activity.activity_id}: ${activity.name}${activity.active ? " *" : ""}`;
}

function isProblem(body: unknown): body is Problem {
  return typeof body === "object" && body !== null && "type" in body && "status" in body;
}

function onMessage(message: WsMessage): string {
  switch (message.type) {
    case "hello":
      return `hello api=${message.api_version} hubs=${message.hubs.length}`;
    case "hub_event":
      return `${message.hub_id} #${message.event.seq} ${message.event.kind}`;
    case "server_event":
      return `${message.hub_id} ${message.kind}`;
    case "dropped":
      return `dropped ${message.count}`;
    default:
      return "unknown";
  }
}

function accepted(result: Accepted): boolean {
  return result.accepted && result.mode === "control";
}

function seenKey(seen: SeenHub): string {
  return `${seen.key} present=${seen.present} registered=${seen.registered_hub_id ?? "no"}`;
}

export const _exercised = [describeHub, powerOfDevice, activityLabel, isProblem, onMessage, accepted, seenKey];
export type { _RequiredOperations, _RequiredPaths };
