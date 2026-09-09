# sofabaton-x-server

REST + WebSocket server over the [sofabaton-x](../sofabaton-x/README.md)
library for **Sofabaton X1 / X1S / X2** hubs, with an OpenAPI document
meant for client generators. It is what an automation platform
integration (Homey, Hubitat, openHAB, ...) talks to; the server owns
persistence, discovery policy and network exposure, the library owns the
hub protocol.

Status: **0.1.0**, the v1 scope: hub records with enable/disable,
reads and control, the event stream, discovery and the server's own
advertisement, packaging. Live-validated against an X1 and an X1S.
Writes (editing hub configuration, provisioning, IR learning) come with
the library's next phase.

> Unofficial; not affiliated with or endorsed by Sofabaton.

## Run

```
pip install sofabaton-x-server
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

`network_mode: host` is not optional: mDNS, the iOS app's UDP broadcast
and the hub's TCP dial-back all need the host's real interface, which a
bridged container network does not provide. Docker Desktop on Windows
and macOS cannot do host networking; run the server on a Linux host (a
Raspberry Pi, a NAS, a small box). `/data` holds `hubs.json` and
`server.json`.

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

Mounting under a prefix (`https://home.example/sofabaton/`): add
`--root-path /sofabaton` and include the prefix in the advertised URL.

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
| `--log-level` | `SOFABATON_LOG_LEVEL` | `info` | |

`--print-settings` prints the effective settings and exits.

## Security

**No authentication in v1.** The server is a LAN service in the same
class as the hub protocol it fronts: anyone who can reach the port can
read the catalogs and send commands. Do not expose it beyond your LAN;
never through NAT or an internet-facing reverse proxy without the proxy
adding authentication. Every control call is logged with the caller's
address.

## API

`GET /api/v1/server` identifies the server. The OpenAPI document is at
`/api/v1/openapi.json` (interactive docs at `/api/v1/docs`). Every
operation has a stable `operationId` and every schema is a named
component, so generated clients stay readable.

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
set, `base_url`. A platform that found the server should use `base_url`
when present, else `http://<SRV host>:<SRV port>` plus `path`.

## Events (WebSocket)

`ws://<server>:8480/api/v1/events` streams every hub's events on one
connection; add `?hub_id=<id>` (repeatable) to narrow it. Messages are
JSON objects discriminated by `type`:

| type | payload |
| --- | --- |
| `hello` | once on connect: `server_version`, `api_version`, `hubs` (`hub_id`, `enabled`) |
| `hub_event` | `hub_id` and the library `event` (`seq`, `kind`, `payload`): `activity_changed`, `activity_list_updated`, `hub_state`, `app_state`, `status_changed`, `catalog_ready`, `ota` |
| `server_event` | `hub_id` and `kind`: `hub_added`, `hub_removed`, `hub_enabled`, `hub_disabled`, `hub_rekeyed` |
| `dropped` | `count` of older messages discarded because this client fell behind; sent before the next message that gets through |

`seq` is the library's per-hub counter and passes through untouched, so
a gap means the hub's own consumer queue overflowed inside the server.
`hub_rekeyed` is the one to watch after registering by host: the id
becomes the hub's MAC once its banner is read. Disabling a hub is
announced by `hub_disabled` alone (its proxy is gone before any link
event could be relayed); enabling it starts a fresh session whose `seq`
begins at 1 again. Inbound text is ignored.
The message types are published as components in the OpenAPI document
(`WsHello`, `WsHubEvent`, `WsServerEvent`, `WsDropped`) for generators.

Writing a platform integration? Start with
[docs/platform-integration.md](docs/platform-integration.md): finding
the server and the hubs, the endpoint rule, the error table, the event
stream, and a pairing flow that feels right.

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
PYTHONPATH=sofabaton-x-server/src python -m sofabaton_server.openapi
```

The codegen smoke (also in CI) proves the document feeds a generator and
the generated types are usable:

```
npx -y openapi-typescript@7 sofabaton-x-server/openapi.json -o sofabaton-x-server/codegen-smoke/schema.d.ts
npx tsc --noEmit -p sofabaton-x-server/codegen-smoke/tsconfig.json
```

Releases: bump `__version__` in `src/sofabaton_server/__init__.py`, tag
`sofabaton-x-server-vX.Y.Z`, and the release workflow publishes to PyPI
(the library must already be published at a matching `sofabaton-x`
version).
