# sofabaton-x-server

> **Unreleased — in development.** The server has not had its first release
> and needs further work before release. This documentation describes the
> current development API.

REST + WebSocket server over the [sofabaton-x](../sofabaton-x/README.md)
library for **Sofabaton X1 / X1S / X2** hubs, with an OpenAPI document
meant for client generators. It is what an automation platform
integration (Homey, Hubitat, openHAB, ...) talks to; the server owns
persistence, discovery policy and network exposure, the library owns the
hub protocol.

The development version is **0.2.0**, built against sofabaton-x 0.2.x.
It supports hub discovery and
management, reads and control, configuration editing, IR payloads, and
backup / restore / erase.

> Unofficial; not affiliated with or endorsed by Sofabaton.

## Run

Install both development packages from the repository root (Python 3.11+):

```
python -m pip install . ./sofabaton-x-server
sofabaton-x-server --hub 192.168.1.50
```

The server must sit on the same network segment as the phones running
the official app (mDNS and UDP broadcast); in Docker that means host
networking on a Linux host. Ports on the host: TCP 8200 (hub connect-
back, shared by all hubs), UDP 8102 (app discovery), UDP 5353 (mDNS),
and the API port.

### Docker

Build from the repository root (both distributions come from one repo)
or use the compose file next to this README:

```
docker build -f sofabaton-x-server/Dockerfile -t sofabaton-x-server .
docker run -d --name sofabaton-x-server --network host -v ./data:/data \
  -e SOFABATON_HUBS=192.168.1.50 sofabaton-x-server
```

```
cd sofabaton-x-server && docker compose up -d
```

Use Docker on Linux with `network_mode: host` so mDNS, the app's UDP
broadcast and the hub's TCP dial-back can reach the LAN interface.
Callback devices (Button events below) need the hubs to reach the
server's callback listener. On host networking nothing is needed. On a
bridge network the server's own address is the container's, which the
hubs cannot reach: set `SOFABATON_CALLBACK_HOST` to the Docker host's
LAN address and publish the callback port (`-p 8060:8060`). The
server cannot detect this itself; it shows the address it will use as
`effective_destination` on the callback device record and on
`GET /api/v1/server`.
The supplied compose file configures this. `/data` holds `hubs.json`,
`server.json` and one `state-<hub_id>.json` per hub (the library's cache
document; see Snapshot below).

### Behind a reverse proxy (TLS)

Terminate TLS in a reverse proxy; that is where certificates are
manageable. Three things to set on the server, then a snippet per proxy.

- `--advertise-url https://sofabaton.home.example`: what clients must
  use. Published in the mDNS TXT record as `base_url` and as the OpenAPI
  document's server URL, so discovery and generated clients both point
  at the proxy.
