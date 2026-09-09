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
| 503 | `hub_start_failed` | the hub's proxy could not start (a port in use); the record is kept, retry `/enable` after fixing the host |
| 504 | `hub_timeout` | the hub did not answer; retry once, then surface it |
| 422 | `validation_error`, `invalid_hub_config` | fix the request (`detail` names the field) |

A filter passed as `?hub_id=<host>` on the event stream follows the hub
when it is re-keyed to its MAC, so a client that subscribed right after
registering by host sees `hub_rekeyed` and everything after it.

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


## Snapshot and jobs (0.2.0)

`GET /hubs/{id}/snapshot` is the hub's configuration as one document,
served from the server's cache with no hub traffic. Keep its `ETag`: it
is the content hash the write endpoints will take back as `If-Match`,
and `If-None-Match` saves the transfer when nothing changed. Entities
that were never read in full carry `editable: false`; ask for a read
with `POST /hubs/{id}/snapshot/refresh` (`{"device_id": 5}` or
`{"activity_id": 101}`; an empty body reads the whole hub, which takes
minutes and should be a user action).

A refresh answers `202` with a job. Follow it on `/events` (`job_event`
messages carry the full job record: `status`, the last `progress`, the
`result` or a `Problem` in `error`) or poll `GET /hubs/{id}/jobs/{job_id}`.
One job runs per hub at a time; `DELETE /hubs/{id}/jobs/{job_id}` cancels
a whole-hub refresh between entities.


## Editing (0.2.0)

Most platforms need the intents: `POST /hubs/{id}/activities/{aid}/rename`,
`PUT /hubs/{id}/activities/{aid}/buttons/VOL_UP` with `{"device_id": 7,
"command_id": 3}` (add `"long_press": {...}` for the held press),
`DELETE` on the same path to clear, `POST .../favorites`, `PUT
.../favorites/order`, `POST /hubs/{id}/devices/{did}/rename`, and the
whole-entity ones (`POST /devices`, `DELETE /devices/{did}`, `POST
/activities`, `DELETE /activities/{aid}`, `PUT /devices/order`, `PUT
/activities/order`, `PUT /name`). Every write answers `202` with a job; follow it as described
above. Send the snapshot `ETag` as `If-Match` when your UI showed the
user a snapshot; the server refuses with `412` if it moved.

An editor that shows the whole configuration works on the document
instead: read `GET /snapshot`, change one activity or device element,
preview with `POST /hubs/{id}/activities/{aid}/plan`, then `PUT` the
element back with `If-Match` (required here). Only the entity you name
may differ from the snapshot; anything else is `422 out_of_scope`.


## IR codes, backup and restore (0.2.0)

A code in any format your platform has (`{"pronto": "..."}`,
`{"descriptor": "P:NEC1 D:4 S:5 F:21"}`, `{"timings_us": [...],
"carrier_hz": 38000}`, or the hub's own `{"hex": "..."}`) can be fired
once with `POST /hubs/{id}/play`, saved as a new command with `POST
/hubs/{id}/devices/{did}/commands` (`{"name": ..., "payload": {...}}`),
or written over an existing command with `PUT .../commands/{cid}/payload`.
`GET .../commands/{cid}/payload` reads what the hub holds. `POST
/hubs/{id}/learn` arms the hub's receiver and returns the captured code
as the job result; cancel the job to stop waiting.

`POST /hubs/{id}/backup` returns a full bundle as the job result; keep
it as a file. `POST /hubs/{id}/restore` with `{"bundle": ..., "replace":
true}` puts a hub back to that state (it erases first), and `POST
/hubs/{id}/erase` wipes it. Both are final; confirm with the user.
