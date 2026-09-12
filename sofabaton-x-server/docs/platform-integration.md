# Integrating an automation platform with sofabaton-x-server

> **Unreleased development API.** The server has not had its first release;
> its API may change before release.

For authors of a Homey app, a Hubitat driver, an openHAB binding, or any
other client. The server fronts one or more Sofabaton X1 / X1S / X2 hubs
on the user's LAN and exposes them over HTTP and one WebSocket. Generate
your client from [`../openapi.json`](../openapi.json); this page covers what
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

Use `base_url` when present, otherwise `http://<SRV host>:<SRV port>`,
as the **server base URL**. Append TXT `path` (currently `/api/v1`) for
the **API root**. Trim a trailing slash; preserve any reverse-proxy prefix.

| deployment | server base URL | API root |
| --- | --- | --- |
| direct | `http://192.168.1.10:8480` | `http://192.168.1.10:8480/api/v1` |
| reverse proxy | `https://home.example/sofabaton` | `https://home.example/sofabaton/api/v1` |

The endpoint tables below use paths relative to the API root. OpenAPI
operation paths already include `/api/v1`, so configure generated clients
with the server base URL instead. A WebSocket uses the API root plus
`/events`, changing `http` to `ws` or `https` to `wss`. Always offer a
manual server URL as well; some networks block multicast.

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
the hub directly again; `/enable` reconnects. Catalog reads and control on a
disabled hub answer 409 `hub_disabled`. The status endpoint remains readable:
its body contains `hub_id`, `enabled` and `status` (null when no proxy runs).
Disable/remove are refused with
409 `hub_job_running` while a job owns the hub; wait for it, or request
cancellation if its `cancellable` flag is true.

## 4. Read and control

Everything is keyed on `(entity_id, command_id)`: activities have ids
from 101 through 255, devices from 1 through 100. Browse to get the ids,
then send. Control calls return an acceptance response immediately;
configuration edits use jobs (section 7).

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
| 504 | `hub_timeout` | a read timed out; retry with backoff; for an uncertain write, inspect jobs and hub state first |
| 422 | `validation_error`, `invalid_hub_config` | fix the request (`detail` names the field) |
| 409 | `entity_not_editable` | refresh the target entity before editing |
| 409 | `hub_job_running` | wait for the active job to finish |
| 409 | `job_not_cancellable` | the operation cannot be cancelled or has already finished; read its status |
| 409 | `ir_learn_failed` | inspect the reason; retry capture with the hub idle when appropriate |
| 412 | `snapshot_outdated` | get a new snapshot and reapply the user's intended edit |
| 428 | `if_match_required` | send the quoted snapshot revision in `If-Match` |
| 422 | `invalid_request`, `invalid_payload`, `out_of_scope` | fix the input; inspect `detail` for the rejected edit or payload |
| 502 | `hub_rejected` | a write was refused or not acknowledged; inspect state before retrying |
| 409 / 502 | `sync_failed`, `restore_failed` | inspect the failed job's `error` and partial `result`; reconcile before another write |
| 404 | `job_not_found` | the job is unknown, expired, or lost across restart; inspect the snapshot |

Once a request returns `202`, operation failures are reported in the job,
not as a later HTTP error from the original request. Polling a failed job
still returns HTTP 200: its `status` is `failed` and `error.status` carries
the mapped failure code. Neither an HTTP timeout nor a missing job proves
that the hub was unchanged. Never blindly retry creates or restores.

A filter passed as `?hub_id=<host>` on the event stream follows the hub
when it is re-keyed to its MAC, so a client that subscribed right after
registering by host sees `hub_rekeyed` and everything after it.

## 5. Events

One WebSocket at the URL derived in section 1, for example
`ws://192.168.1.10:8480/api/v1/events` (`?hub_id=` to narrow, repeatable).
Messages are JSON with a `type`:

- `hello` (once): server version, API version, the hub list.
- `hub_event`: `hub_id` and `event` (`seq`, `kind`, `payload`). Kinds:
  `activity_changed` (payload `activity_id`, `previous_activity_id`,
  `name`; `activity_id` null means powered off), `activity_list_updated`
  (re-read activities), `hub_state` / `app_state` (`connected`),
  `status_changed` (`mode`, `previous_mode`), `catalog_ready` (`ready`),
  `snapshot_changed` (`snapshot_id`, `engine_generation`, affected ids;
  re-read the snapshot), and `ota` (the hub goes silent for a few
  minutes). `app_state` with `connected: false` means a vendor-app
  session through the proxy just ended and is a good moment to offer a
  refresh.
