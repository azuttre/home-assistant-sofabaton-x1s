# sofabaton-x — Python Library

> **Breaking changes in the upcoming 0.2.0 release.** This README describes
> the unreleased API. Existing 0.1.x consumers should read the
> [changelog and migration guide](https://github.com/m3tac0de/home-assistant-sofabaton-x1s/blob/main/sofabaton-x/CHANGELOG.md#unreleased--020)
> before upgrading.

[![PyPI](https://img.shields.io/pypi/v/sofabaton-x)](https://pypi.org/project/sofabaton-x/)
[![Python versions](https://img.shields.io/pypi/pyversions/sofabaton-x)](https://pypi.org/project/sofabaton-x/)
[![License: MIT](https://img.shields.io/pypi/l/sofabaton-x)](https://github.com/m3tac0de/home-assistant-sofabaton-x1s/blob/main/LICENSE)

Unofficial Python library for **Sofabaton X1 / X1S / X2** universal remote
hubs: a reverse-engineered protocol implementation and a man-in-the-middle
**proxy** that sits between the hub and the official mobile app.

This is the protocol engine extracted from the
[Home Assistant Sofabaton X integration](https://github.com/m3tac0de/home-assistant-sofabaton-x1s);
the integration is its reference consumer.

> **Disclaimer:** this project is not affiliated with or endorsed by
> Sofabaton. The protocol was reverse-engineered from network captures;
> behavior may break with future hub firmware.

## ◇ What it does

- **Proxy** a physical hub: the library advertises itself via mDNS exactly
  like a real hub, the official app connects to it, and every frame is
  relayed, decoded and observable. The hub keeps working with the app while
  your application gets full visibility and control.
- **Catalogs**: read activities, devices (with live power state),
  buttons, commands, macros and favorites as typed results.
- **Control**: send button/command presses, switch activities, trigger
  find-my-remote.
- **Status and events**: typed connection status and hub identity, and
  one event stream for activity changes, catalog updates, hub and app
  link state, OTA and mode flips. The hub's own activity-state MQTT
  publishes (X2) can be fed back in as an external state source.
- **Configuration as data**: one hub record for every intake path,
  whether the library discovered the hub, a foreign mDNS stack did, a
  user typed an address, or it arrived as a REST body.
- **Backup / restore**: export and restore hub configuration, including
  provisioning devices of any class from a hand-built bundle.
- **Snapshots and live editing**: project the hub configuration from the
  cache at no hub cost, diff an edited copy against it and sync the
  difference to the hub as targeted in-place writes
  (activity- or device-scoped), with a pure plan builder for dry-run
  previews.
- **IR payloads**: play a raw payload once, learn a code from a physical
  remote through the hub's IR receiver, and convert between the hub's
  stored formats, Pronto hex and raw timings through `IrPayload` and the
  async facade. Save or replace payloads through the edit helpers and sync.

Deliberately **out of scope**: executing the HTTP or MQTT callbacks that
network-class devices define (e.g. a Roku-style ECP listener). The
library carries the protocol artifacts for those features so applications
can build them on top — the Home Assistant integration does exactly that.

## ◇ Install

For the unreleased API documented here, install from the repository root:

```
python -m pip install .
```

For existing applications using the released 0.1.x API, stay on that series
until you have migrated:

```
python -m pip install "sofabaton-x>=0.1,<0.2"
```

Python 3.11+. The only dependency is
[python-zeroconf](https://pypi.org/project/zeroconf/) (mDNS advertising and
hub discovery).

## ◇ Quickstart

Find a hub, then proxy it. Blocking work runs in the event loop's
executor and callbacks (plain functions or coroutines) are delivered on
the loop, so application code never touches the engine threads:

```python
import asyncio
from sofabaton import AsyncXProxy, async_discover_hubs

async def main():
    hubs = await async_discover_hubs(timeout=5.0)   # physical hubs; proxies filtered
    if not hubs:
        print("No Sofabaton hub found on this network.")
        return
    hub = hubs[0]

    proxy = AsyncXProxy(hub_ip=hub.host)   # the hub's IP is all you need
    proxy.on_activity_change(lambda new, old, name: print(f"activity -> {name}"))

    async with proxy:
        if not await proxy.wait_until_controllable():   # own the hub (see below)
            raise RuntimeError("Hub did not become controllable")

        for act in await proxy.activities():           # list[Activity]
            print(f"activity {act.activity_id}: {act.name}")

        for dev in await proxy.devices():              # list[Device]
            for cmd in await proxy.commands(dev.device_id):   # list[Command]
                print(f"device {dev.device_id} ({dev.name}): "
                      f"command {cmd.command_id} = {cmd.label}")

        # Fires one real command — command 5 on device 1. Pick your own
        # (entity_id, command_id) pair from the listing printed above.
        await proxy.send(1, 5)

asyncio.run(main())
```

`hub_ip` is the only required argument; everything else has a sensible
default and the hub model is confirmed from the connect banner. The one
thing worth adding is the proxy's mDNS identity — pass
`mdns_instance=hub.name` and `mdns_txt=hub.txt` so the proxy advertises
itself **exactly like the hub it fronts**, letting the official Sofabaton
app keep working while pointed at the proxy. Skip them and the proxy still
reads and controls the hub fine; it just advertises under a generic name:

```python
proxy = AsyncXProxy(
    hub_ip=hub.host,
    mdns_instance=hub.name,   # advertise as the hub, so the app finds the proxy
    mdns_txt=hub.txt,         # carries HVER -> X1/X1S/X2 classification
)
```

### Configuration as data

Every way a hub can reach your application produces the same record, a
`HubConfig` dataclass that round-trips through a plain dict (a REST body,
a config file) and builds the proxy:

```python
from sofabaton import AsyncXProxy, HubConfig

cfg = HubConfig(host="192.168.1.50")                    # manual entry: host is enough
cfg = HubConfig.from_discovered(hub)                    # from async_discover_hubs / HubBrowser
cfg = HubConfig.from_advertisement(                     # from a foreign mDNS stack's record
    service_type, instance_name, host=host, port=port, properties=txt_properties,
)
cfg = HubConfig.from_dict(json_body)                    # from a REST body or config file

proxy = AsyncXProxy.from_config(cfg)                    # same as AsyncXProxy(**cfg.proxy_kwargs())
```

`from_advertisement` accepts what mDNS libraries hand out (bytes or str
keys and values) and raises `ValueError` for anything that is not a
Sofabaton hub advertisement. An unrecognised `HVER` does not reject the
record: `hub_version` stays `None` and the connect banner settles it.
`is_proxy` is `True` when the record describes one of *your own* proxy
advertisements (they mimic hubs by design and carry the `HA_PROXY` TXT
key); use it to map such a record back to the hub you already front
instead of proxying a proxy. `source` (`"server"`, `"client"`,
`"manual"`) is informational.

### Ports

The proxy has two network faces. Apart from `hub_ip`, every port defaults
to the right value — you usually only touch `hub_listen_port` to avoid a
local collision:

| Argument             | Default | Side | What it is                                                                |
| -------------------- | ------- | ---- | ------------------------------------------------------------------------- |
| `hub_ip`             | —       | hub  | the physical hub's IPv4 address                                           |
| `hub_port`           | 8102    | hub  | UDP port **on the hub** we send `CALL_ME` to (protocol-fixed)             |
| `hub_listen_port`    | 8200    | hub  | TCP port **on this host** the hub connects back to                        |
| `app_discovery_port` | 8102    | app  | UDP port **on this host** the app finds + calls us on (keep 8102 for iOS) |

The hub model (X1/X1S/X2) is confirmed from the connect banner, so
`hub_version` is only a pre-connect hint. See
[`docs/networking.md`](https://github.com/m3tac0de/home-assistant-sofabaton-x1s/blob/main/docs/networking.md)
for the complete port map and firewall guidance.

Everything is keyed on **`(entity_id, command_id)`** — you browse to get
those ids, then `send(entity_id, command_id)`. The reads return typed
dataclasses (each with a `to_dict()`), cached if available, else fetched:

| read                     | returns                                                                                  |
| ------------------------ | ---------------------------------------------------------------------------------------- |
| `activities()`           | `list[Activity]`: `activity_id`, `name`, `active`, `needs_confirm`                       |
| `devices()`              | `list[Device]`: `device_id`, `name`, `brand`, `device_class`, `device_class_code`, `power_state`, `idle_behavior` |
| `commands(device_id)`    | `list[Command]`: `command_id`, `label`                                                   |
| `macros(activity_id)`    | `list[Macro]`: `command_id`, `label`                                                     |
| `favorites(activity_id)` | `list[Favorite]`: `device_id`, `command_id`, `label`                                     |
| `buttons(entity_id)`     | `list[Button]`: `button_code`, `name`, `device_id`, `command_id`                         |
| `current_activity()`     | `{activity_id, name}` or `None` when idle                                                |

Lists are sorted by id. `Device.power_state` is the hub's live power byte
(0 off, 1 on) as of the last devices fetch, or `None` when the row carried
no parseable record; the hub commits it with a short lag after a power
command, so it is not an instantaneous read. `activities(refresh=True)`
and `devices(refresh=True)` re-read the list from the hub (fresh power
bytes included). The engine is fetch-then-prune: a refresh that cannot
be issued or never lands raises the typed error and the cached list
stays readable; nothing is cleared first.

`current_activity()` is the exception to the table above — it reads the
hub's **live** running-activity state (no fetch) and works in observe mode
too; subscribe to changes with `on_activity_change(cb)`.

Two typed status reads sit beside the catalog reads, both returning
dataclasses with a `to_dict()`:

| status read                | returns                                                                                              |
| -------------------------- | ---------------------------------------------------------------------------------------------------- |
| `status()`                 | `HubStatus`: `hub_connected`, `app_connected`, `controllable`, `mode`, `hub_version`, `proxy_enabled`, `running_activity`, cached counts, `catalog_ready` |
| `hub_info(refresh=False)`  | `HubInfo`: `known`, `model`, `name`, `mac`, `firmware_version`, `production_batch` (from the connect banner) |

`status()` is a pure state read and works in every mode; `mode` is
`"disconnected"`, `"observe"` or `"control"` and explains why a send was
refused. `hub_info()` serves the banner known from the session and only
re-reads it on `refresh=True`, which needs control mode.

A read that has to fetch and cannot raises a typed error: `HubBusyError`
(an app holds the hub), `HubNotConnectedError` (no hub session), or
`FetchTimeoutError` (the reply never landed). They subclass `RuntimeError`
and `TimeoutError`, so existing `except` clauses keep working.

Control: `send(entity_id, command_id)` (alias `press`),
`start_activity(act)`, `stop_activity(act)`, `find_remote()`.

### Events

Every engine listener is also available as one typed stream:

```python
async for event in proxy.events():          # HubEvent(seq, kind, payload)
    print(event.seq, event.kind, event.to_dict()["payload"])
```

| kind                    | payload                                            |
| ----------------------- | -------------------------------------------------- |
| `activity_changed`      | `ActivityChanged`: `activity_id`, `previous_activity_id`, `name` (`activity_id` is `None` when the hub powered off) |
| `activity_list_updated` | none (re-read `activities()`)                      |
| `hub_state` / `app_state` | `ConnectionState`: `connected`                   |
| `status_changed`        | `StatusChanged`: `mode`, `previous_mode` (derived; fires once per mode flip) |
| `catalog_ready`         | `CatalogReady`: `ready` (the connect-time initial sync finished, or the session dropped) |
| `snapshot_changed`      | `SnapshotChanged`: `snapshot_id`, `engine_generation`, `device_ids`, `activity_ids` (a refresh landed, a write was rebased, or the cache was imported) |
| `ota`                   | none (the hub goes silent for a few minutes)       |

Each consumer gets its own bounded queue (`maxsize=256` by default). A
consumer that falls behind loses the oldest events rather than stalling
the engine; drops are counted in `proxy.events_dropped` and show up as a
gap in `seq`. The `on_*` registrations keep working alongside the stream.

### Two modes

The proxy sits transparently between the hub and the official app, which
gives it two distinct modes:

- **Observe** — the app is connected through the proxy. You watch
  activity changes (`current_activity()` / `on_activity_change`), connects
  and OTA events in real time, but the app owns the hub, so you can't issue
  commands. Gate on `await proxy.wait_connected()`.
- **Control** — no app attached; the proxy owns the hub, so reads fetch
  fresh and commands/backup work. Gate on
  `await proxy.wait_until_controllable()`.

`start()` only spawns the transport; the connect handshake happens
afterwards, so await the matching readiness primitive before reading or
acting (otherwise a read raises with the reason — hub not connected, or
an app holds it).

Every time the hub connects, the proxy also runs a small **initial
sync** on its own: it reads the connect banner, the device list and the
activity list, so that minimum is always cached for the session (and
`hub_info()` / `activities()` / `devices()` never need a fetch of their
own afterwards). `await proxy.wait_until_ready()` resolves once that has
happened; `status().catalog_ready` mirrors it and the `catalog_ready`
event announces it. The sync needs control mode, so with an app already
attached it runs as soon as the app lets go. Pass `initial_sync=False`
to the constructor to opt out (an application that runs its own
connect-time sync, as the Home Assistant integration does).

To take a hub out of service while keeping its configuration (so the
official app can talk to it directly again), stop the proxy with
`await proxy.stop(release_hub=True)`. A dropped hub keeps dialling the
shared connect-back port for as long as that port is open for other
hubs, and while it dials it does not advertise itself; it only gives up
on a refused connection. The release bounces the shared listener (the
listening socket closes for a short window and reopens) and, for a
grace period, bounces again whenever that hub dials back, so one of its
own retries is guaranteed to meet a closed port. Accepted sessions are
untouched, so every other hub stays connected straight through; only
new dial-backs are refused during a window. With no other hub
registered the port simply closes and the release is a no-op. A plain
`stop()` is for shutdown.

The mode is not fixed at startup — it follows the app. If the official
app connects while you hold control, you are demoted to observe mode
immediately: `send()` / `start_activity()` return `False` (refused, not
raised), and reads still serve cached data but raise `RuntimeError` when
they would need a fresh hub fetch. When the app disconnects, control
returns on its own. Both waiters are plain state predicates, so a
long-running application can simply re-await
`wait_until_controllable()` whenever a send comes back `False`.

## ◇ Snapshots and live editing

The **snapshot** is the hub's structural configuration (devices,
activities, commands, bindings, macros, favorites; everything but the IR
payload blobs) as the library's cache holds it. `snapshot()` projects it
with **no hub traffic**, in every mode, as a `HubSnapshot`:

```python
snap = await proxy.snapshot()
snap.snapshot_id          # configuration content hash, used as the edit revision
snap.complete             # every entity fetched in full
for e in snap.devices + snap.activities:
    print(e.kind, e.entity_id, e.name, e.complete, e.editable, e.fetched_at)
snap.bundle               # the structural hub_bundle dict the sync takes as baseline
snap.to_dict()            # the bundle with the header merged in (one JSON document)
```

The initial sync fills the catalogs automatically; detailed reads,
backups and sync reconciliation populate further cache entries.
`refresh(device_id=5)` or `refresh(activity_id=101)` explicitly re-reads one
entity (a few bursts); `refresh()` alone re-reads both catalogs once and
then every device and activity. A whole-hub read can take **minutes**, so
treat it as a user action, report its `progress` (a `WriteProgress` per
entity) and expect to cancel it between entities. Nothing in the library
starts a whole-hub refresh on its own. A refresh publishes
`snapshot_changed`; configuration writes rebase the snapshot and announce
changes. Cancellation of a whole-hub refresh finishes the current entity
before releasing the operation, then publishes the detail read so far.

The content hash excludes provenance such as `fetched_at`: it can change
without a new `snapshot_id`. Importing saved state preserves its detail
and completeness; a partial cache remains partial after a restart.

The cache is a last-known copy, and only the user can say whether it is
still current. The hub can be edited outside this library at any time
(the vendor app, another client, a restore) and never says so; the
library deliberately reports no freshness verdict. `fetched_at` is the
age of each entity's copy, a refresh is the only way to bring it up to
date, and the sync stale check protects writes by re-reading the target
entity first. An `app_state` event with `connected=False` means a
vendor-app session through the proxy just ended: one visible occasion,
among many invisible ones, on which to offer a refresh.

To keep the cache warm across restarts, persist the state document (the
library never touches disk):

```python
doc = await proxy.export_state()        # opaque, versioned JSON document
...                                     # next run, before start():
snap = await proxy.import_state(doc)    # StateDocumentError if unreadable
```

Editing is bundle-based: take a snapshot as the baseline, modify a copy
of its bundle, and sync. The engine diffs the two bundles into an ordered
plan of targeted in-place writes (nothing is deleted-and-restored),
re-reads the entity first to detect concurrent changes, applies the steps
serially, each gated on the hub's acknowledgement, and re-reads the entity
afterwards so the next snapshot reflects the hub:

```python
import copy

snap = await proxy.snapshot()
baseline = snap.bundle
edited = copy.deepcopy(baseline)
# ... modify `edited`: rename the activity, rebind buttons, edit macros,
#     favorites, membership ...

# Optional dry run: the pure planner shows exactly what a sync would write.
from sofabaton import build_activity_sync_plan
for step in build_activity_sync_plan(baseline, edited, activity_id=101):
    print(step.kind, "-", step.label)

result = await proxy.sync_activity(
    baseline=baseline, edited=edited, activity_id=101,
    snapshot_id=snap.snapshot_id,          # refused up front if the snapshot moved
    progress=lambda p: print(p.phase, p.message),   # WriteProgress, on the loop
)
assert result.ok, result.message           # SyncResult: failed_at, completed_steps, snapshot_id
```

`sync_device` / `build_device_sync_plan` are the device-scoped
counterparts (command adds and renames, payload edits, idle behaviour,
input records) with the same bundle-pair contract. Two guards run before
anything is written: a `snapshot_id` that is no longer current raises
`SnapshotOutdatedError`, and a baseline entity that is not `editable`
(never fetched, or fetched incomplete) raises `SnapshotIncompleteError`;
refresh the entity and edit again. A failed sync reports where it stopped
(`failed_at`, `completed_steps`) in the `SyncResult` rather than raising;
`failed_at: "stale_check"` means the entity changed on the hub after the
baseline was captured, and `wrote_nothing` tells you no step reached the
hub. The planner refuses (with `ValueError`, surfaced as `failed_at:
"plan"`) any bundle difference outside the entity being edited, so an
editor bug cannot silently rewrite unrelated configuration.

### Editing the whole document

An editor that changes many things at once hands back the **whole**
snapshot document and lets the library own the transition: order,
id allocation, the one remote-sync trigger, and what happens when the
run stops halfway. A new device or activity carries a negative
placeholder id of your choosing (and every reference to it uses that
same negative id); a removed entity must be removed from every activity
in the same document; the array order of `devices` / `activities` is
the display order. `build_hub_sync_plan` validates the document with no
hub traffic and previews the items; `sync_hub` runs them inside one
batch (one trigger, one `snapshot_changed`), re-reading the affected
entities strictly before the first write:

```python
from sofabaton import ApplyState, DocumentError, build_hub_sync_plan

snap = await proxy.snapshot()
desired = copy.deepcopy(snap.bundle)
desired["hub"]["name"] = "Loft"
desired["devices"].append({"device": {"device_id": -1, "name": "Projector", "device_class": "ir"},
                           "commands": [], "button_bindings": [], "macros": []})
try:
    plan = build_hub_sync_plan(snap.bundle, desired)        # stage A: DocumentError subclasses
except DocumentError as err:
    print(err.code, err)                                    # dangling_reference, out_of_scope, ...
for item in plan.items:
    print(item.index, item.kind, item.entity_id or item.placeholder_id, item.label)

records: list[dict] = []
result = await proxy.sync_hub(
    baseline=snap.bundle, desired=desired, snapshot_id=snap.snapshot_id,
    progress=lambda p: print(p.item_index, p.phase, p.message),
    on_state=lambda state: records.append(state.to_dict()),   # persist this; the library never does
)
print(result.status, result.id_map, result.remote_sync)      # HubSyncResult
for item in result.items:
    print(item.kind, item.status, item.completed_steps, item.total_steps, item.message)
```

Every item ends `done`, `partial` (some steps landed), `uncertain` (a
write went out and no answer followed), `failed` (refused before its
first write), `not_attempted` or `cancelled`; the first non-`done` item
stops the run and nothing is rolled back. `on_state` receives the
`ApplyState` after every item (and right after a created entity's id is
known): store the last one, and continue later with
`sync_hub(state=ApplyState.from_dict(doc))`, which re-reads what the run
touched, keeps the ids it already created and re-plans the rest from the
hub's actual state. Cancelling the awaiting task finishes the item in
flight and leaves the state resumable. In the CLI: `snapshot out=D0.json`,
edit a copy, `apply D1.json baseline=D0.json plan`, then without `plan`
to write; `apply resume=D1.json.apply.json` continues.

### Edit helpers

The common row edits are pure functions in `sofabaton.edits`: each takes
a snapshot bundle and returns an edited copy for the sync, so a script
does not have to know the row shapes. `rename_activity`, `rename_device`,
`bind_button` (with an optional long press), `clear_button`,
`add_favorite`, `remove_favorite`, `reorder_favorites`, `rename_command`,
`set_idle_behavior`, `set_command_payload`, and `add_command` (which returns
the edited bundle and an available command id):

```python
from sofabaton import ButtonName, edits

snap = await proxy.snapshot()
edited = edits.bind_button(snap.bundle, 101, ButtonName.VOL_UP, device_id=7, command_id=3,
                           long_press=(7, 4))
result = await proxy.sync_activity(baseline=snap.bundle, edited=edited, activity_id=101,
                                   snapshot_id=snap.snapshot_id)
```

### Intents

Whole-entity writes a bundle diff cannot express are explicit coroutines.
They raise `HubBusyError` / `HubNotConnectedError` when the hub cannot be
written, `ValueError` for bad input, and `HubRejectedError` when the hub
refused or did not acknowledge; each ends with a rebase and a
`snapshot_changed` event:

```python
device_id = await proxy.add_device("Ceiling fan", "ir")   # empty IR device, hub-assigned id
activity_id = await proxy.add_activity("Read")
removed = await proxy.remove_device(device_id)            # DeviceRemoved: impacted activities
await proxy.remove_activity(activity_id)
await proxy.reorder_devices([7, 5, 8])                     # every device, once
await proxy.reorder_activities([102, 101])
await proxy.set_hub_name("Living room")
bundle = await proxy.backup(progress=print)                # full, restorable (minutes)
result = await proxy.restore(bundle, replace=False)        # RestoreResult; replace=True erases first
await proxy.erase()                                        # everything, final
```

These are separate operations, not a script to run in sequence. Whole-entity
intents use their own validation; the live baseline comparison described
above belongs to `sync_activity` / `sync_device`.

`backup()` includes command payloads by default. A structural snapshot or
`backup(include_blobs=False)` cannot be restored. `restore(bundle)` is
additive: it creates entities with new hub-assigned ids. Use
`restore(bundle, replace=True)` to validate the bundle before erasing and
rebuilding the hub; do not call `erase()` separately to implement replace.
Inspect `RestoreResult.ok`, `failed_at`, `restored_devices`,
`restored_activities`, `device_id_map` and `snapshot_id`. A partial restore
is not rolled back; inspect the resulting snapshot before deciding how to
recover. Retrying an additive restore can create duplicates.

### IR payloads

`IrPayload` is one command's stored payload. Build it from the formats
codes circulate in, read it back from the hub, fire it once, or capture it
from the original remote; saving one as a new command is a row edit:

```python
from sofabaton import IrPayload, edits

# A complete descriptor; choose a code appropriate to your device.
p = IrPayload.from_descriptor("P:NEC1 D:4 S:5 F:21")
await proxy.play(p)                     # emits IR once; nothing is saved

# Or capture from the original remote. Keep the hub idle while learning.
p = await proxy.learn_ir(timeout=30)    # IrLearnError if no usable capture
snap = await proxy.refresh(device_id=5)
edited, command_id = edits.add_command(snap.bundle, 5, p, "Learned key")
result = await proxy.sync_device(
    baseline=snap.bundle, edited=edited, device_id=5,
    snapshot_id=snap.snapshot_id,
)
if not result.ok:
    raise RuntimeError(f"Save failed at {result.failed_at}: {result.message}")
print("Saved command", command_id)
```

`IrPayload.from_pronto(text)`, `from_raw_timings(timings_us, carrier_hz)`
and `from_hex(text)` accept the other input formats. `read_payload(device_id,
command_id)` returns `None` when no payload is stored, so check it before
calling `play()`. `cancel_learn()` ends an active capture wait.

### Network commands

Wifi devices (`wifi_ip` on X1S and X2, `wifi_roku` everywhere, `wifi_hue`,
`wifi_sonos`) store a request the hub renders at press time. `NetworkCommand`
is that request in structured form; the same edit helpers save it, and the
class must match the device's class or the helper refuses before anything is
planned:

```python
from sofabaton import NetworkCommand, edits

device_id = await proxy.add_device("Home automation", "wifi_ip")   # X1S / X2
hook = NetworkCommand.http(host="192.168.1.20", port=8123, method="POST",
                           path="/api/webhook/lights", content_type="application/json",
                           body='{"state": "toggle"}')
snap = await proxy.refresh(device_id=device_id)
edited, command_id = edits.add_command(snap.bundle, device_id, hook, "Lights")
await proxy.sync_device(baseline=snap.bundle, edited=edited, device_id=device_id)
```

`NetworkCommand.roku("keypress/Home")` is a Roku ECP path; the Roku device head
carries the target address (set the device block's `ip_address` in the bundle
and sync the device) and the hub always POSTs to port 8060.
`NetworkCommand.hue(path, body_block)` and `.sonos(path, body_block)` cover the
two REST-over-head-address classes. `set_command_payload` takes a
`NetworkCommand` too.

### Managed wifi devices

A *managed* wifi device is one you create so the remote can call you:
`WIFI_SLOT_COUNT` slots, each a short and a long press record whose callback
path is `launch/<hub action id>/<device id>/<slot index>/<short|long>`, all
pointing at one host and port. Deploy one from a `WifiDeviceSpec`, keep the
returned `WifiDeployment`, and edit it in place later:

```python
from sofabaton import WifiDeviceSpec, WifiSlotSpec, WifiUpdateDeclined

spec = WifiDeviceSpec(name="Server", slots=(WifiSlotSpec("Play"), WifiSlotSpec("Pause")),
                      power_on_slot=1, input_slots=(2,))
deployment = await proxy.deploy_wifi_device(spec, host="192.168.1.10", port=8060)
store(deployment.to_dict())           # device id, spec, target, the 20 labels written

# Bind the commands with the generic helpers; the update below never touches those.
snap = await proxy.refresh(activity_id=101)
edited = edits.bind_button(snap.bundle, 101, ButtonName.PLAY, device_id=deployment.device_id, command_id=1)
await proxy.sync_activity(baseline=snap.bundle, edited=edited, activity_id=101)

try:
    deployment = await proxy.update_wifi_device(
        deployment, WifiDeviceSpec(name="Server", slots=(WifiSlotSpec("Start"), WifiSlotSpec("Pause"))))
except WifiUpdateDeclined as declined:      # "drift", "missing", "device" or "planner"
    ...                                     # nothing was written; remove and deploy again
```

Every slot is written, defaults included (`Button n` / `Button n Long`). The
callback target never changes in place: a new address is a remove and a new
deploy. The X1 always calls port 8060 and ignores the power and input hooks.
`update_wifi_device` refuses (`WifiUpdateDeclined`) when a record's label
matches neither what the deployment wrote nor what the new spec asks, so an
edit made in the Sofabaton app is never silently overwritten; a record that
already carries the new label is an interrupted update being resumed. A write
the hub rejects raises `WifiUpdateFailed` (a `HubRejectedError`) and the next
update with the same spec resumes.

A CLI ships as a console script:

```
sofabaton discover                   # scan the LAN for hubs
sofabaton run --hub-ip 192.168.1.50  # proxy + interactive shell
x> status
x> activities
x> commands 1                        # list (command_id, label) for device 1
x> send 1 5                          # numeric ids, exactly like the Python API
x> send 101 POWER_ON                 # the CLI also resolves button names to codes
x> testir 01 20 00 10 01 00 94 ac .. # fire a raw IR payload once (nothing saved)
x> snapshot                          # every device/activity + complete / stale flags
x> refresh act=101                   # re-read one entity (refresh alone = whole hub, slow)
x> rename act 101 Movie night        # snapshot, edit, sync
x> bind 101 VOL_UP 7 3 7 4           # button -> device 7 command 3, long press command 4
x> unbind 101 VOL_UP
x> hubname Den
x> backup hub.json                   # save a full bundle
x> restore hub.json                  # additive restore: creates new entities
x> restore hub.json erase            # replacing restore: validates, then erases
```

For command sending, the CLI's `send` (alias `press`) accepts either
a numeric command/button code or a `ButtonName` alias like `POWER_ON`.
The Python API itself is numeric-only — `send(entity_id, command_id)` —
with the `ButtonName` constants importable from the package root when
you want named button codes.

`testir` works with raw IR payload hex (the bytes a command replays, as
shown in a backup's `data_hex` fields) and plays it once without saving;
in the Python API this is `play(IrPayload.from_hex(...))`.

Runnable examples — discovery, accepting a hub record from a platform's
own discovery, watching the event stream, watching a live session, taking
control of a hub, reading per-entity detail (commands/macros/favorites),
schema-versioned backup/restore, provisioning a network device from
scratch via restore, and building an HTTP callback listener on top of the
library — live in
[`sofabaton-x/examples/`](https://github.com/m3tac0de/home-assistant-sofabaton-x1s/tree/main/sofabaton-x/examples).

For a complete facade edit workflow, run
[`examples/edit_activity.py`](examples/edit_activity.py) with a hub address,
activity id and name. It connects, refreshes that entity, previews the plan
and, with `--apply`, syncs and checks the result:

```sh
python sofabaton-x/examples/edit_activity.py --hub 192.168.1.50 --activity 101 --name "Movie night"
python sofabaton-x/examples/edit_activity.py --hub 192.168.1.50 --activity 101 --name "Movie night" --apply
```

## ◇ Protocol & networking docs

This library is a reverse-engineered implementation; the wire protocol and
network topology are documented in the repository:

- **Protocol reference** —
  [`docs/protocol/`](https://github.com/m3tac0de/home-assistant-sofabaton-x1s/tree/main/docs/protocol):
  [connection flow](https://github.com/m3tac0de/home-assistant-sofabaton-x1s/blob/main/docs/protocol/connection-flow.md),
  [frame format](https://github.com/m3tac0de/home-assistant-sofabaton-x1s/blob/main/docs/protocol/frame-format.md),
  [opcodes](https://github.com/m3tac0de/home-assistant-sofabaton-x1s/blob/main/docs/protocol/opcodes.md),
  [data structures](https://github.com/m3tac0de/home-assistant-sofabaton-x1s/blob/main/docs/protocol/data-structures.md),
  [hub versions](https://github.com/m3tac0de/home-assistant-sofabaton-x1s/blob/main/docs/protocol/hub-versions.md)
  and more.
- **Networking guide** —
  [`docs/networking.md`](https://github.com/m3tac0de/home-assistant-sofabaton-x1s/blob/main/docs/networking.md):
  the full port map, the two proxy faces, firewall rules and VLAN caveats.

## ◇ Stability

Names importable from the package root — `from sofabaton import ...`,
the set listed in `sofabaton.__all__` — are the supported API and follow
semver. Everything else (`sofabaton.opcode_handlers`, frame parsing,
wire schemas, the `proxy_*` mixin modules) is internal and may change
between minor releases. The public surface is async-first by design:
`AsyncXProxy` is the supported entry point, and the underlying
synchronous engine (reachable via `AsyncXProxy.sync` when you need the
raw surface) is internal and not semver-covered. Prefer the named facade
methods in this README; compatibility delegates and direct engine access
are for advanced consumers. Until 1.0, pin a minor version.

The facade exports these typed exceptions, all subclasses of stdlib
exceptions. Plain `ValueError` also reports malformed input or unsupported
operations. Readiness waiters return a boolean; control sends may return
`False`; sync and restore can return unsuccessful results. Check those
values as well as catching exceptions.

| exception | base | response |
| --- | --- | --- |
| `HubBusyError` | `RuntimeError` | wait until the app releases the hub |
| `HubNotConnectedError` | `RuntimeError` | wait for reconnection before retrying |
| `FetchTimeoutError` | `TimeoutError` | a read timed out; retain cached data and retry with backoff |
| `SnapshotIncompleteError` | `ValueError` | refresh the target entity before editing |
| `SnapshotOutdatedError` | `ValueError` | obtain a new snapshot and reapply the intended edit |
| `StateDocumentError` | `ValueError` | discard the unreadable state document and start cold |
| `HubRejectedError` | `RuntimeError` | inspect hub state before retrying a write; its outcome may be uncertain |
| `IrLearnError` | `RuntimeError` | inspect `state`; retry capture with the hub idle if appropriate |

## ◇ Issues & release notes

Bugs and feature requests go to the shared
[issue tracker](https://github.com/m3tac0de/home-assistant-sofabaton-x1s/issues).
For standalone library issues, include the command you ran, the terminal
output or traceback, the package and Python versions, and a small
reproduction snippet if possible.

See the [changelog](https://github.com/m3tac0de/home-assistant-sofabaton-x1s/blob/main/sofabaton-x/CHANGELOG.md)
for library changes and migration instructions. Library versions are tagged
`sofabaton-x-vX.Y.Z`; published releases are listed on the
[GitHub releases page](https://github.com/m3tac0de/home-assistant-sofabaton-x1s/releases).

## ◇ License

MIT — see [LICENSE](https://github.com/m3tac0de/home-assistant-sofabaton-x1s/blob/main/LICENSE).
