# Integrating an automation platform with sofabaton-x-server

For authors of a Homey app, a Hubitat driver, an openHAB binding, or any
other client. The server fronts one or more Sofabaton X1 / X1S / X2 hubs
on the user's LAN and exposes them over HTTP and one WebSocket. Generate
your client from `openapi.json` in this directory; this page covers what
the document cannot say.

## 1. Find the server

The server advertises `_sofabaton-x._tcp.local.` over mDNS. TXT fields:

| key | meaning |
| --- | --- |
| `version` | server version |
| `api` | API version (`1`); bump means generated clients must be re-checked |
| `path` | API prefix (`/api/v1`) |
| `hubs` | number of configured hubs |
| `base_url` | present only when the operator runs the server behind a reverse proxy; the URL clients must use |

Endpoint rule: use `base_url` when present, otherwise
`http://<SRV host>:<SRV port>` followed by `path`. Always let the user
type a host and port as well; some networks block multicast.

`GET /api/v1/server` returns the same information plus `features`
(`discovery` when the server can browse the LAN).

There is **no authentication** in v1. The server is a LAN service; the
operator is told not to expose it beyond the LAN.

## 2. Find hubs

Three ways, in the order users expect:

1. **Ask the server.** `GET /api/v1/discovery/hubs` is the live table of
   hubs the server has seen (`present` says whether the advertisement is
   current; `registered_hub_id` links to a configured hub). `POST
   /api/v1/discovery/scan` with `{"timeout": 5}` listens for that long
   first, for a synchronous answer.
2. **Your own mDNS.** Hubs advertise `_x1hub._udp.local.` (X1, X1S) and
   `_sofabaton_hub._udp.local.` (X2). Pass what you saw to `POST
   /api/v1/hubs`: `host`, plus `mac`, `name`, `txt` and `hub_version`
   when you have them. **Skip advertisements whose TXT carries
   `HA_PROXY=1`**: those are proxies (this server's or Home Assistant's)
   that mimic hubs on purpose. If you send one anyway the server refuses
   it with 409 and, when it knows, the id of the hub it fronts.
3. **Manual.** `POST /api/v1/hubs` with `{"host": "192.168.1.50"}`. Host
   is the only required field; the server confirms the model from the
   hub itself.

## 3. Register and identify

`POST /api/v1/hubs` returns the record. Until the server has read the
hub's banner, `hub_id` is the host string; after the first connection it
becomes the hub's MAC (lower-case hex, no separators) and the record is
re-keyed once. Watch for `hub_rekeyed` on the event stream, or re-read
`GET /api/v1/hubs` after `status.catalog_ready` turns true. Store the
MAC form.

`enabled` is the user's switch: `POST /api/v1/hubs/{id}/disable` keeps
the record but disconnects, so the official Sofabaton app can talk to
the hub directly again; `/enable` reconnects. Reads and control on a
disabled hub answer 409 `hub_disabled`.

## 4. Read and control

Everything is keyed on `(entity_id, command_id)`: activities have ids
from 101 up, devices from 1 up. Browse to get the ids, then send.

| what | call |
| --- | --- |
| status (mode, running activity, `catalog_ready`) | `GET /hubs/{id}/status` |
| identity (model, name, MAC, firmware) | `GET /hubs/{id}/info` |
| activities / devices | `GET /hubs/{id}/activities`, `GET /hubs/{id}/devices` (`?refresh=true` re-reads power state) |
| a device's commands | `GET /hubs/{id}/devices/{dev}/commands` |
| buttons bound on an entity | `GET /hubs/{id}/entities/{ent}/buttons` |
| an activity's macros / favorites | `GET /hubs/{id}/activities/{act}/macros`, `.../favorites` |
| running activity | `GET /hubs/{id}/activity` (null when idle) |
| switch activity | `POST /hubs/{id}/activities/{act}/start`, `.../stop` |
| send a command | `POST /hubs/{id}/send` `{entity_id, command_id}` |
| beep the remote | `POST /hubs/{id}/find-remote` |

`mode` explains refusals: `control` (the server owns the hub),
`observe` (the official app is attached through the proxy; reads work
from cache, sends are refused with 409 `send_refused`), `disconnected`.

Errors are `Problem` bodies (`type`, `title`, `status`, `detail`,
`hub_id`, `mode`):

| status | type | what to do |
| --- | --- | --- |
| 404 | `hub_not_found`, `device_not_found`, `activity_not_found`, `entity_not_found` | fix the id |
| 409 | `hub_disabled` | enable the hub |
| 409 | `hub_busy`, `send_refused` | the app holds the hub; retry later or tell the user |
| 503 | `hub_not_connected` | the hub is offline or reconnecting; retry with backoff |
| 504 | `hub_timeout` | the hub did not answer; retry once, then surface it |
| 422 | validation | fix the request |

## 5. Events

One WebSocket: `ws://<server>/api/v1/events` (`?hub_id=` to narrow).
Messages are JSON with a `type`:

- `hello` (once): server version, API version, the hub list.
- `hub_event`: `hub_id` and `event` (`seq`, `kind`, `payload`). Kinds:
  `activity_changed` (payload `activity_id`, `previous_activity_id`,
  `name`; `activity_id` null means powered off), `activity_list_updated`
  (re-read activities), `hub_state` / `app_state` (`connected`),
  `status_changed` (`mode`, `previous_mode`), `catalog_ready` (`ready`),
  `ota` (the hub goes silent for a few minutes).
- `server_event`: `hub_id` and `kind`: `hub_added`, `hub_removed`,
  `hub_enabled`, `hub_disabled`, `hub_rekeyed`, `hub_discovered`,
  `hub_lost`.
- `dropped`: `count` older messages were discarded because your client
  fell behind; resync by re-reading status.

`seq` is per hub per session and restarts after enable. Reconnect with
backoff on close; the `hello` tells you the current hub list. The
message types are in `openapi.json` components (`WsHello`, `WsHubEvent`,
`WsServerEvent`, `WsDropped`) for your generator.

## 6. A pairing flow that feels right

1. Discover the server over mDNS (fall back to a typed host and port).
2. `GET /api/v1/discovery/hubs`; offer the present ones, plus "enter an
   IP".
3. `POST /api/v1/hubs` with the chosen record; show the hub as
   "connecting" until `catalog_ready`.
4. Read the catalogs, subscribe to events, done. Offer the enable /
   disable switch in the hub's settings so the user can hand the hub to
   the official app when they need it.