- `server_event`: `hub_id` and `kind`: `hub_added`, `hub_removed`,
  `hub_enabled`, `hub_disabled`, `hub_rekeyed`, `hub_discovered`,
  `hub_lost`.
- `job_event`: `hub_id` and the full `job` record on queueing, starting,
  progress updates and completion (`done`, `failed` or `cancelled`).
- `press`: a button press the hub delivered to the server's callback
  listener (section 10): `seq`, `hub_id`, `device_id`, `command_id`,
  `slot`, `label`, `press_type` (`short` / `long`), `resolution`,
  `transport`, `source`, `received_at`.
- `dropped`: `count` older messages were discarded because your client
  fell behind; re-read hub records, status, relevant snapshots and jobs.

`seq` is per hub per session and restarts after enable. Reconnect with
backoff on close; the `hello` tells you the current hub list. The
message types are in `openapi.json` components (`WsHello`, `WsHubEvent`,
`WsServerEvent`, `WsJobEvent`, `WsPress`, `WsDropped`) for your generator.`WsServerEvent`, `WsJobEvent`, `WsDropped`) for your generator. A gap in
the hub's sequence also requires reconciliation. There is no event replay:
re-read relevant state after reconnecting rather than assuming every event
was delivered. Job events do not carry the library's per-hub `seq`.

## 6. Pair a hub

1. Discover the server over mDNS (fall back to a typed host and port).
2. `GET /api/v1/discovery/hubs`; offer the present ones, plus "enter an
   IP".
3. `POST /api/v1/hubs` with the chosen record; show the hub as
   "connecting" until `catalog_ready`.
4. Read the catalogs, subscribe to events, done. Offer the enable /
   disable switch in the hub's settings so the user can hand the hub to
   the official app when they need it.


## 7. Snapshots and jobs

`GET /hubs/{id}/snapshot` is the hub's configuration as one document,
projected from the library's cache with no hub traffic. Keep two values:

- `snapshot_id` in the body is the configuration revision. Send it quoted
  as `If-Match` when editing, for example `If-Match: "<snapshot_id>"`.
- HTTP `ETag` is an opaque response validator that also covers provenance
  (`fetched_at`, `complete`, `editable`), so a conditional read returns
  200 when only provenance changed. Send it back unchanged as
  `If-None-Match` for conditional reads. Do not substitute it for the edit
  revision or assume that both values are equal.

Provenance can change without a new configuration revision. The server
never claims the cache is current: the hub can be edited outside it at any
time without notice, so `fetched_at` is the age of each entity's copy and
the decision to refresh is the user's. A 304 means the cached
representation is what the server holds, not that the hub agrees. Entities
that were never read in full carry `editable: false`; ask for a read
with `POST /hubs/{id}/snapshot/refresh` (`{"device_id": 5}` or
`{"activity_id": 101}`; an empty body reads the whole hub, which takes
tens of seconds to minutes and should be a user action). Initial catalogs
are read automatically. Persistence preserves previously fetched detail;
it does not make incomplete detail complete after a restart.

A refresh answers `202` with a job. Follow it on `/events` (`job_event`
messages carry the full job record: `status`, the last `progress`, the
`result` or a `Problem` in `error`) or poll `GET /hubs/{id}/jobs/{job_id}`.
One job runs per hub at a time; `DELETE /hubs/{id}/jobs/{job_id}` requests
cancellation of a whole-hub refresh or IR learn. A refresh finishes its
in-flight entity before releasing the hub, even if cancellation is requested
again. Wait for terminal status before submitting another job. Configuration
writes, single-entity refreshes, backup and restore are not cancellable.

Only recent jobs are retained, in memory. Persist the hub id and any
outstanding job id in your client, but reconcile after server restart.
Cached reads can continue while a job runs; reads needing hub traffic may
wait or fail. In particular, other traffic can interrupt an IR capture.


## 8. Complete edit workflow

Most platforms need the intents: `POST /hubs/{id}/activities/{aid}/rename`,
`PUT /hubs/{id}/activities/{aid}/buttons/VOL_UP` with `{"device_id": 7,
"command_id": 3}` (add `"long_press": {...}` for the held press),
`DELETE` on the same path to clear, `POST .../favorites`, `PUT
.../favorites/order`, `POST /hubs/{id}/devices/{did}/rename`, and the
whole-entity ones (`POST /devices`, `DELETE /devices/{did}`, `POST
/activities`, `DELETE /activities/{aid}`, `PUT /devices/order`, `PUT
/activities/order`, `PUT /name`). Every write answers `202` with a job; follow it as described
above. Send the quoted `snapshot_id` as `If-Match` when your UI showed the
user a snapshot; the server refuses with `412` if it moved.

