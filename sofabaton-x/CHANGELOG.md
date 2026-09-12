# Changelog

Changes to the standalone `sofabaton-x` library, with migration guidance
for applications using its public API. Server changes are documented
separately in the [server documentation](../sofabaton-x-server/README.md).

<!-- Keep new changes under Unreleased. At release, add the version and date,
update the README notice and install instructions, and start a new Unreleased
section. For each breaking release, link the README notice to its migration
entry. Keep previous entries when adding a new release. -->

## Unreleased — 0.2.0

This release is in development and contains breaking changes from 0.1.x.
The migration notes below use 0.1.5 as the previous API baseline.

### Breaking changes and migration

**Catalog reads return typed objects.** `activities()` and `devices()`
now return lists of `Activity` and `Device` objects, sorted by ID, instead
of dictionaries keyed by ID. `commands()`, `buttons()`, `macros()` and
`favorites()` return lists of typed objects instead of dictionaries.
Use attributes for fields and `.to_dict()` when you need a dictionary.

```python
# 0.1.x
activities = await proxy.activities()
for activity_id, activity in activities.items():
    print(activity_id, activity["name"])

# 0.2.0
for activity in await proxy.activities():
    print(activity.activity_id, activity.name)
```

`current_activity()` still returns a dictionary or `None`.

**Sync uses typed progress and results.** For `sync_activity()` and
`sync_device()`, replace `progress_callback=` with `progress=`. The callback
receives one `WriteProgress` object instead of keyword arguments. The
return value is a `SyncResult`, so use `result.ok` instead of
`result["status"] == "success"`.

```python
def on_progress(event):
    print(event.to_dict())

result = await proxy.sync_activity(
    baseline=snapshot.bundle,
    edited=edited_bundle,
    activity_id=activity_id,
    snapshot_id=snapshot.snapshot_id,
    progress=on_progress,
)
if not result.ok:
    print(result.failed_at)
```

Sync requires a complete, editable snapshot of the target. Refresh the
target before editing if needed. Pass the snapshot's `snapshot_id` to
reject changes based on an outdated revision. Handle
`SnapshotIncompleteError` and `SnapshotOutdatedError` as well as checking
`result.ok`. A disconnected hub or an app-held hub now raises
`HubNotConnectedError` or `HubBusyError` instead of returning an
`"unavailable"` result. See the [editing example](examples/edit_activity.py) for the
snapshot, preview and apply flow.

**Initial catalog sync is enabled by default.** `AsyncXProxy(...)` and
`AsyncXProxy.from_config(...)` now read hub identity, devices and activities
when the hub connects. Pass `initial_sync=False` if your application owns
that work. `AsyncXProxy.wrap(...)` still defaults to `initial_sync=False`.
Use `wait_until_ready()` to wait for the automatic sync and check its
boolean result before using the catalogs.

**Write methods raise on failure.** `set_hub_name()` and
`reorder_activities()` now return `None` on success. Remove checks that
treat a falsey return value as failure; handle the documented exceptions
instead. Sync and restore retain explicit result objects whose `.ok`
must be checked.

**Several engine delegates are no longer exposed on `AsyncXProxy`.** Use
the facade operations below. These are migration paths; argument and
return types can differ from the old methods.

| Removed facade delegate | Migration path |
| --- | --- |
| `get_banner_info()` | `hub_info()` returns a typed `HubInfo`. |
| `backup_hub_bundle()` | `backup()` returns a bundle. |
| `restore_hub_bundle()` | `restore(bundle)` returns a `RestoreResult`; use `replace=True` when replacing the hub configuration. |
| `erase_configuration()` | `erase()`. |
| `create_activity()` | `add_activity(name)` returns the new activity ID. |
| `delete_device()` | `remove_device(device_id)`. |
| `play_ir_blob()` | `play(payload)` accepts an `IrPayload` or bytes. |
| `request_ir_command_dump()` | `read_payload(device_id, command_id)` returns an `IrPayload` or `None`. |
| `persist_ir_blob()` | Apply `edits.add_command()` to a bundle, then `sync_device()`. |
| `command_to_button()` | Apply `edits.bind_button()` to a bundle, then `sync_activity()`. |
| `command_to_favorite()`, `delete_favorite()`, `reorder_favorites()` | Apply `edits.add_favorite()`, `edits.remove_favorite()` or `edits.reorder_favorites()` to a bundle, then `sync_activity()`. |
| `add_device_to_activity()` | Edit activity membership in a bundle, then `sync_activity()`. |
| `create_wifi_device()` | Use `deploy_wifi_device(WifiDeviceSpec(...))` for managed callback devices. For generic network devices, use `add_device()` plus `NetworkCommand`/edit helpers, or `restore(bundle)` for complete provisioning. |

The [library README](README.md) documents the current facade and its
exceptions. Direct engine access through `.sync` is internal and has no
compatibility guarantee.

### Added

- `HubConfig`, typed hub status and identity, and typed events.
- Snapshot revisions, explicit refresh, and state export/import.
- Pure bundle edit helpers and plan previews before applying changes.
- Whole-document `build_hub_sync_plan()` and `sync_hub()`, with placeholder
  IDs and `ApplyState` checkpoints. Read the [current recovery limitations](README.md#current-document-write-limitations)
  before implementing resume or retry.
- `batch_writes()` coalesces requested remote-sync triggers and snapshot
  notifications; empty/no-op work does not guarantee either.
- Facade operations for activity/device management, backup, restore and erase.
- `IrPayload` conversion, playback, capture and command payload reads.

- `NetworkCommand` payloads (`http`, `roku`, `hue`, `sonos`) for the edit
  helpers, with a device-class check.
- `deploy_wifi_device` / `update_wifi_device` with `WifiDeviceSpec`,
  `WifiDeployment`, `WifiUpdateDeclined` and `WifiUpdateFailed`; the
  in-place planner carries a pinned callback address (`target_host`).
- `local_address()`: the routed local IPv4 toward the hub (the default
  callback target; a container on a bridge network must pass its host's).

## Earlier releases

For 0.1.x and earlier, see the
[GitHub releases](https://github.com/m3tac0de/home-assistant-sofabaton-x1s/releases)
tagged `sofabaton-x-v…`.