- `--trusted-proxy 127.0.0.1` (or the proxy's address): honours
  `X-Forwarded-*` from that source, so control-call logs show the real
  client and redirects keep the public scheme.
- `--bind 127.0.0.1` when the proxy runs on the same host, so plain
  HTTP is not also reachable directly.

Caddy (WebSocket upgrade is automatic):

```
sofabaton.home.example {
    reverse_proxy 127.0.0.1:8480
}
```

nginx:

```
location / {
    proxy_pass         http://127.0.0.1:8480;
    proxy_http_version 1.1;
    proxy_set_header   Host              $host;
    proxy_set_header   X-Forwarded-For   $proxy_add_x_forwarded_for;
    proxy_set_header   X-Forwarded-Proto $scheme;
    # /api/v1/events is a WebSocket:
    proxy_set_header   Upgrade           $http_upgrade;
    proxy_set_header   Connection        "upgrade";
    proxy_read_timeout 3600s;
}
```

Mounting under a prefix (`https://home.example/sofabaton/`): configure the
proxy to strip `/sofabaton` when forwarding, add `--root-path /sofabaton`,
and set `--advertise-url https://home.example/sofabaton`. The API then lives
at `https://home.example/sofabaton/api/v1`; do not include `/api/v1` in
`--advertise-url`.

Exposing the server beyond the LAN through a proxy means the **proxy
must add authentication** (forward-auth or basic auth): the server has
none in v1 and ignores authorization headers, so any scheme works.

## Settings

Defaults, then `server.json` in the data directory, then environment
variables, then flags; each layer overrides the one before.

| flag | environment | default | meaning |
| --- | --- | --- | --- |
| `--bind` | `SOFABATON_BIND` | `0.0.0.0` | address to listen on |
| `--port` | `SOFABATON_PORT` | `8480` | API port |
| `--data-dir` | `SOFABATON_DATA_DIR` | `./data` | `server.json`, `hubs.json` |
| `--hub HOST` (repeatable) | `SOFABATON_HUBS=a,b` | none | hubs registered on first start, only while `hubs.json` is empty |
| `--advertise-url` | `SOFABATON_ADVERTISE_URL` | none | public base URL behind a reverse proxy; published as mDNS TXT `base_url` and as the OpenAPI `servers[0].url` |
| `--root-path` | `SOFABATON_ROOT_PATH` | none | path prefix a reverse proxy mounts the API under |
| `--trusted-proxy ADDR` (repeatable) | `SOFABATON_TRUSTED_PROXIES=a,b` | none | sources whose `X-Forwarded-*` headers are honoured |
| `--tls-cert` / `--tls-key` | `SOFABATON_TLS_CERT` / `_KEY` | none | bring your own certificate (a reverse proxy is the usual way) |
| `--callback-host` | `SOFABATON_CALLBACK_HOST` | routed local IP per hub | IPv4 address the hubs call back on for callback devices (see Button events); set the host's LAN address inside a container on a bridge network |
| `--callback-port` | `SOFABATON_CALLBACK_PORT` | `8060` | port of the callback listener; the X1 can call no other |
| `--log-level` | `SOFABATON_LOG_LEVEL` | `info` | |

`--print-settings` prints the effective settings and exits.

## Security

**No authentication in v1.** The server is a LAN service in the same
class as the hub protocol it fronts: anyone who can reach the port can
read the catalogs, send commands and change the hub's
configuration, including `POST /hubs/{id}/erase` and a replacing
restore. Do not expose it beyond your LAN; never through NAT or an
internet-facing reverse proxy without the proxy adding authentication.
Control calls are logged with the caller's address. Configuration writes
run as jobs whose records name the operation; transient control and IR
play calls return their acceptance immediately.

## API

`GET /api/v1/server` identifies the server. The OpenAPI document is at
`/api/v1/openapi.json` (interactive docs at `/api/v1/docs`). Every
operation has a stable `operationId`, and public models are named
components. Job results and editable entity tables contain open objects;
clients must interpret them according to the operation. Errors are one shape,
`Problem` (`type`, `title`, `status`, `detail`, `hub_id`, `mode`).

Paths below beginning `/hubs` are relative to the API root `/api/v1`.
For an executable refresh/preview/apply workflow, see
[the integration guide](docs/platform-integration.md#8-complete-edit-workflow).

## Snapshot

`GET /api/v1/hubs/{id}/snapshot` is the hub's structural configuration
(devices, activities, commands, bindings, macros, favorites; no IR
payloads) projected from the library's cache with **no hub traffic**.
`snapshot_id` identifies the configuration content and is stable when that
content survives a restart. Send this revision, quoted, as `If-Match` when
editing. The response's HTTP `ETag` is an opaque cache validator: retain it
verbatim for `If-None-Match`, which returns 304 only when the whole
representation is unchanged. Do not assume the ETag and configuration
revision are equal; provenance (`fetched_at`, `complete`, `editable`)
changes the ETag without changing the configuration revision, so a poll
that gets 200 with the same `snapshot_id` is a provenance change.
Every entity carries `complete`,
`editable` and `fetched_at`. These describe the server's copy, not the
hub: the hub can be edited outside this server at any time (the vendor
app, another client) and does not say so, so the server gives no
freshness verdict. Show `fetched_at` and offer a refresh; the
`app_state` event with `connected: false` (a vendor-app session through
the proxy just ended) is one good moment for that offer.

The library reads initial catalogs automatically when the hub connects.
`POST /hubs/{id}/snapshot/refresh` with
`{"device_id": 5}` or `{"activity_id": 101}` re-reads one entity; an
empty body re-reads the whole hub, which can take tens of seconds to
minutes depending on its configuration. Treat it as a user action.
Detailed refreshes are explicit; backups and write reconciliation also
read hub data. The server saves the library's state document on
`snapshot_changed` and when stopping a hub, and imports it before starting
the hub. This preserves previously fetched detail and its completeness
flags. A partial cache remains partial; absent or unreadable state starts
cold. Check `editable` before editing rather than assuming a restart made
the snapshot complete.

## Jobs

Anything that holds the hub for more than a moment answers `202` with a
job record: structural refresh, configuration writes, learn, backup,
restore and erase. Follow
it on the event stream (`job_event` messages carry the full record:
`status`, the last `progress`, the `result` or a `Problem` in `error`)
or poll `GET /hubs/{id}/jobs/{job_id}`; `GET /hubs/{id}/jobs` lists
recent ones. One job runs per hub at a time (`409 hub_job_running`). Reads
are not rejected merely because a job runs, but a read that needs hub
traffic can wait or fail; keep the hub idle during IR learning.
`DELETE /hubs/{id}/jobs/{job_id}` cancels a
whole-hub refresh (between entities) or a learn; writes, backup and
restore run to completion, and disabling or removing a hub is refused
while a job holds it. Cancellation may remain pending until the current
entity finishes; repeating the request is accepted and changes nothing.
Wait for the terminal job status before starting another operation.
A graceful stop waits for running writes, with a bounded timeout.

`202` means accepted, not successful. Terminal states are `done`, `failed`
and `cancelled`. On failure inspect both `error` and `result`, which may
describe partial changes. Job records are in memory and only recent ones
are retained; after a server restart, reconcile against a fresh snapshot
rather than assuming a lost job succeeded or failed.

## Writes

Two shapes, both jobs:

- **Intents** say what to change and the server derives the edit from
  the current snapshot: `POST .../activities/{aid}/rename`, `PUT
  .../activities/{aid}/buttons/{button}` (a code or a `ButtonName`
  alias such as `VOL_UP`, with an optional long press) and `DELETE` on
  the same path, `POST` / `DELETE` / `PUT .../favorites[/order]`,
  `POST .../devices/{did}/rename`, `POST .../commands/{cid}/rename`,
  `PUT .../devices/{did}/idle-behavior`; and the whole-entity ones:
  `POST /devices` (empty device of a class the hub can create), `POST
  /activities`, `DELETE /devices/{did}`, `DELETE /activities/{aid}`,
  `PUT /devices/order`, `PUT /activities/order`, `PUT /hubs/{id}/name`.
  `If-Match` is optional and honours the snapshot's `snapshot_id` revision.
- **Row edits** for an editor that works on the document: change one
  `activities[]` or `devices[]` element of the snapshot, preview with
  `POST .../plan`, then `PUT` it back with `If-Match` (required: `428`
  without it, `412` when the snapshot moved). Only the named entity may
  differ from the snapshot (`422 out_of_scope`).

`If-Match` compares the cached configuration revision. Sync-based row edits
and intents also re-read the target entity before writing: a changed live
baseline fails with `sync_failed` at `stale_check`. Whole-entity operations
(create, delete, reorder, hub rename, restore) use their own validation;
they do not all perform this live baseline comparison. Configuration writes
are refused up front while an app holds the hub (`409 hub_busy`).

## IR payloads, backup, restore

A code in any format your platform has (`{"pronto": ...}`,
`{"descriptor": "P:NEC1 D:4 S:5 F:21"}`, `{"timings_us": [...],
"carrier_hz": 38000}`, or the hub's own `{"hex": ...}`) can be fired
once with `POST /hubs/{id}/play`, saved as a new command with `POST
.../devices/{did}/commands`, or written over an existing one with `PUT
.../commands/{cid}/payload`; `GET .../commands/{cid}/payload` reads what
the hub holds. `POST /hubs/{id}/learn` arms the hub's receiver and
returns the captured code as the job result.

`POST /hubs/{id}/backup` returns a full, restorable bundle as the job
result (minutes; keep it as a file). `POST /hubs/{id}/restore` with
`{"bundle": ..., "replace": true}` erases first and then writes the
bundle back. The bundle and its entity references are validated before erase.
With `replace` omitted or false, restore is additive and assigns new ids.
Structural snapshots and backups made with `include_blobs: false` are not
restorable. Keep the complete full-backup bundle, not just its job header.

`POST /hubs/{id}/erase` and a replacing restore are whole-hub destructive
operations. Device/activity deletion and payload replacement can also remove
existing configuration. A failed restore is not rolled back: inspect its
result (`failed_at`, restored counts, `device_id_map`, `snapshot_id`) and the
current snapshot before recovery. Automatically retrying an additive restore
can create duplicates. If a write request times out, check the hub's jobs
before submitting it again.

## Button events

The hubs never report presses of IR or Bluetooth commands, but a Wifi
device's commands call an address when pressed. The server turns that
into button events: it deploys a **callback device** on a hub, a managed
Wifi device whose commands call the server's own listener, and relays
every press to your platform.

```
POST /hubs/{id}/callback-device        {"name": "Server", "slots": [{"label": "Play"}, {"label": "Pause"}]}
GET  /hubs/{id}/callback-device        the record: device_id, labels, target, stale, effective_destination
PUT  /hubs/{id}/callback-device        rename slots or change the power / input hooks in place (a job)
DELETE /hubs/{id}/callback-device      remove it from the hub and forget it (409 while activities reference it; ?force=true)
POST /hubs/{id}/callback-device/redeploy   deploy a stale one again from its stored spec
GET  /hubs/{id}/presses?after=<seq>    the catch-up view of the press stream
GET  /server/callback-listener         the listener's state; POST .../retry tries to bind it now
```

Every deploy writes all ten slots (unnamed ones are `Button n`), each as
a short and a long press record: command ids `1..10` and `11..20`. Bind
them like any command with the generic routes (`PUT
/activities/{aid}/buttons/{button}`, favorites, activity membership); an
in-place update never touches those bindings. On the X1S and X2,
`power_on_slot` / `power_off_slot` fire when an activity powers on or
off and `input_slots` are offered as activity-start inputs; the X1
ignores both (its firmware fires one power and one input callback per
transition regardless) and always calls port 8060.

Presses arrive as `press` messages on `/events` and in `GET
/hubs/{id}/presses`. Both carry the same `seq`, a counter of this server
instance; de-duplicate across the two channels by it, and after a
reconnect or a `dropped` message fetch `?after=<last seq you saw>`.
`expired: true` means presses newer than that were already evicted from
the ring (100 per hub); accept the gap. The `hello` message and `GET
/server` carry an `instance_id`: when it changes the server restarted,
the ring is empty and the sequence started over. `resolution` says how a
press matched the record: `deployed`, `stale` (the record is flagged
stale, see below), `unknown_slot`, `unknown_device`; nothing is dropped.

The listener is a separate plain-HTTP port (8060 by default, the same
default as the Home Assistant integration and Emulated Roku, so only
one of them can own it on a host). It runs while any hub has a callback
device, accepts only the hub's own address (or the forwarded client when
the peer is a `--trusted-proxy`) and answers every request at once; the
hub retries anything it dislikes. A port in use is not fatal: the deploy
still succeeds, `callback_listener_failed` is announced, `GET
/server/callback-listener` shows the error and the next retry, the
server keeps retrying with backoff, and `POST
/server/callback-listener/retry` tries at once.

Failures split two ways. An immediate `409` is something the record
alone decides: `callback_device_exists`, `callback_device_stale`,
`callback_device_not_stale`, `callback_device_referenced` (the detail
names the activities and reference kinds), `callback_port_x1`. Anything
that needs the hub happens inside the accepted job and fails it with a
coded error: `callback_update_declined` (a record's label matches
neither what was deployed nor what you asked, so the device was edited
elsewhere; or the planner refused the diff; nothing was written) and
`callback_update_failed` (the hub rejected a step; the next update with
the same spec resumes).

If the device disappears from the hub (deleted in the Sofabaton app, an
erase), the record is marked `stale` (`callback_device_stale` server
event), presses that still arrive are tagged `resolution: "stale"`, and
`redeploy` creates it again from the stored spec. The server verifies
identity (brand, name and the callback path inside the first record)
before it clears the flag on its own. Every create, update and delete
writes its intent to `hubs.json` before the hub is touched; at boot and
before every deploy the server reconciles it, and a device it created
but forgot (a crash before the save, a lost data directory) is adopted
by that same identity check instead of being created twice
(`adopted: true` on the record).

## Discovery

The server browses for hubs for as long as it runs and keeps a table of
what it has seen: `GET /api/v1/discovery/hubs` lists physical hubs
(`key` is the MAC when the advertisement carries one, else the host),
whether each is currently advertised (`present`), and the configured
hub it matches (`registered_hub_id`) if any. `POST /api/v1/discovery/scan`
with `{"timeout": 5}` listens for that long and returns the table, for
platforms that want a synchronous answer. Advertisements from this
server's own proxies are recognised and left out. New and vanished hubs
arrive on the event stream as `hub_discovered` / `hub_lost`.

To register a discovered hub, `POST /api/v1/hubs` with the entry's
`config` object. A record from your platform's own mDNS stack works the
same way: pass `host`, and `mac`, `name`, `txt` and `hub_version` when
you have them; filter out advertisements carrying `HA_PROXY=1` (they
are proxies, and the server refuses them with a pointer to the hub they
front).

The server advertises itself as `_sofabaton-x._tcp.local.` with TXT
`version`, `api`, `path`, `hubs` (count) and, when `--advertise-url` is
set, `base_url`. Use `base_url` when present, otherwise
`http://<SRV host>:<SRV port>`, as the **server base URL**. Append `path` to
obtain the **API root** for hand-written calls. Generated clients use the
server base URL because OpenAPI operation paths already include `/api/v1`.
Preserve a reverse-proxy prefix and avoid appending `/api/v1` twice.

## Events (WebSocket)

`ws://<server>:8480/api/v1/events` streams every hub's events on one
connection; add `?hub_id=<id>` (repeatable) to narrow it. Messages are
JSON objects discriminated by `type`:

| type | payload |
| --- | --- |
| `hello` | once on connect: `server_version`, `api_version`, `hubs` (`hub_id`, `enabled`) |
| `hub_event` | `hub_id` and the library `event` (`seq`, `kind`, `payload`): `activity_changed`, `activity_list_updated`, `hub_state`, `app_state`, `status_changed`, `catalog_ready`, `snapshot_changed`, `ota` |
| `server_event` | `hub_id` and `kind`: `hub_added`, `hub_removed`, `hub_enabled`, `hub_disabled`, `hub_rekeyed`, `hub_discovered`, `hub_lost` |
| `job_event` | `hub_id` and the full `job` record on every transition: queued, running, each progress report, done / failed / cancelled |
| `press` | a button press the hub delivered to the callback listener: `seq` (the server-instance press sequence, shared with `GET /hubs/{id}/presses`), `hub_id`, `device_id`, `command_id`, `slot`, `label`, `press_type` (`short` / `long`), `resolution`, `transport`, `source`, `received_at` (see Button events) |
| `dropped` | `count` of older messages discarded because this client fell behind; sent before the next message that gets through |

`seq` is the library's per-hub counter and passes through untouched, so
a gap means the hub's own consumer queue overflowed inside the server.
`hub_rekeyed` is the one to watch after registering by host: the id
becomes the hub's MAC once its banner is read. Disabling a hub is
announced by `hub_disabled` alone (its proxy is gone before any link
event could be relayed); enabling it starts a fresh session whose `seq`
begins at 1 again. On reconnect, a `dropped` message or a sequence gap,
re-read the hub list, status, relevant snapshots and outstanding jobs;
events have no replay history. Inbound text is ignored.
The message types are published as components in the OpenAPI document
(`WsHello`, `WsHubEvent`, `WsServerEvent`, `WsJobEvent`, `WsDropped`)
for generators.

Writing a platform integration? Start with
[docs/platform-integration.md](docs/platform-integration.md): finding
the server and the hubs, the endpoint rule, the error table, the event
stream, a pairing flow that feels right, and the snapshot, job and
editing flows.

## Development

From the repository root, with the library importable (the tests alias
the in-tree library automatically):

```
pip install fastapi "uvicorn[standard]" httpx pytest
pytest sofabaton-x-server/tests -q
```

`openapi.json` is the committed contract; a test fails when the running
app's document differs. After an API change:

```
pip install -r sofabaton-x-server/openapi-toolchain.txt
PYTHONPATH=sofabaton-x-server/src python -m sofabaton_server.openapi
```

The toolchain file pins the FastAPI and pydantic versions the document
is generated with; CI installs the same set before the drift test, so a
framework's own wording (the 422 description changed between FastAPI
releases, for instance) never shows up as API drift.

The codegen smoke (also in CI) checks generation and type-checks the sample
client:

```
npx -y openapi-typescript@7 sofabaton-x-server/openapi.json -o sofabaton-x-server/codegen-smoke/schema.d.ts
npx tsc --noEmit -p sofabaton-x-server/codegen-smoke/tsconfig.json
```

When ready for the first release, set `__version__` in
`src/sofabaton_server/__init__.py` and tag `sofabaton-x-server-vX.Y.Z`.
The release workflow publishes to PyPI; a compatible `sofabaton-x` version
must be published first.