An editor that shows the whole configuration works on the document
instead: read `GET /snapshot`, change one activity or device element,
preview with `POST /hubs/{id}/activities/{aid}/plan`, then `PUT` the
element back with `If-Match` (required here). Only the entity you name
may differ from the snapshot; anything else is `422 out_of_scope`.

The cache revision check and the hub check serve different purposes.
Sync-based edits re-read the target before writing and fail with
`sync_failed` at `stale_check` if the live entity differs. Whole-entity
intents use their own validation, not this same baseline comparison.
An edit also needs `editable: true`; refresh the entity if necessary.

For example, renaming activity 101 on a registered hub follows these calls
(all paths here are complete, before any reverse-proxy prefix):

| step | request | response/action |
| --- | --- | --- |
| ready | `GET /api/v1/hubs/{hub_id}/status` | check `enabled`; wait for `status.catalog_ready` and `status.controllable` |
| fetch detail | `POST /api/v1/hubs/{hub_id}/snapshot/refresh` with `{"activity_id":101}` | `202` with `job_id`; poll to `done` |
| baseline | `GET /api/v1/hubs/{hub_id}/snapshot` | save `snapshot_id`; copy the activity 101 element |
| edit | change the copy's `device.name` to `Movie night` | preserve its other fields |
| preview | `POST /api/v1/hubs/{hub_id}/activities/101/plan` with that element | plan with `step_count` and `steps` |
| apply | `PUT /api/v1/hubs/{hub_id}/activities/101` with the same element and quoted revision in `If-Match` | `202`; follow the returned job |
| reconcile | `GET /api/v1/hubs/{hub_id}/snapshot` after completion | adopt the hub's resulting state |

A `202` response includes a job record, for example these identifying
fields (timestamps and other fields omitted here):

```json
{"job_id":"abc123","hub_id":"e26a44861b45","kind":"sync_activity","status":"queued","cancellable":false}
```

`GET /api/v1/hubs/e26a44861b45/jobs/abc123` returns that job's current
record. A successful sync finishes with `status: "done"` and a `result`
containing `status: "success"`, `completed_steps` and `snapshot_id`. A
failure finishes with `status: "failed"`; `error` explains it and `result`
may retain partial completion information. A cancelled job has
`status: "cancelled"`. Handle all three terminal states.

The runnable [REST example](../examples/edit_activity.py) performs this
workflow using only Python's standard library, including readiness and job
polling. It previews by default; `--apply` submits the change:

```sh
python sofabaton-x-server/examples/edit_activity.py --server http://localhost:8480 --hub-id e26a44861b45 --activity 101 --name "Movie night"
python sofabaton-x-server/examples/edit_activity.py --server http://localhost:8480 --hub-id e26a44861b45 --activity 101 --name "Movie night" --apply
```

On `snapshot_outdated` or a sync's `stale_check` failure, refresh the target,
obtain a new snapshot and reapply the intended change. Do not resend the old
edited document with a new revision: that could overwrite changes made
elsewhere. On a failure after writing starts, inspect the partial result
and current snapshot before constructing another edit.


### Editing the whole document

When one user action changes several entities (a new device, the
activities that use it, an old device removed, a reorder), send the
whole edited document instead of a sequence of jobs and let the server
own the transition:

| step | request | response/action |
| --- | --- | --- |
| baseline | `GET /api/v1/hubs/{hub_id}/snapshot` | save `snapshot_id`; edit a copy of the document |
| new entities | give each a negative `device.device_id` (`-1`, `-2`, ...) and reference it by that id everywhere | the hub assigns the real ids |
| preview | `POST /api/v1/hubs/{hub_id}/snapshot/plan` with the document | ordered `items`, `notes` to confirm, `live_check_count` re-reads |
| apply | `PUT /api/v1/hubs/{hub_id}/snapshot` with the document, `If-Match` and an `Idempotency-Key` | `202`; follow the `sync_hub` job |
| result | the finished job's `result` | `status`, per-item outcomes, `id_map`, `apply_id` |
| stopped | job `failed` with `apply_stopped` | `POST /api/v1/hubs/{hub_id}/applies/{apply_id}/resume` when ready |

Rules the server enforces before any hub traffic: a removed entity must
be removed from every activity in the same document (`422
dangling_reference`), every edited entity must be `editable` (`409
entity_not_editable`), a device deletion needs every activity read in
full (`409 snapshot_incomplete`), and each entity's change must be one
the live editor supports (`422 out_of_scope`). Array order is display
order. A created device's command rows carry their payload in
`restore_data` (the same shape `POST .../commands` takes).

The run stops at the first item that does not end `done`; the items
before it landed, the rest were not attempted, and nothing is rolled
back. Read the record (`GET /applies/{apply_id}`) to see each item's
`status` (`done`, `partial`, `uncertain`, `failed`, `not_attempted`,
`cancelled`) and resume when the cause is gone; the server re-reads what
the run touched, keeps the ids it created and re-plans the rest. A job
cancelled with `DELETE /jobs/{job_id}` finishes the item in flight and
leaves the record resumable too.

## 9. IR codes, backup and restore

A code in any format your platform has (`{"pronto": "..."}`,
`{"descriptor": "P:NEC1 D:4 S:5 F:21"}`, `{"timings_us": [...],
"carrier_hz": 38000}`, or the hub's own `{"hex": "..."}`) can be fired
once with `POST /hubs/{id}/play`, saved as a new command with `POST
/hubs/{id}/devices/{did}/commands` (`{"name": ..., "payload": {...}}`),
or written over an existing command with `PUT .../commands/{cid}/payload`.
`GET .../commands/{cid}/payload` reads what the hub holds. `POST
/hubs/{id}/learn` arms the hub's receiver and returns the captured code
as the job result; cancel the job to stop waiting.

`POST /hubs/{id}/play` returns an immediate acceptance response; it is not
a configuration job. Keep the hub idle while learning, since other hub
traffic can interrupt capture.

`POST /hubs/{id}/backup` returns a full bundle in the completed job's
`result.bundle`; keep that whole document as a file. A structural snapshot
or a backup with `include_blobs: false` cannot be restored. `POST
/hubs/{id}/restore` with `{"bundle": ..., "replace": true}` validates
the bundle and its references, erases, then rebuilds the configuration.
With `replace` false or omitted, restore creates additional entities with
new ids. `POST /hubs/{id}/erase` wipes the hub.

Replacing restore and erase affect the whole hub; delete endpoints and
payload replacement can also remove data. Explain the affected scope in
your client before the user commits the operation. Restore has no rollback:
inspect `failed_at`, restored counts, `device_id_map` and the
snapshot before recovery. Never automatically retry an additive restore.

## 10. Button events

Deploy a callback device once per hub and the remote becomes an input
device for your platform:

1. `POST /api/v1/hubs/{id}/callback-device` with the slot labels your
   users will see (up to ten; every slot is written, unnamed ones as
   `Button n`). The job result is the record: `device_id` and `labels`
   (command ids `1..10` short, `11..20` long).
2. Let the user bind those commands with the generic edit routes, or do
   it for them: a hard button in an activity, a favorite, activity
   membership. Nothing else is needed; the hub calls the server when the
   user presses.
3. Handle `press` messages on `/events`: `device_id` and `command_id`
   identify the slot, `label` is what you named it, `press_type` tells
   short from long. Ignore `resolution` values other than `deployed` if
   you only want presses that match what you deployed.
4. Keep `(instance_id, seq)` of the last press you handled. On
   reconnect, or after a `dropped` message, `GET
   /api/v1/hubs/{id}/presses?after=<seq>` returns what you missed, oldest
   first; `expired: true` means the ring no longer reaches back that far.
   A different `instance_id` (in `hello` and `GET /api/v1/server`) means
   the server restarted: the sequence started over and there is no
   history to fetch.
5. Rename slots with `PUT /callback-device`; bindings survive, because
   the device and its command ids stay. A failed job with
   `callback_update_declined` means the device was edited outside the
   server (or the planner refused the diff); show the detail and offer
   remove-and-deploy. `callback_device_stale` on the record (and the
   `callback_device_stale` server event) means the hub lost the device;
   offer `POST /callback-device/redeploy`.

Inside a container on a bridge network the hubs cannot reach the
server's own address; the operator sets `--callback-host` to the Docker
host's LAN address and publishes the callback port. Show
`effective_destination` from the record when a deploy produces no
presses, and the listener state from `GET /api/v1/server` when
`callback_listener.bound` is false (the port is usually taken by a Home
Assistant install or Emulated Roku on the same host).
