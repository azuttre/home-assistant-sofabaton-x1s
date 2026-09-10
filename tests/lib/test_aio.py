"""Tests for the asyncio facade (lib/aio.py).

The facade owns no protocol logic, so these tests verify its jobs:
executor delegation, thread->loop callback marshaling, and the
human-friendly read/control surface — including the burst->Future bridge
that turns the engine's lazy ``(data, ready)`` getters into clean
awaitables.
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import sys
import threading
import types
from pathlib import Path

LIB_DIR = (
    Path(__file__).resolve().parents[2]
    / "custom_components"
    / "sofabaton_x1s"
    / "lib"
)


def _load_lib() -> types.ModuleType:
    name = "sofabaton_aio_test_pkg"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(
        name, LIB_DIR / "__init__.py", submodule_search_locations=[str(LIB_DIR)]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_pkg = _load_lib()
aio = importlib.import_module(f"{_pkg.__name__}.aio")
x1_proxy_mod = importlib.import_module(f"{_pkg.__name__}.x1_proxy")
protocol_const = importlib.import_module(f"{_pkg.__name__}.protocol_const")
errors = importlib.import_module(f"{_pkg.__name__}.errors")


class FakeProxy:
    """Duck-typed engine modelling the lazy-fetch + burst pattern.

    A catalog/detail getter returns ``(data, ready)``; while not ready it
    records a fetch and returns empty. ``make_ready`` + ``fire_burst``
    (optionally from a worker thread) emulate the hub reply landing.
    """

    class _Transport:
        is_hub_connected = True
        is_client_connected = False

    class _State:
        def __init__(self, proxy):
            self._p = proxy
            self.button_details: dict[int, dict] = {}
            self.current_activity: int | None = None
            self.activity_names: dict[int, str] = {}
            # Snapshot provenance mirrors (phase 3 W0).
            self.detail_fetched_at: dict[str, dict[int, str]] = {"device": {}, "activity": {}}
            self.detail_stale_risk: dict[str, set[int]] = {"device": set(), "activity": set()}
            self.generation = 0

        def get_activity_favorite_labels(self, act_lo):
            return list(self._p.favorite_labels.get(act_lo, []))

        def get_activity_name(self, act_id):
            if act_id is None:
                return None
            return self.activity_names.get(act_id & 0xFF)

        def entities(self, kind):
            # The engine's unstripped rows (raw_body kept); the facade
            # projects devices and status counts from these.
            key = "activities" if kind == "activity" else "devices"
            return dict(self._p._ready.get(key) or {})

    def __init__(self) -> None:
        self._listeners: dict[str, list] = {}
        self.hub_state_listeners: list = []
        self.client_state_listeners: list = []
        self.burst_listeners: dict[str, list] = {}
        self.can_issue = True
        self.started = False
        self.stopped = False
        self.sent: list[tuple[int, int]] = []
        self.fetch_calls: list[tuple[str, int | None]] = []
        self.buttons_ready: dict[int, list] = {}
        self.favorite_labels: dict[int, list] = {}
        self.transport = FakeProxy._Transport()
        self.state = FakeProxy._State(self)
        self._ready: dict[str, dict] = {"commands": {}, "macros": {}, "activities": None, "devices": None}
        # Discovery surface the facade orchestrates: reading the banner
        # (fetch_banner_info) yields an identity (has_banner_identity), which
        # the facade then publishes via update_discovery_identity.
        self.banner_fetches = 0
        self.banner_known = False
        self.advertised: list[tuple[dict, str]] = []
        self.mdns_txt: dict[str, str] = {}
        self.hub_version = "X1"
        self.real_hub_ip = "1.2.3.4"
        # Structural detail per entity (what a backup-grade fetch leaves
        # behind): kind -> id -> list of binding rows. ``pending_detail`` is
        # what the next backup_* call "reads from the hub".
        self.detail: dict[str, dict[int, list]] = {"device": {}, "activity": {}}
        self.pending_detail: dict[str, dict[int, list]] = {"device": {}, "activity": {}}
        self.backup_calls: list[tuple[str, int, bool]] = []
        self.write_calls: list = []
        self.reject = False
        self.payloads: dict[tuple[int, int], bytes] = {}
        descriptor = b"P:NEC1 D:4 F:21"
        descriptive = len(descriptor).to_bytes(2, "big") + bytes([0, 0, 0x11, 0, 0x94, 0x70]) + descriptor + bytes(4)
        self.learn_result: dict = {"state": "learned", "payload_hex": descriptive.hex(" ")}

    # -- snapshot / state document surface (phase 3 W0) -------------------
    def bump_cache_generation(self) -> int:
        self.state.generation += 1
        return self.state.generation

    def get_known_device_ids(self) -> set[int]:
        return set((self._ready["devices"] or {}).keys())

    def get_known_activity_ids(self) -> set[int]:
        return set((self._ready["activities"] or {}).keys())

    def _catalog(self, kind: str) -> dict:
        return dict(self._ready["devices" if kind == "device" else "activities"] or {})

    def _entity_payload(self, kind: str, ent: int) -> dict:
        meta = self._catalog(kind).get(ent) or {}
        fetched = self.detail[kind].get(ent)
        block = {"device_id": ent, "name": meta.get("name")}
        if kind == "activity":
            block["entity_type"] = "activity"
        payload = {
            "kind": f"{kind}_backup",
            "captured_at": "2026-01-01T00:00:00Z",
            "complete": fetched is not None,
            "payload_profile": "structural",
            "device": block,
            "button_bindings": list(fetched or []),
        }
        stamp = self.state.detail_fetched_at[kind].get(ent)
        if stamp:
            payload["fetched_at"] = stamp
        payload["stale_risk"] = ent in self.state.detail_stale_risk[kind]
        payload["editable"] = payload["complete"]
        return payload

    def assemble_hub_bundle_from_state(self, *, hub_info=None, include_unfetched=False):
        if not include_unfetched and not any(self.state.detail_fetched_at.values()):
            return None
        devices = [self._entity_payload("device", i) for i in sorted(self._catalog("device"))]
        activities = [self._entity_payload("activity", i) for i in sorted(self._catalog("activity"))]
        return {
            "kind": "hub_bundle",
            "captured_at": "2026-01-01T00:00:00Z",
            "complete": all(p["complete"] for p in devices + activities),
            "payload_profile": "structural",
            "hub": {"name": "Fake"},
            "devices": devices,
            "activities": activities,
        }

    def _backup(self, kind: str, ent: int, refresh_catalog: bool):
        self.backup_calls.append((kind, ent, refresh_catalog))
        if ent not in self._catalog(kind):
            return None
        self.detail[kind][ent] = list(self.pending_detail[kind].get(ent, []))
        self.state.detail_fetched_at[kind][ent] = f"stamp-{len(self.backup_calls)}"
        self.state.detail_stale_risk[kind].discard(ent)
        self.bump_cache_generation()
        return self._entity_payload(kind, ent)

    def backup_device(self, device_id, *, wait_timeout=10.0, include_blobs=True,
                      reuse_commands=False, refresh_catalog=True):
        assert include_blobs is False, "refresh() must be structural"
        return self._backup("device", device_id & 0xFF, refresh_catalog)

    def backup_activity(self, activity_id, *, wait_timeout=10.0, refresh_catalog=True):
        return self._backup("activity", activity_id & 0xFF, refresh_catalog)

    def mark_detail_stale_risk(self, kind=None, ent_id=None) -> None:
        kinds = ("device", "activity") if kind is None else (kind,)
        for k in kinds:
            if ent_id is None:
                self.state.detail_stale_risk[k].update(self.state.detail_fetched_at[k])
            else:
                self.state.detail_stale_risk[k].add(ent_id & 0xFF)
        self.bump_cache_generation()

    def export_cache_state(self) -> dict:
        import copy
        return {
            "catalog": copy.deepcopy(self._ready),
            "detail": copy.deepcopy(self.detail),
            "detail_fetched_at": copy.deepcopy(self.state.detail_fetched_at),
            "detail_stale_risk": {k: sorted(v) for k, v in self.state.detail_stale_risk.items()},
            "generation": self.state.generation,
        }

    # -- intents / payloads (phase 3 W3) ----------------------------------
    # The fake's catalogs count as read unless a test says otherwise.
    _devices_catalog_ready = True
    _activities_catalog_ready = True

    def create_device(self, name, *, device_class):
        self.write_calls.append(("create_device", name, device_class))
        if self.reject:
            return None
        new_id = max(self._catalog("device"), default=0) + 1
        self._ready["devices"] = {**self._catalog("device"), new_id: {"name": name}}
        return {"status": "success", "device_id": new_id}

    def create_activity(self, name):
        self.write_calls.append(("create_activity", name))
        if self.reject:
            return None
        new_id = max(self._catalog("activity"), default=100) + 1
        self._ready["activities"] = {**self._catalog("activity"), new_id: {"name": name}}
        return {"status": "success", "activity_id": new_id}

    def delete_device(self, device_id):
        self.write_calls.append(("delete_device", device_id))
        if self.reject:
            return None
        if device_id >= 101:
            catalog = self._catalog("activity"); catalog.pop(device_id, None)
            self._ready["activities"] = catalog
            self.detail["activity"].pop(device_id, None)
            return {"status": "success", "device_id": device_id, "confirmed_activities": [], "impacted_activities": []}
        catalog = self._catalog("device"); catalog.pop(device_id, None)
        self._ready["devices"] = catalog
        self.detail["device"].pop(device_id, None)
        self.detail["activity"].pop(101, None)   # the hub rewrote it; detail dropped
        return {"status": "success", "device_id": device_id,
                "confirmed_activities": [101], "impacted_activities": [101]}

    def reorder_devices(self, ordered_ids):
        self.write_calls.append(("reorder_devices", list(ordered_ids)))
        return None if self.reject else {"status": "success", "ordered_ids": list(ordered_ids)}

    def reorder_activities(self, ordered_ids):
        self.write_calls.append(("reorder_activities", list(ordered_ids)))
        return None if self.reject else {"status": "success", "ordered_ids": list(ordered_ids)}

    def set_hub_name(self, name, *, timeout=5.0):
        self.write_calls.append(("set_hub_name", name))
        return not self.reject

    def erase_configuration(self, *, timeout=120.0, settle_seconds=2.0):
        self.write_calls.append(("erase",))
        if self.reject:
            return False
        self._ready["devices"] = {}; self._ready["activities"] = {}
        self.detail = {"device": {}, "activity": {}}
        return True

    def backup_hub_bundle(self, *, include_blobs=True, wait_timeout=10.0, progress=None, device_ids=None, hub_info=None):
        self.write_calls.append(("backup", include_blobs))
        if progress is not None:
            progress(status="running", phase="device", message="Backing up device 5…",
                     completed_steps=0, total_steps=1, current_device_id=5)
        for dev in self._catalog("device"):
            self._backup("device", dev, False)
        return {"kind": "hub_bundle", "payload_profile": "full_backup" if include_blobs else "structural",
                "devices": [], "activities": []}

    def preflight_restore_bundle(self, payload):
        self.write_calls.append(("preflight", payload.get("tag")))
        if payload.get("schema_version", 1) != 1:
            raise ValueError("bad schema")
        return {"devices": 1, "activities": 1}

    def restore_hub_bundle(self, payload, *, progress_callback=None, **kwargs):
        self.write_calls.append(("restore", payload.get("tag")))
        if progress_callback is not None:
            progress_callback(status="running", phase="device", message="Restoring…",
                              completed_steps=0, total_steps=2)
        if self.reject:
            # The engine's shape: lists of per-entity records, not counts.
            return {"status": "failed", "failed_at": ["device", 3], "device_id_map": {"3": 9},
                    "restored_devices": [], "restored_activities": []}
        self._ready["devices"] = {**self._catalog("device"), 9: {"name": "Restored"}}
        return {"status": "success", "device_id_map": {"3": 9},
                "restored_devices": [{"source_device_id": 3, "device_id": 9}],
                "restored_activities": [{"source_activity_id": 101, "activity_id": 101}]}

    def request_ir_command_dump(self, device_id, command_id=None, *, timeout=10.0):
        self.write_calls.append(("dump", device_id, command_id))
        blob = self.payloads.get((device_id, command_id))
        if blob is None:
            return {"complete": True, "commands": []}
        # The engine's dump row: blob hex INCLUDING the replay-tail byte.
        return {"complete": True, "commands": [
            {"command_id": command_id, "device_id": device_id, "ir_blob_hex": (blob + b"\x5a").hex()}
        ]}

    def _resolve_device_class(self, device_id):
        return "ir"

    def play_ir_blob(self, blob, **kwargs):
        self.write_calls.append(("play", bytes(blob)))
        return not self.reject

    def ir_learn_command(self, *, timeout=60.0, ack_timeout=2.0):
        self.write_calls.append(("learn", timeout))
        return self.learn_result

    def cancel_ir_learn(self):
        return True

    def import_cache_state(self, payload: dict) -> None:
        import copy
        self._ready = copy.deepcopy(payload["catalog"])
        self.detail = copy.deepcopy(payload["detail"])
        self.state.detail_fetched_at = copy.deepcopy(payload["detail_fetched_at"])
        self.state.detail_stale_risk = {k: set(v) for k, v in payload["detail_stale_risk"].items()}
        self.state.generation = max(self.state.generation, int(payload["generation"]))
        self.bump_cache_generation()

    # -- listener registration ------------------------------------------
    def on_hub_state_change(self, cb) -> None:
        self.hub_state_listeners.append(cb)

    def on_client_state_change(self, cb) -> None:
        self.client_state_listeners.append(cb)

    # The remaining engine listeners, for the events() stream.
    def on_activity_change(self, cb) -> None:
        self._listeners.setdefault("activity", []).append(cb)

    def on_activity_list_update(self, cb) -> None:
        self._listeners.setdefault("activity_list", []).append(cb)

    def on_ota_update(self, cb) -> None:
        self._listeners.setdefault("ota", []).append(cb)

    def on_app_activation(self, cb) -> None:
        self._listeners.setdefault("activation", []).append(cb)

    def fire_activity_change(self, new_id, old_id, name) -> None:
        for cb in self._listeners.get("activity", []):
            cb(new_id, old_id, name)

    def fire_simple(self, which: str) -> None:
        for cb in self._listeners.get(which, []):
            cb()

    def set_connected(self, *, hub: bool, client: bool = False) -> None:
        if self.transport.is_client_connected and not client:
            self.mark_detail_stale_risk()  # the engine's W1 transition flag
        self.transport.is_hub_connected = hub
        self.transport.is_client_connected = client
        self.can_issue = hub and not client
        for cb in list(self.hub_state_listeners):
            cb(hub)
        for cb in list(self.client_state_listeners):
            cb(client)

    def on_burst_end(self, key, cb) -> None:
        self._listeners.setdefault(key, []).append(cb)
        self.burst_listeners.setdefault(key, []).append(cb)

    # emulate state_helpers.BurstScheduler._notify_burst_end
    def fire_burst(self, full_key: str) -> None:
        for cb in self._listeners.get(full_key, []):
            cb(full_key)
        if ":" in full_key:
            prefix = full_key.split(":", 1)[0]
            for cb in self._listeners.get(prefix, []):
                cb(full_key)

    def fire_hub_state(self, value: bool) -> None:
        for cb in self.hub_state_listeners:
            cb(value)

    # -- gating / lifecycle ---------------------------------------------
    def can_issue_commands(self) -> bool:
        return self.can_issue

    def get_proxy_status(self) -> bool:
        return True

    def has_banner_identity(self) -> bool:
        return self.banner_known

    def fetch_banner_info(self, *, force_refresh=True, timeout=2.0):
        # Reading the banner is only possible when we own the hub.
        self.banner_fetches += 1
        if self.can_issue:
            self.banner_known = True
        return ({}, self.banner_known)

    def get_banner_info(self) -> dict:
        return {"model": "X1S", "name": "Living Room"} if self.banner_known else {}

    def update_discovery_identity(self, *, mdns_txt, hub_version):
        # Publishing the advertisement; record what identity we advertised.
        self.advertised.append((dict(mdns_txt), hub_version))

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    # -- lazy getters ----------------------------------------------------
    # NOTE: catalog getters gate on force_refresh (matching the real
    # engine); per-entity getters below gate on fetch_if_missing.
    def get_activities(self, *, force_refresh=True):
        if force_refresh:
            self.fetch_calls.append(("activities", None))
            return ({}, False)
        data = self._ready["activities"]
        return (data, True) if data is not None else ({}, False)

    def get_devices(self, *, force_refresh=False):
        if force_refresh:
            self.fetch_calls.append(("devices", None))
            return ({}, False)
        data = self._ready["devices"]
        return (data, True) if data is not None else ({}, False)

    def get_commands_for_entity(self, ent_id, *, fetch_if_missing=True):
        lo = ent_id & 0xFF
        if lo in self._ready["commands"]:
            return (dict(self._ready["commands"][lo]), True)
        if fetch_if_missing:
            self.fetch_calls.append(("commands", lo))
        return ({}, False)

    def get_macros_for_activity(self, act_id, *, fetch_if_missing=True):
        lo = act_id & 0xFF
        if lo in self._ready["macros"]:
            return (list(self._ready["macros"][lo]), True)
        if fetch_if_missing:
            self.fetch_calls.append(("macros", lo))
        return ([], False)

    def get_buttons_for_entity(self, ent_id, *, fetch_if_missing=True):
        lo = ent_id & 0xFF
        if lo in self.buttons_ready:
            return (list(self.buttons_ready[lo]), True)
        if fetch_if_missing:
            self.fetch_calls.append(("buttons", lo))
        return ([], False)

    def ensure_commands_for_activity(self, act_id, *, fetch_if_missing=True):
        return ({}, True)

    def send_command(self, ent_id, key_code) -> bool:
        self.sent.append((ent_id, key_code))
        return True

    # -- test helpers ----------------------------------------------------
    def make_commands_ready(self, lo, data) -> None:
        self._ready["commands"][lo] = data

    def make_activities_ready(self, data) -> None:
        self._ready["activities"] = data


def _wrap(fake: FakeProxy) -> "aio.AsyncXProxy":
    return aio.AsyncXProxy.wrap(fake)


# ---------------------------------------------------------------------------
# delegation / surface guards
# ---------------------------------------------------------------------------


def test_proxy_methods_exist_on_real_engine() -> None:
    missing = [
        name
        for name in aio.AsyncXProxy.PROXY_METHODS
        if not callable(getattr(x1_proxy_mod.X1Proxy, name, None))
    ]
    assert not missing, f"PROXY_METHODS drifted from X1Proxy: {sorted(missing)}"


def test_every_public_engine_method_is_triaged() -> None:
    # The facade is curated by hand on purpose. This guard does not force
    # exposure; it forces a *decision*: every public X1Proxy method must
    # sit in exactly one tier (wrapped / delegated / listener / engine-only
    # with a reason), so a new engine method fails here until placed.
    triage = aio.engine_method_triage(x1_proxy_mod.X1Proxy)
    assert not triage["untriaged"], (
        "public engine methods not placed in any facade tier "
        f"(wrap, delegate, or add to aio.ENGINE_ONLY with a reason): "
        f"{sorted(triage['untriaged'])}"
    )
    assert not triage["overlap"], f"placed in more than one tier: {sorted(triage['overlap'])}"
    assert not triage["stale"], f"placed but gone from the engine: {sorted(triage['stale'])}"


def test_engine_only_entries_carry_a_reason() -> None:
    empty = [name for name, reason in aio.ENGINE_ONLY.items() if not str(reason).strip()]
    assert not empty, f"ENGINE_ONLY entries without a reason: {empty}"


def test_triage_flags_an_unplaced_engine_method() -> None:
    class Grown(x1_proxy_mod.X1Proxy):
        def brand_new_public_method(self) -> None:  # pragma: no cover - never called
            pass

    triage = aio.engine_method_triage(Grown)
    assert triage["untriaged"] == {"brand_new_public_method"}
    assert not triage["overlap"]
    assert not triage["stale"]


def test_human_surface_delegates_to_real_engine_methods() -> None:
    # Each clean method wraps a real engine method; assert those exist so
    # the human surface can't silently drift from the engine.
    required = [
        "get_activities",
        "get_devices",
        "get_commands_for_entity",
        "get_buttons_for_entity",
        "get_macros_for_activity",
        "ensure_commands_for_activity",
        "send_command",
        "can_issue_commands",
        "sync_activity",
        "sync_device",
    ]
    missing = [n for n in required if not callable(getattr(x1_proxy_mod.X1Proxy, n, None))]
    assert not missing, f"engine methods missing: {missing}"

    for name in (
        "activities",
        "devices",
        "commands",
        "buttons",
        "macros",
        "favorites",
        "current_activity",
        "press",
        "start_activity",
        "stop_activity",
        "find_remote",
        "sync_activity",
        "sync_device",
    ):
        assert callable(getattr(aio.AsyncXProxy, name, None)), f"missing facade method {name}"


def test_unknown_attribute_raises_with_hint() -> None:
    async def main():
        proxy = _wrap(FakeProxy())
        try:
            proxy.definitely_not_a_method
        except AttributeError as err:
            assert ".sync" in str(err)
        else:
            raise AssertionError("expected AttributeError")

    asyncio.run(main())


# ---------------------------------------------------------------------------
# read surface
# ---------------------------------------------------------------------------


def test_activities_devices_read_cached_via_force_refresh() -> None:
    # Locks in that the facade uses force_refresh (not fetch_if_missing)
    # for the catalog getters — the real engine signature.
    async def main():
        fake = FakeProxy()
        fake.make_activities_ready({1: {"name": "Watch TV"}})
        fake._ready["devices"] = {5: {"name": "TV"}}
        proxy = _wrap(fake)
        acts = await proxy.activities()
        assert [(a.activity_id, a.name, a.active) for a in acts] == [(1, "Watch TV", False)]
        devs = await proxy.devices()
        assert [(d.device_id, d.name, d.power_state) for d in devs] == [(5, "TV", None)]
        assert fake.fetch_calls == []  # cached: no refresh fetch kicked

    asyncio.run(main())


def test_current_activity_reports_live_state_without_fetch() -> None:
    # current_activity reads the live engine state (no catalog fetch) and
    # is available even in observe mode (app holds the hub).
    async def main():
        fake = FakeProxy()
        fake.set_connected(hub=True, client=True)  # observe mode
        fake.state.activity_names = {1: "Watch TV"}
        proxy = _wrap(fake)

        assert await proxy.current_activity() is None  # idle

        fake.state.current_activity = 1
        assert await proxy.current_activity() == {"activity_id": 1, "name": "Watch TV"}
        assert fake.fetch_calls == []  # never triggers a hub fetch

    asyncio.run(main())


def test_read_returns_cached_without_fetch() -> None:
    async def main():
        fake = FakeProxy()
        fake.make_commands_ready(5, {0xC6: "Power"})
        proxy = _wrap(fake)
        assert [c.to_dict() for c in await proxy.commands(5)] == [{"command_id": 0xC6, "label": "Power"}]
        assert fake.fetch_calls == []  # already cached: no hub fetch

    asyncio.run(main())


def test_read_fetches_then_awaits_burst() -> None:
    async def main():
        fake = FakeProxy()
        proxy = _wrap(fake)

        async def land_later():
            await asyncio.sleep(0.05)
            fake.make_commands_ready(5, {0xC6: "Power"})
            # Fire from a worker thread to exercise call_soon_threadsafe.
            t = threading.Thread(target=fake.fire_burst, args=("commands:5",))
            t.start()
            t.join()

        asyncio.ensure_future(land_later())
        result = await proxy.commands(5)
        assert [c.to_dict() for c in result] == [{"command_id": 0xC6, "label": "Power"}]
        assert ("commands", 5) in fake.fetch_calls  # fetch was kicked

    asyncio.run(main())


def test_read_raises_when_app_connected_and_uncached() -> None:
    async def main():
        fake = FakeProxy()
        fake.set_connected(hub=True, client=True)  # app holds the hub
        proxy = _wrap(fake)
        try:
            await proxy.commands(5)
        except RuntimeError as err:
            assert "app client" in str(err)
        else:
            raise AssertionError("expected RuntimeError")
        assert fake.fetch_calls == []  # never tried to fetch

    asyncio.run(main())


def test_read_error_distinguishes_hub_not_connected() -> None:
    async def main():
        fake = FakeProxy()
        fake.set_connected(hub=False)  # not connected yet, no app
        proxy = _wrap(fake)
        try:
            await proxy.commands(5)
        except RuntimeError as err:
            assert "not connected" in str(err)
            assert "app client" not in str(err)
        else:
            raise AssertionError("expected RuntimeError")

    asyncio.run(main())


def test_wait_until_controllable_resolves_on_state_change() -> None:
    async def main():
        fake = FakeProxy()
        fake.set_connected(hub=False)  # start: not controllable
        proxy = _wrap(fake)

        async def connect_later():
            await asyncio.sleep(0.05)
            t = threading.Thread(target=fake.set_connected, kwargs={"hub": True})
            t.start()
            t.join()

        asyncio.ensure_future(connect_later())
        assert await proxy.wait_until_controllable(timeout=5) is True

    asyncio.run(main())


def test_wait_until_discoverable_reads_banner_then_advertises() -> None:
    async def main():
        fake = FakeProxy()
        fake.set_connected(hub=True)  # control mode, not yet advertising
        proxy = _wrap(fake)
        assert await proxy.wait_until_discoverable(timeout=2) is True
        # It drove the banner read once, then published the advertisement
        # aligned to the banner identity (model X1S -> HVER "2").
        assert fake.banner_fetches == 1
        assert len(fake.advertised) == 1
        txt, hub_version = fake.advertised[0]
        assert hub_version == "X1S"
        assert txt["HVER"] == "2"
        assert txt["NAME"] == "Living Room"

    asyncio.run(main())


def test_wait_until_discoverable_publishes_without_refetch_when_identity_known() -> None:
    async def main():
        fake = FakeProxy()
        # App attached (observe mode): it drove the banner, so identity is
        # already known but we can't issue commands.
        fake.set_connected(hub=True, client=True)
        fake.banner_known = True
        proxy = _wrap(fake)
        assert await proxy.wait_until_discoverable(timeout=2) is True
        # No banner read needed; we just (re)published the advertisement.
        assert fake.banner_fetches == 0
        assert len(fake.advertised) == 1

    asyncio.run(main())


def test_wait_until_discoverable_false_when_hub_never_connects() -> None:
    async def main():
        fake = FakeProxy()
        fake.set_connected(hub=False)
        proxy = _wrap(fake)
        assert await proxy.wait_until_discoverable(timeout=0.2) is False
        assert fake.banner_fetches == 0
        assert fake.advertised == []

    asyncio.run(main())


def test_wait_connected_true_in_observe_mode() -> None:
    async def main():
        fake = FakeProxy()
        # Hub up but app attached: connected (observe) yet not controllable.
        fake.set_connected(hub=True, client=True)
        proxy = _wrap(fake)
        assert await proxy.wait_connected(timeout=1) is True
        assert await proxy.wait_until_controllable(timeout=0.2) is False

    asyncio.run(main())


def test_read_returns_cached_even_when_app_connected() -> None:
    async def main():
        fake = FakeProxy()
        fake.can_issue = False
        fake.make_commands_ready(5, {0xC6: "Power"})
        proxy = _wrap(fake)
        assert [c.to_dict() for c in await proxy.commands(5)] == [{"command_id": 0xC6, "label": "Power"}]

    asyncio.run(main())


def test_read_timeout_cleans_up_waiter() -> None:
    async def main():
        fake = FakeProxy()
        proxy = _wrap(fake)
        try:
            await proxy.commands(7, timeout=0.05)
        except TimeoutError:
            pass
        else:
            raise AssertionError("expected TimeoutError")
        assert proxy._burst_waiters.get("commands:7", []) == []  # no leak

    asyncio.run(main())


def test_favorites_returns_rich_device_command_label() -> None:
    async def main():
        fake = FakeProxy()
        fake.buttons_ready[101] = []  # keymap fetched (no buttons), so no wait
        fake.favorite_labels[101] = [
            {"name": "Denon Power", "device_id": 3, "command_id": 45}
        ]
        proxy = _wrap(fake)
        assert [f.to_dict() for f in await proxy.favorites(101)] == [
            {"device_id": 3, "command_id": 45, "label": "Denon Power"}
        ]

    asyncio.run(main())


def test_commands_and_buttons_return_send_pairs() -> None:
    async def main():
        fake = FakeProxy()
        fake.make_commands_ready(5, {12: "Sleep"})
        fake.buttons_ready[101] = [174]
        fake.state.button_details = {101: {174: {"device_id": 3, "command_id": 20}}}
        proxy = _wrap(fake)

        assert [c.to_dict() for c in await proxy.commands(5)] == [{"command_id": 12, "label": "Sleep"}]

        btns = await proxy.buttons(101)
        assert [b.to_dict() for b in btns] == [
            {"button_code": 174, "name": btns[0].name, "device_id": 3, "command_id": 20}
        ]
        assert set(btns[0].to_dict()) == {"button_code", "name", "device_id", "command_id"}

    asyncio.run(main())


# ---------------------------------------------------------------------------
# control surface
# ---------------------------------------------------------------------------


def test_press_and_activity_control() -> None:
    async def main():
        fake = FakeProxy()
        proxy = _wrap(fake)

        assert await proxy.press(101, 0xB0) is True
        await proxy.start_activity(5)
        await proxy.stop_activity(5)

        assert fake.sent[0] == (101, 0xB0)
        assert fake.sent[1] == (5, protocol_const.ButtonName.POWER_ON)
        assert fake.sent[2] == (5, protocol_const.ButtonName.POWER_OFF)

    asyncio.run(main())


# ---------------------------------------------------------------------------
# executor delegation / lifecycle / marshaling
# ---------------------------------------------------------------------------


def test_lifecycle_and_context_manager() -> None:
    async def main():
        fake = FakeProxy()
        async with _wrap(fake) as proxy:
            assert fake.started and not fake.stopped
            assert proxy.sync is fake
        assert fake.stopped

    asyncio.run(main())


def test_run_escape_hatch() -> None:
    async def main():
        proxy = _wrap(FakeProxy())
        assert await proxy.run(lambda a, b: a + b, 2, b=3) == 5

    asyncio.run(main())


def test_delegated_method_runs_in_executor() -> None:
    async def main():
        call_thread = {}

        # restore_device is a delegated PROXY_METHODS name.
        class WithProvision(FakeProxy):
            def restore_device(self, *args, **kwargs):
                call_thread["t"] = threading.get_ident()
                return {"ok": True}

        proxy = _wrap(WithProvision())
        assert await proxy.restore_device({}) == {"ok": True}
        assert call_thread["t"] != threading.get_ident()

    asyncio.run(main())


def test_cache_snapshot_serializers_are_not_on_facade() -> None:
    # The engine's raw (de)serializers stay off the public async surface;
    # the typed state document (export_state / import_state) is the
    # facade's face for them (phase 3 W0).
    async def main():
        proxy = _wrap(FakeProxy())
        assert callable(proxy.export_state) and callable(proxy.import_state)
        for name in ("export_cache_state", "import_cache_state", "clear_cached_entity_detail"):
            assert name not in aio.AsyncXProxy.PROXY_METHODS
            try:
                getattr(proxy, name)
            except AttributeError:
                pass
            else:
                raise AssertionError(f"{name} should not be exposed on the facade")

    asyncio.run(main())


def test_live_edit_sync_runs_in_executor_with_loop_progress() -> None:
    # sync_activity/sync_device are explicit facade methods (not
    # PROXY_METHODS delegates) so the progress callback lands on the event
    # loop while the engine call itself runs in the executor.
    async def main():
        call_threads = {}
        progress_threads: list[int] = []
        done = asyncio.Event()

        class WithSync(FakeProxy):
            def sync_activity(self, *, baseline, edited, activity_id, progress_callback=None):
                call_threads["activity"] = threading.get_ident()
                if progress_callback is not None:
                    progress_callback(phase="writing", completed_steps=0, total_steps=1)
                return {"status": "success", "completed_steps": 1, "total_steps": 1}

            def sync_device(self, *, baseline, edited, device_id, progress_callback=None):
                call_threads["device"] = threading.get_ident()
                return {"status": "success", "completed_steps": 0, "total_steps": 0}

        proxy = _wrap(WithSync())
        loop_thread = threading.get_ident()
        seen: list = []

        def on_progress(report) -> None:
            progress_threads.append(threading.get_ident())
            seen.append(report)
            done.set()

        result = await proxy.sync_activity(
            baseline={}, edited={}, activity_id=0x65, progress=on_progress
        )
        assert isinstance(result, models.SyncResult) and result.ok
        assert result.completed_steps == 1 and result.snapshot_id
        assert call_threads["activity"] != loop_thread

        await asyncio.wait_for(done.wait(), 5)
        assert progress_threads == [loop_thread]
        assert isinstance(seen[0], models.WriteProgress) and seen[0].phase == "writing"

        result = await proxy.sync_device(baseline={}, edited={}, device_id=3)
        assert result.to_dict()["status"] == "success" and result.total_steps == 0
        assert call_threads["device"] != loop_thread

    asyncio.run(main())


def test_sync_callback_delivered_on_loop_thread() -> None:
    async def main():
        fake = FakeProxy()
        proxy = _wrap(fake)
        loop_thread = threading.get_ident()
        seen: list[tuple[bool, int]] = []
        done = asyncio.Event()

        def cb(value: bool) -> None:
            seen.append((value, threading.get_ident()))
            done.set()

        proxy.on_hub_state_change(cb)
        worker = threading.Thread(target=fake.fire_hub_state, args=(True,))
        worker.start()
        worker.join()
        await asyncio.wait_for(done.wait(), 5)
        assert seen == [(True, loop_thread)]

    asyncio.run(main())


def test_coroutine_callback_scheduled_on_loop() -> None:
    async def main():
        fake = FakeProxy()
        proxy = _wrap(fake)
        loop_thread = threading.get_ident()
        seen: list[tuple[str, bool, int]] = []
        done = asyncio.Event()

        async def cb(value: bool) -> None:
            seen.append(("coro", value, threading.get_ident()))
            done.set()

        proxy.on_hub_state_change(cb)
        proxy.on_burst_end("devices", lambda *a: None)  # registration shape check
        assert "devices" in fake.burst_listeners

        worker = threading.Thread(target=fake.fire_hub_state, args=(False,))
        worker.start()
        worker.join()
        await asyncio.wait_for(done.wait(), 5)
        assert seen == [("coro", False, loop_thread)]

    asyncio.run(main())


# ---------------------------------------------------------------------------
# discovery facade
# ---------------------------------------------------------------------------


def test_async_discover_hubs_delegates(monkeypatch) -> None:
    sentinel = ["hub"]
    captured: dict = {}

    def fake_discover(*, timeout, zc, include_proxies):
        captured.update(timeout=timeout, zc=zc, include_proxies=include_proxies)
        captured["thread"] = threading.get_ident()
        return sentinel

    monkeypatch.setattr(aio, "discover_hubs", fake_discover)

    async def main():
        result = await aio.async_discover_hubs(0.5, include_proxies=True)
        assert result is sentinel
        assert captured["timeout"] == 0.5 and captured["include_proxies"] is True
        assert captured["thread"] != threading.get_ident()

    asyncio.run(main())


def test_async_hub_browser_marshals_callbacks() -> None:
    discovery = importlib.import_module(f"{_pkg.__name__}.discovery")
    hub_versions = importlib.import_module(f"{_pkg.__name__}.hub_versions")

    class FakeServiceInfo:
        port = 8102
        properties = {b"HVER": b"2", b"NAME": b"Den"}

        def parsed_addresses(self):
            return ["192.168.1.50"]

    class FakeZeroconf:
        def get_service_info(self, service_type, name, timeout=3000):
            return FakeServiceInfo()

    class FakeStateChange:
        name = "Added"

    async def main():
        loop_thread = threading.get_ident()
        seen: list[tuple[str, int]] = []
        done = asyncio.Event()

        async def on_added(hub) -> None:
            seen.append((hub.name, threading.get_ident()))
            done.set()

        browser = aio.AsyncHubBrowser(zc=FakeZeroconf(), on_added=on_added)
        browser.sync._create_browser = lambda zc: types.SimpleNamespace(
            cancel=lambda: None
        )
        await browser.start()
        try:
            worker = threading.Thread(
                target=browser.sync._on_service_state_change,
                args=(
                    browser.sync._zc,
                    hub_versions.MDNS_SERVICE_TYPE_X1,
                    "DEN._x1hub._udp.local.",
                    FakeStateChange(),
                ),
            )
            worker.start()
            worker.join()
            await asyncio.wait_for(done.wait(), 5)
        finally:
            await browser.stop()

        assert seen == [("Den", loop_thread)]
        # The snapshot survives stop(); it reflects the last browse state.
        assert [hub.name for hub in browser.hubs] == ["Den"]

    asyncio.run(main())


# ---------------------------------------------------------------------------
# typed errors + status surface (phase 1 F4 / F2)
# ---------------------------------------------------------------------------

errors = importlib.import_module(f"{_pkg.__name__}.errors")
models = importlib.import_module(f"{_pkg.__name__}.models")


def test_typed_errors_are_stdlib_subclasses() -> None:
    assert issubclass(errors.HubNotConnectedError, RuntimeError)
    assert issubclass(errors.HubBusyError, RuntimeError)
    assert issubclass(errors.FetchTimeoutError, TimeoutError)


def test_read_raises_typed_busy_and_not_connected() -> None:
    async def main():
        fake = FakeProxy()
        proxy = _wrap(fake)
        fake.set_connected(hub=True, client=True)
        try:
            await proxy.commands(1)
        except errors.HubBusyError:
            pass
        else:
            raise AssertionError("expected HubBusyError")
        fake.set_connected(hub=False)
        try:
            await proxy.commands(1)
        except errors.HubNotConnectedError:
            pass
        else:
            raise AssertionError("expected HubNotConnectedError")

    asyncio.run(main())


def test_read_timeout_is_typed() -> None:
    async def main():
        fake = FakeProxy()
        proxy = _wrap(fake)
        try:
            await proxy.commands(1, timeout=0.05)
        except errors.FetchTimeoutError:
            pass
        else:
            raise AssertionError("expected FetchTimeoutError")

    asyncio.run(main())


def test_status_reports_mode_and_counts() -> None:
    async def main():
        fake = FakeProxy()
        fake.make_activities_ready({1: {"name": "TV"}, 2: {"name": "Music"}})
        fake.state.current_activity = 0x0102
        fake.state.activity_names[2] = "Music"
        proxy = _wrap(fake)

        st = await proxy.status()
        assert isinstance(st, models.HubStatus)
        assert st.mode == "control" and st.controllable and st.hub_connected
        assert st.hub_version == "X1" and st.proxy_enabled
        assert st.activities_cached == 2 and st.devices_cached == 0
        assert st.running_activity == models.RunningActivity(activity_id=2, name="Music")
        assert st.to_dict()["running_activity"] == {"activity_id": 2, "name": "Music"}

        fake.set_connected(hub=True, client=True)
        assert (await proxy.status()).mode == "observe"
        fake.set_connected(hub=False)
        st = await proxy.status()
        assert st.mode == "disconnected" and not st.app_connected

    asyncio.run(main())


def test_hub_info_cached_then_refresh_then_busy() -> None:
    async def main():
        fake = FakeProxy()
        proxy = _wrap(fake)

        # Nothing known and control mode: hub_info fetches the banner.
        info = await proxy.hub_info()
        assert fake.banner_fetches == 1
        assert isinstance(info, models.HubInfo) and info.known
        assert info.model == "X1S" and info.name == "Living Room"

        # Known banner is served from cache without a fetch.
        await proxy.hub_info()
        assert fake.banner_fetches == 1
        # refresh=True forces a re-read.
        await proxy.hub_info(refresh=True)
        assert fake.banner_fetches == 2

        # Observe mode: cached identity still served, refresh refused typed.
        fake.set_connected(hub=True, client=True)
        assert (await proxy.hub_info()).known
        try:
            await proxy.hub_info(refresh=True)
        except errors.HubBusyError:
            pass
        else:
            raise AssertionError("expected HubBusyError")

    asyncio.run(main())


def test_hub_info_unknown_without_fetch_is_not_an_error() -> None:
    async def main():
        fake = FakeProxy()
        fake.set_connected(hub=False)
        proxy = _wrap(fake)
        info = await proxy.hub_info()
        assert not info.known and info.model is None
        assert fake.banner_fetches == 0

    asyncio.run(main())


# ---------------------------------------------------------------------------
# typed catalog results (phase 1 F3)
# ---------------------------------------------------------------------------

devices_mod = importlib.import_module(f"{_pkg.__name__}.devices")


def test_devices_project_power_state_from_stored_record() -> None:
    async def main():
        fake = FakeProxy()
        fake.hub_version = "X1S"
        on = devices_mod.DeviceConfig(name="TV", brand="Sony", power_state=1)
        # The create payload carries a 3-byte header in front of the record
        # body; the catalog row stores the body alone (what parse expects).
        body_on = devices_mod.build_device_create_payload(on, hub_version="X1S")[3:]
        # The catalog row keeps the record body; the facade parses the
        # power byte out of it (None when the body is missing or bad).
        fake._ready["devices"] = {
            5: {"name": "TV", "brand": "Sony", "device_class": "ir",
                "device_class_code": 1, "raw_body": body_on, "idle_behavior": 2},
            6: {"name": "Amp", "raw_body": b"\x00\x01"},
            7: {"name": "Lamp"},
        }
        proxy = _wrap(fake)
        devs = {d.device_id: d for d in await proxy.devices()}
        assert devs[5].power_state == 1
        assert devs[5].brand == "Sony" and devs[5].device_class == "ir"
        assert devs[5].device_class_code == 1 and devs[5].idle_behavior == 2
        assert devs[6].power_state is None  # unparseable body
        assert devs[7].power_state is None and devs[7].brand is None
        assert devs[5].to_dict()["power_state"] == 1

    asyncio.run(main())


def test_activities_carry_flags_and_sort_by_id() -> None:
    async def main():
        fake = FakeProxy()
        fake.make_activities_ready({
            102: {"name": "Music", "active": True, "needs_confirm": True},
            101: {"name": "TV", "active": False},
        })
        proxy = _wrap(fake)
        acts = await proxy.activities()
        assert [a.activity_id for a in acts] == [101, 102]
        assert acts[1] == models.Activity(activity_id=102, name="Music", active=True, needs_confirm=True)
        assert acts[0].to_dict() == {"activity_id": 101, "name": "TV", "active": False, "needs_confirm": False}

    asyncio.run(main())


# ---------------------------------------------------------------------------
# event stream (phase 1 F5)
# ---------------------------------------------------------------------------


async def _collect_until(proxy, kind, *, timeout=2.0):
    """Events up to and including the first one of ``kind``."""

    out = []
    agen = proxy.events()
    try:
        while not out or out[-1].kind != kind:
            out.append(await asyncio.wait_for(agen.__anext__(), timeout))
    finally:
        await agen.aclose()
    return out


async def _collect(proxy, n, *, maxsize=256, timeout=2.0):
    out = []
    agen = proxy.events(maxsize=maxsize)
    try:
        while len(out) < n:
            out.append(await asyncio.wait_for(agen.__anext__(), timeout))
    finally:
        await agen.aclose()
    return out


def test_events_fold_every_listener_into_typed_hub_events() -> None:
    async def main():
        fake = FakeProxy()
        proxy = _wrap(fake)

        async def fire():
            await asyncio.sleep(0.01)
            # From a worker thread, like the engine.
            def _engine():
                fake.fire_activity_change(0x0102, None, "Music")
                fake.fire_simple("activity_list")
                fake.fire_simple("ota")
            t = threading.Thread(target=_engine)
            t.start(); t.join()

        asyncio.ensure_future(fire())
        events = await _collect(proxy, 3)
        assert [e.kind for e in events] == ["activity_changed", "activity_list_updated", "ota"]
        assert [e.seq for e in events] == [1, 2, 3]
        assert events[0].payload == models.ActivityChanged(activity_id=2, previous_activity_id=None, name="Music")
        assert events[1].payload is None
        assert events[0].to_dict() == {
            "seq": 1, "kind": "activity_changed",
            "payload": {"activity_id": 2, "previous_activity_id": None, "name": "Music"},
        }
        assert isinstance(events[0], models.HubEvent)

    asyncio.run(main())


def test_events_emit_status_changed_once_per_mode_flip() -> None:
    async def main():
        fake = FakeProxy()
        proxy = _wrap(fake)

        async def fire():
            # The mode is derived on the loop after each callback (the
            # callbacks may run under the transport's locks), so yield
            # between flips the way real engine-thread callbacks would.
            await asyncio.sleep(0.01)
            fake.set_connected(hub=True, client=True)   # control -> observe
            await asyncio.sleep(0.01)
            fake.set_connected(hub=True, client=True)   # no flip: no status event
            await asyncio.sleep(0.01)
            fake.set_connected(hub=False)               # observe -> disconnected

        asyncio.ensure_future(fire())
        # 1st set: hub_state, app_state, status_changed; 2nd: hub_state, app_state;
        # 3rd: hub_state, app_state, status_changed.
        events = await _collect(proxy, 8)
        kinds = [e.kind for e in events]
        assert kinds.count("status_changed") == 2
        flips = [e.payload for e in events if e.kind == "status_changed"]
        assert flips[0] == models.StatusChanged(mode="observe", previous_mode="control")
        assert flips[1] == models.StatusChanged(mode="disconnected", previous_mode="observe")
        assert events[0].payload == models.ConnectionState(connected=True)

    asyncio.run(main())


def test_events_bounded_queue_drops_oldest_and_counts() -> None:
    async def main():
        fake = FakeProxy()
        proxy = _wrap(fake)
        agen = proxy.events(maxsize=2)
        # Prime the generator so its queue is registered, then flood it
        # before consuming.
        first_task = asyncio.ensure_future(agen.__anext__())
        await asyncio.sleep(0.01)
        for i in range(5):
            fake.fire_simple("ota")
        await asyncio.sleep(0.01)
        first = await first_task
        second = await asyncio.wait_for(agen.__anext__(), 1.0)
        await agen.aclose()
        # All five dispatches run on the loop before the pending get
        # resumes, so capacity 2 keeps the newest two (4, 5) and drops
        # three (1, 2, 3), each counted.
        assert proxy.events_dropped == 3
        assert (first.seq, second.seq) == (4, 5)

    asyncio.run(main())


def test_events_consumer_exit_unregisters_queue() -> None:
    async def main():
        fake = FakeProxy()
        proxy = _wrap(fake)
        agen = proxy.events()
        task = asyncio.ensure_future(agen.__anext__())
        await asyncio.sleep(0.01)
        assert len(proxy._event_queues) == 1
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        # Cancellation ran the generator's finally: the queue is gone.
        assert not proxy._event_queues
        await agen.aclose()
        # Listeners stay armed (registered once) even with no consumer.
        fake.fire_simple("ota")
        await asyncio.sleep(0.01)
        assert proxy.events_dropped == 0

    asyncio.run(main())


# ---------------------------------------------------------------------------
# connect-time initial sync (phase 1 F6)
# ---------------------------------------------------------------------------


def _land(fake, key, data=None) -> None:
    """Emulate the hub reply landing for a catalog burst."""

    if key == "devices":
        fake._ready["devices"] = data if data is not None else {}
    elif key == "activities":
        fake._ready["activities"] = data if data is not None else {}
    fake.fire_burst(key)


def test_initial_sync_runs_on_connect_in_order_and_marks_ready() -> None:
    async def main():
        fake = FakeProxy()
        fake.set_connected(hub=False)
        proxy = aio.AsyncXProxy.wrap(fake, initial_sync=True)
        assert not (await proxy.status()).catalog_ready

        fake.set_connected(hub=True)          # hub connects: sync starts
        await asyncio.sleep(0.02)
        assert fake.banner_fetches == 1
        assert fake.fetch_calls == [("devices", None)]   # devices requested first

        _land(fake, "devices", {5: {"name": "TV"}})
        await asyncio.sleep(0.02)
        assert fake.fetch_calls == [("devices", None), ("activities", None)]
        assert not proxy._catalog_ready

        _land(fake, "activities", {1: {"name": "Watch TV"}})
        assert await proxy.wait_until_ready(timeout=1.0)
        st = await proxy.status()
        assert st.catalog_ready and st.activities_cached == 1 and st.devices_cached == 1
        # The catalog is served from cache now: no new fetch.
        assert [a.name for a in await proxy.activities()] == ["Watch TV"]
        assert fake.fetch_calls == [("devices", None), ("activities", None)]

    asyncio.run(main())


def test_read_during_initial_sync_joins_the_inflight_fetch() -> None:
    async def main():
        fake = FakeProxy()
        fake.set_connected(hub=False)
        proxy = aio.AsyncXProxy.wrap(fake, initial_sync=True)
        fake.set_connected(hub=True)
        await asyncio.sleep(0.02)
        assert fake.fetch_calls == [("devices", None)]

        # A consumer asks for devices while the sync's fetch is in flight.
        read = asyncio.ensure_future(proxy.devices())
        await asyncio.sleep(0.02)
        assert fake.fetch_calls == [("devices", None)]   # no second request

        _land(fake, "devices", {5: {"name": "TV"}})
        devs = await asyncio.wait_for(read, 1.0)
        assert [d.name for d in devs] == ["TV"]

    asyncio.run(main())


def test_initial_sync_disconnect_mid_fetch_stays_not_ready_and_reruns() -> None:
    async def main():
        fake = FakeProxy()
        fake.set_connected(hub=False)
        proxy = aio.AsyncXProxy.wrap(fake, initial_sync=True)
        events = proxy.events()
        first = asyncio.ensure_future(events.__anext__())

        fake.set_connected(hub=True)
        await asyncio.sleep(0.02)
        fake.set_connected(hub=False)          # drop while devices is pending
        await asyncio.sleep(0.02)
        assert not proxy._catalog_ready
        assert not await proxy.wait_until_ready(timeout=0.05)

        # Reconnect: the sync runs again from the banner.
        fake.set_connected(hub=True)
        await asyncio.sleep(0.02)
        assert fake.banner_fetches == 2
        _land(fake, "devices")
        await asyncio.sleep(0.02)
        _land(fake, "activities")
        assert await proxy.wait_until_ready(timeout=1.0)

        # The stream carried the ready flip.
        seen = [await first]
        agen = events
        for _ in range(12):
            try:
                seen.append(await asyncio.wait_for(agen.__anext__(), 0.05))
            except TimeoutError:
                break
        await agen.aclose()
        ready_events = [e.payload for e in seen if e.kind == "catalog_ready"]
        assert ready_events == [models.CatalogReady(ready=True)]

    asyncio.run(main())


def test_initial_sync_waits_for_control_mode() -> None:
    async def main():
        fake = FakeProxy()
        fake.set_connected(hub=False)
        proxy = aio.AsyncXProxy.wrap(fake, initial_sync=True)
        fake.set_connected(hub=True, client=True)   # observe: app holds the hub
        await asyncio.sleep(0.02)
        assert fake.banner_fetches == 0 and fake.fetch_calls == []

        fake.set_connected(hub=True)                # app left: control mode
        await asyncio.sleep(0.02)
        assert fake.banner_fetches == 1 and fake.fetch_calls == [("devices", None)]

    asyncio.run(main())


def test_ready_waiter_is_false_when_initial_sync_is_off() -> None:
    async def main():
        proxy = _wrap(FakeProxy())          # wrap() defaults initial_sync=False
        assert not await proxy.wait_until_ready(timeout=0.01)
        assert not (await proxy.status()).catalog_ready

    asyncio.run(main())


# ---------------------------------------------------------------------------
# stop(release_hub=True) (phase 1 addendum F9)
# ---------------------------------------------------------------------------


def test_stop_release_hub_bounces_shared_listener_after_stop(monkeypatch) -> None:
    calls: list[str] = []

    class Engine(FakeProxy):
        def stop(self) -> None:
            calls.append("stop")

    monkeypatch.setattr(aio, "release_hub_from_listener", lambda ip: calls.append(f"release {ip}"))

    async def main():
        proxy = _wrap(Engine())
        await proxy.stop()
        assert calls == ["stop"]                      # plain stop: no release
        calls.clear()
        await proxy.stop(release_hub=True)
        assert calls == ["stop", "release 1.2.3.4"]   # release: AFTER the stop, for this hub's IP

    asyncio.run(main())


# ---------------------------------------------------------------------------
# refresh reads (review 2026-09-09: fetch-then-prune, never clear)
# ---------------------------------------------------------------------------


def test_devices_refresh_forces_a_fetch_and_awaits_it() -> None:
    async def main():
        fake = FakeProxy()
        fake._ready["devices"] = {5: {"name": "TV"}}
        proxy = _wrap(fake)
        assert [d.name for d in await proxy.devices()] == ["TV"]
        assert fake.fetch_calls == []                       # cached read

        read = asyncio.ensure_future(proxy.devices(refresh=True))
        await asyncio.sleep(0.02)
        assert fake.fetch_calls == [("devices", None)]      # a forced re-read
        assert fake._ready["devices"] == {5: {"name": "TV"}}   # nothing cleared meanwhile
        _land(fake, "devices", {5: {"name": "TV"}, 6: {"name": "Amp"}})
        devs = await asyncio.wait_for(read, 1.0)
        assert [d.name for d in devs] == ["TV", "Amp"]

        # activities(refresh=True) takes the same path.
        fake.make_activities_ready({1: {"name": "Watch TV"}})
        read = asyncio.ensure_future(proxy.activities(refresh=True))
        await asyncio.sleep(0.02)
        assert fake.fetch_calls[-1] == ("activities", None)
        _land(fake, "activities", {1: {"name": "Watch TV"}})
        assert [a.name for a in await asyncio.wait_for(read, 1.0)] == ["Watch TV"]

    asyncio.run(main())


def test_refused_refresh_raises_and_keeps_the_cached_catalog() -> None:
    async def main():
        fake = FakeProxy()
        fake._ready["devices"] = {5: {"name": "TV"}}
        proxy = _wrap(fake)
        fake.set_connected(hub=True, client=True)           # an app holds the hub
        try:
            await proxy.devices(refresh=True)
        except errors.HubBusyError:
            pass
        else:
            raise AssertionError("expected HubBusyError")
        assert fake.fetch_calls == []                       # never asked
        # The plain read still serves the last catalog.
        assert [d.name for d in await proxy.devices()] == ["TV"]

        fake.set_connected(hub=False)
        try:
            await proxy.activities(refresh=True)
        except errors.HubNotConnectedError:
            pass
        else:
            raise AssertionError("expected HubNotConnectedError")

    asyncio.run(main())


# ---------------------------------------------------------------------------
# snapshot / refresh / state document (phase 3 plan, W0)
# ---------------------------------------------------------------------------


def _catalog_fake(*, fetched_devices=(), fetched_activities=()) -> FakeProxy:
    """Two devices, one activity in the catalog; optionally some fetched."""

    fake = FakeProxy()
    fake._ready["devices"] = {5: {"name": "TV"}, 7: {"name": "Amp"}}
    fake._ready["activities"] = {101: {"name": "Watch TV"}}
    fake.pending_detail["device"] = {5: [{"button_id": 1}], 7: []}
    fake.pending_detail["activity"] = {101: [{"button_id": 2}]}
    for dev in fetched_devices:
        fake._backup("device", dev, True)
    for act in fetched_activities:
        fake._backup("activity", act, True)
    fake.backup_calls.clear()
    return fake


def test_snapshot_projects_without_fetch_and_reports_incomplete() -> None:
    async def main():
        fake = _catalog_fake(fetched_devices=(5,))
        proxy = _wrap(fake)
        snap = await proxy.snapshot()
        assert fake.fetch_calls == [] and fake.backup_calls == []
        assert isinstance(snap, models.HubSnapshot)
        assert [e.entity_id for e in snap.devices] == [5, 7]
        assert [e.entity_id for e in snap.activities] == [101]
        tv, amp = snap.devices
        assert tv.complete and tv.editable and tv.fetched_at and not tv.stale_risk
        assert not amp.complete and not amp.editable and amp.fetched_at is None
        assert snap.complete is False and snap.stale_risk is False
        assert snap.entity("device", 7) is amp and snap.entity("activity", 1) is None
        doc = snap.to_dict()
        assert doc["snapshot_id"] == snap.snapshot_id and len(snap.snapshot_id) == 64
        assert doc["devices"][0]["editable"] is True and doc["complete"] is False
        assert snap.bundle["payload_profile"] == "structural"
        # Observe mode projects the same: no HubBusyError for a cache read.
        fake.set_connected(hub=True, client=True)
        assert (await proxy.snapshot()).snapshot_id == snap.snapshot_id

    asyncio.run(main())


def test_snapshot_id_is_content_only() -> None:
    async def main():
        fake = _catalog_fake(fetched_devices=(5, 7), fetched_activities=(101,))
        proxy = _wrap(fake)
        base = (await proxy.snapshot()).snapshot_id
        # Provenance never moves the id: a new fetch stamp, a stale flag.
        fake.state.detail_fetched_at["device"][5] = "later"
        fake.mark_detail_stale_risk()
        again = await proxy.snapshot()
        assert again.snapshot_id == base and again.stale_risk is True
        assert again.entity("device", 5).stale_risk is True
        # Content does: a binding appears on a device.
        fake.detail["device"][7] = [{"button_id": 9}]
        assert (await proxy.snapshot()).snapshot_id != base

    asyncio.run(main())


def test_refresh_whole_reads_catalogs_once_then_every_entity() -> None:
    async def main():
        fake = _catalog_fake()
        proxy = _wrap(fake)
        progress: list = []

        async def run():
            return await proxy.refresh(progress=progress.append)

        task = asyncio.ensure_future(run())
        await asyncio.sleep(0.02)
        assert fake.fetch_calls == [("devices", None)]
        _land(fake, "devices", {5: {"name": "TV"}, 7: {"name": "Amp"}})
        await asyncio.sleep(0.02)
        _land(fake, "activities", {101: {"name": "Watch TV"}})
        snap = await asyncio.wait_for(task, 2)

        # One catalog read each, then every entity WITHOUT its own catalog read.
        assert fake.fetch_calls == [("devices", None), ("activities", None)]
        assert fake.backup_calls == [
            ("device", 5, False), ("device", 7, False), ("activity", 101, False),
        ]
        assert snap.complete and all(e.editable for e in snap.devices + snap.activities)
        assert [p.phase for p in progress] == ["preparing", "device", "device", "activity", "finalizing"]
        assert all(isinstance(p, models.WriteProgress) for p in progress)
        assert (progress[1].entity_kind, progress[1].entity_id, progress[1].total_steps) == ("device", 5, 3)
        assert progress[-1].completed_steps == 3

    asyncio.run(main())


def test_refresh_emits_snapshot_changed_with_touched_ids() -> None:
    async def main():
        fake = _catalog_fake(fetched_devices=(5, 7), fetched_activities=(101,))
        proxy = _wrap(fake)
        before = await proxy.snapshot()
        fake.pending_detail["activity"][101] = [{"button_id": 3}]  # hub changed

        async def fire():
            await asyncio.sleep(0.01)
            await proxy.refresh(activity_id=101)

        asyncio.ensure_future(fire())
        (event,) = await _collect(proxy, 1)
        assert event.kind == "snapshot_changed"
        assert event.payload.activity_ids == (101,) and event.payload.device_ids == ()
        assert event.payload.snapshot_id != before.snapshot_id
        assert event.to_dict()["payload"]["engine_generation"] == fake.state.generation
        # Only the asked entity was read, with its own catalog read.
        assert fake.backup_calls == [("activity", 101, True)]

    asyncio.run(main())


def test_refresh_refused_in_observe_mode_without_hub_traffic() -> None:
    async def main():
        fake = _catalog_fake()
        fake.set_connected(hub=True, client=True)
        proxy = _wrap(fake)
        for kwargs in ({}, {"device_id": 5}):
            try:
                await proxy.refresh(**kwargs)
            except errors.HubBusyError:
                pass
            else:
                raise AssertionError("refresh must be refused in observe mode")
        assert fake.backup_calls == [] and fake.fetch_calls == []
        try:
            await proxy.refresh(device_id=5, activity_id=101)
        except ValueError:
            pass
        else:
            raise AssertionError("one entity at a time")

    asyncio.run(main())


def test_refresh_concurrent_whole_calls_join_one_read() -> None:
    async def main():
        fake = _catalog_fake()
        proxy = _wrap(fake)

        async def drive():
            await asyncio.sleep(0.02)
            _land(fake, "devices", {5: {"name": "TV"}, 7: {"name": "Amp"}})
            await asyncio.sleep(0.02)
            _land(fake, "activities", {101: {"name": "Watch TV"}})

        asyncio.ensure_future(drive())
        first, second = await asyncio.wait_for(
            asyncio.gather(proxy.refresh(), proxy.refresh()), 2
        )
        assert first.snapshot_id == second.snapshot_id
        assert fake.fetch_calls == [("devices", None), ("activities", None)]
        assert len(fake.backup_calls) == 3

    asyncio.run(main())


def test_refresh_cancel_stops_between_entities() -> None:
    async def main():
        gate = threading.Event()
        started = threading.Event()

        class Slow(FakeProxy):
            def backup_device(self, device_id, **kwargs):
                started.set()
                gate.wait(5)
                return super().backup_device(device_id, **kwargs)

        fake = Slow()
        fake._ready["devices"] = {5: {"name": "TV"}, 7: {"name": "Amp"}}
        fake._ready["activities"] = {}
        proxy = _wrap(fake)

        task = asyncio.ensure_future(proxy.refresh())
        await asyncio.sleep(0.02)
        _land(fake, "devices", {5: {"name": "TV"}, 7: {"name": "Amp"}})
        await asyncio.sleep(0.02)
        _land(fake, "activities", {})
        await asyncio.get_running_loop().run_in_executor(None, started.wait, 2)
        task.cancel()
        gate.set()
        try:
            await task
        except asyncio.CancelledError:
            pass
        await asyncio.sleep(0.05)
        # The entity in flight completed; the next one never started.
        assert [c[1] for c in fake.backup_calls] == [5]
        assert proxy._whole_refresh_task is None
        # The facade is usable again afterwards.
        snap = await proxy.snapshot()
        assert snap.entity("device", 5).complete and not snap.entity("device", 7).complete

    asyncio.run(main())


def test_export_import_state_round_trip_keeps_id_and_flags() -> None:
    async def main():
        source = _catalog_fake(fetched_devices=(5, 7), fetched_activities=(101,))
        source.mark_detail_stale_risk("device", 7)
        src = _wrap(source)
        origin = await src.snapshot()
        doc = await src.export_state()
        assert doc["kind"] == "sofabaton_state" and doc["schema"] == 1
        assert doc["library"] == _pkg.__version__ and isinstance(doc["state"], dict)

        target = FakeProxy()
        dst = _wrap(target)
        assert (await dst.snapshot()).devices == []

        async def fire():
            await asyncio.sleep(0.01)
            await dst.import_state(doc)

        asyncio.ensure_future(fire())
        (event,) = await _collect(dst, 1)
        assert event.kind == "snapshot_changed" and event.payload.device_ids == ()
        restored = await dst.snapshot()
        assert restored.snapshot_id == origin.snapshot_id
        assert restored.complete and restored.entity("device", 7).stale_risk
        assert restored.engine_generation > origin.engine_generation
        assert target.fetch_calls == [] and target.backup_calls == []

        for bad in ({}, {"kind": "sofabaton_state", "schema": 99, "state": {}},
                    {"kind": "sofabaton_state", "schema": 1}):
            try:
                await dst.import_state(bad)
            except errors.StateDocumentError:
                pass
            else:
                raise AssertionError(f"{bad!r} must be rejected")

    asyncio.run(main())


def test_sync_guards_snapshot_id_and_editable_baseline_then_rebases() -> None:
    async def main():
        calls: list = []

        class WithSync(FakeProxy):
            def sync_activity(self, *, baseline, edited, activity_id, progress_callback=None):
                calls.append(activity_id)
                self.detail["activity"][activity_id] = [{"button_id": 42}]  # hub moved
                return {"status": "success", "completed_steps": 1, "total_steps": 1}

            def sync_device(self, *, baseline, edited, device_id, progress_callback=None):
                calls.append(device_id)
                return {"status": "failed", "failed_at": "stale_check", "message": "changed"}

        fake = WithSync()
        fake._ready["devices"] = {5: {"name": "TV"}, 7: {"name": "Amp"}}
        fake._ready["activities"] = {101: {"name": "Watch TV"}}
        fake._backup("device", 5, True)
        fake._backup("activity", 101, True)
        proxy = _wrap(fake)
        snap = await proxy.snapshot()

        # Wrong snapshot id: refused before the engine, no hub traffic.
        try:
            await proxy.sync_activity(baseline=snap.bundle, edited=snap.bundle,
                                      activity_id=101, snapshot_id="stale")
        except errors.SnapshotOutdatedError:
            pass
        else:
            raise AssertionError("outdated snapshot must be refused")
        # Device 7 was never fetched: not editable.
        try:
            await proxy.sync_device(baseline=snap.bundle, edited=snap.bundle,
                                    device_id=7, snapshot_id=snap.snapshot_id)
        except errors.SnapshotIncompleteError:
            pass
        else:
            raise AssertionError("incomplete baseline must be refused")
        assert calls == []

        # A pre-write failure (stale preflight) rebases nothing and emits nothing.
        result = await proxy.sync_device(baseline=snap.bundle, edited=snap.bundle,
                                         device_id=5, snapshot_id=snap.snapshot_id)
        assert result.failed_at == "stale_check" and result.wrote_nothing and calls == [5]
        assert result.snapshot_id == snap.snapshot_id
        assert (await proxy.snapshot()).snapshot_id == snap.snapshot_id

        # A real write rebases: the projection moves and an event says so.
        async def fire():
            await asyncio.sleep(0.01)
            await proxy.sync_activity(baseline=snap.bundle, edited=snap.bundle,
                                      activity_id=101, snapshot_id=snap.snapshot_id)

        asyncio.ensure_future(fire())
        (event,) = await _collect(proxy, 1)
        assert event.kind == "snapshot_changed" and event.payload.activity_ids == (101,)
        assert event.payload.snapshot_id != snap.snapshot_id
        assert (await proxy.snapshot()).snapshot_id == event.payload.snapshot_id

    asyncio.run(main())


def test_app_session_end_flags_cache_and_announces_without_fetch() -> None:
    async def main():
        fake = _catalog_fake(fetched_devices=(5, 7), fetched_activities=(101,))
        proxy = _wrap(fake)
        before = await proxy.snapshot()
        assert before.stale_risk is False

        async def session():
            await asyncio.sleep(0.01)
            fake.set_connected(hub=True, client=True)   # app attaches
            await asyncio.sleep(0.01)
            fake.set_connected(hub=True, client=False)  # app leaves

        asyncio.ensure_future(session())
        events = await _collect_until(proxy, "snapshot_changed")
        kinds = [e.kind for e in events]
        assert kinds.count("snapshot_changed") == 1 and "app_state" in kinds
        changed = events[-1]
        assert changed.payload.stale_risk is True
        assert changed.payload.device_ids == () and changed.payload.activity_ids == ()
        # Content did not move: same id, only provenance changed.
        assert changed.payload.snapshot_id == before.snapshot_id
        after = await proxy.snapshot()
        assert after.stale_risk and all(e.stale_risk for e in after.devices + after.activities)
        assert after.complete and after.entity("device", 5).editable  # detail kept
        assert fake.fetch_calls == [] and fake.backup_calls == []

        # A refresh of one entity clears its flag and announces again.
        async def fire():
            await asyncio.sleep(0.01)
            await proxy.refresh(device_id=5)

        asyncio.ensure_future(fire())
        (event,) = await _collect(proxy, 1)
        assert event.kind == "snapshot_changed" and event.payload.device_ids == (5,)
        latest = await proxy.snapshot()
        assert not latest.entity("device", 5).stale_risk and latest.entity("device", 7).stale_risk

    asyncio.run(main())


def test_app_session_end_skips_projection_when_nobody_listens() -> None:
    async def main():
        calls = []

        class Counting(FakeProxy):
            def assemble_hub_bundle_from_state(self, **kwargs):
                calls.append(1)
                return super().assemble_hub_bundle_from_state(**kwargs)

        fake = Counting()
        proxy = _wrap(fake)
        fake.set_connected(hub=True, client=True)
        fake.set_connected(hub=True, client=False)
        await asyncio.sleep(0.05)
        assert calls == []  # no snapshot taken, no consumer: nothing projected
        assert proxy._last_snapshot_id is None

    asyncio.run(main())


# ---------------------------------------------------------------------------
# intents and payloads (phase 3 plan, W3)
# ---------------------------------------------------------------------------


def _write_fake() -> FakeProxy:
    fake = _catalog_fake(fetched_devices=(5, 7), fetched_activities=(101,))
    fake._devices_catalog_ready = True
    fake._activities_catalog_ready = True
    return fake


def test_intents_are_refused_in_observe_mode_without_engine_calls() -> None:
    async def main():
        fake = _write_fake()
        fake.set_connected(hub=True, client=True)
        proxy = _wrap(fake)
        attempts = [
            proxy.sync_activity(baseline={}, edited={}, activity_id=101),
            proxy.sync_device(baseline={}, edited={}, device_id=5),
            proxy.add_device("Lamp", "ir"), proxy.add_activity("Read"),
            proxy.remove_device(5), proxy.remove_activity(101), proxy.reorder_devices([7, 5]),
            proxy.reorder_activities([101]), proxy.set_hub_name("Den"), proxy.erase(),
            proxy.backup(), proxy.restore({"kind": "hub_bundle"}),
            proxy.read_payload(5, 1), proxy.play(bytes(16)), proxy.learn_ir(),
        ]
        for coro in attempts:
            try:
                await coro
            except errors.HubBusyError:
                pass
            else:
                raise AssertionError("must be refused in observe mode")
        assert fake.write_calls == []

    asyncio.run(main())


def test_intents_raise_typed_rejection_and_validate_inputs() -> None:
    async def main():
        fake = _write_fake()
        fake.reject = True
        proxy = _wrap(fake)
        for coro in (proxy.add_device("Lamp", "ir"), proxy.remove_device(5),
                     proxy.set_hub_name("Den"), proxy.erase(), proxy.play(bytes(16))):
            try:
                await coro
            except errors.HubRejectedError:
                pass
            else:
                raise AssertionError("engine refusal must raise HubRejectedError")
        # Input validation happens before the engine is touched.
        fake.write_calls.clear()
        for coro in (proxy.add_device("   ", "ir"), proxy.add_device("Lamp", "tv"), proxy.add_activity(""), proxy.set_hub_name(""),
                     proxy.reorder_devices([5, 5, 7]), proxy.reorder_devices([5]),
                     proxy.reorder_activities([101, 7]), proxy.restore({"kind": "nope"})):
            try:
                await coro
            except ValueError:
                pass
            else:
                raise AssertionError("bad input must raise ValueError")
        assert fake.write_calls == []

    asyncio.run(main())


def test_add_and_remove_device_rebase_and_announce() -> None:
    async def main():
        fake = _write_fake()
        proxy = _wrap(fake)
        before = await proxy.snapshot()

        async def fire():
            await asyncio.sleep(0.01)
            new_id = await proxy.add_device("Lamp", "ir")
            assert new_id == 8
            removed = await proxy.remove_device(5)
            assert removed == models.DeviceRemoved(device_id=5, confirmed_activity_ids=(101,), impacted_activity_ids=(101,))
            await proxy.remove_activity(101)

        asyncio.ensure_future(fire())
        added, removed_ev, act_removed = await _collect(proxy, 3)
        assert added.kind == "snapshot_changed" and added.payload.device_ids == (8,)
        assert removed_ev.payload.device_ids == (5,) and removed_ev.payload.activity_ids == (101,)
        assert act_removed.payload.activity_ids == (101,) and act_removed.payload.device_ids == ()
        assert fake.write_calls == [("create_device", "Lamp", "ir"), ("delete_device", 5), ("delete_device", 101)]
        try:
            await proxy.remove_activity(5)
        except ValueError:
            pass
        else:
            raise AssertionError("a device id is not an activity")
        after = await proxy.snapshot()
        assert after.snapshot_id != before.snapshot_id
        assert [e.entity_id for e in after.devices] == [7, 8]
        assert not after.entity("device", 8).editable          # new: never fetched
        assert after.entity("activity", 101) is None           # removed

    asyncio.run(main())


def test_reorder_rename_erase_go_through_the_engine_and_announce() -> None:
    async def main():
        fake = _write_fake()
        proxy = _wrap(fake)
        events = []

        async def consume():
            async for event in proxy.events():
                if event.kind == "snapshot_changed":
                    events.append(event)
                    if len(events) == 4:
                        return

        task = asyncio.ensure_future(consume())
        await asyncio.sleep(0.01)
        await proxy.reorder_devices([7, 5])
        await proxy.reorder_activities([101])
        await proxy.set_hub_name("Den")
        await proxy.erase()
        await asyncio.wait_for(task, 2)
        assert fake.write_calls == [("reorder_devices", [7, 5]), ("reorder_activities", [101]),
                                    ("set_hub_name", "Den"), ("erase",)]
        assert events[0].payload.device_ids == (7, 5) and events[1].payload.activity_ids == (101,)
        assert (await proxy.snapshot()).devices == []

    asyncio.run(main())


def test_backup_and_restore_typed_results_and_replace() -> None:
    async def main():
        fake = _write_fake()
        proxy = _wrap(fake)
        progress: list = []
        bundle = await proxy.backup(progress=progress.append)
        assert bundle["payload_profile"] == "full_backup"
        assert progress and isinstance(progress[0], models.WriteProgress)
        assert (progress[0].entity_kind, progress[0].entity_id) == ("device", 5)

        result = await proxy.restore({"kind": "hub_bundle", "tag": "a"}, replace=True, progress=progress.append)
        assert isinstance(result, models.RestoreResult) and result.ok
        assert result.device_id_map == {3: 9} and result.restored_devices == 1
        assert result.snapshot_id == (await proxy.snapshot()).snapshot_id
        # replace=True: preflight, THEN erase, then restore.
        kinds = [c[0] for c in fake.write_calls]
        assert kinds == ["backup", "preflight", "erase", "restore"]
        assert result.restored["devices"][0]["device_id"] == 9
        # A bundle the restore would refuse never reaches the erase.
        fake.write_calls.clear()
        try:
            await proxy.restore({"kind": "hub_bundle", "tag": "bad", "schema_version": 999}, replace=True)
        except ValueError:
            pass
        else:
            raise AssertionError("an invalid bundle must be refused")
        assert [c[0] for c in fake.write_calls] == ["preflight"]

        fake.reject = True
        failed = await proxy.restore({"kind": "hub_bundle", "tag": "b"})
        assert not failed.ok and failed.failed_at == ("device", 3) and failed.wrote_nothing
        assert failed.to_dict()["failed_at"] == ["device", 3]
        assert models.RestoreResult.from_engine({"status": "failed", "failed_at": ["proxy", None]}, snapshot_id=None).failed_at == ("proxy", None)

    asyncio.run(main())


def test_payload_read_play_and_learn() -> None:
    async def main():
        fake = _write_fake()
        raw = _pkg.IrPayload.from_raw_timings([9000, 4500, 560, 560], 38000)
        fake.payloads[(5, 2)] = raw.blob
        proxy = _wrap(fake)

        got = await proxy.read_payload(5, 2)
        assert got == raw and got.kind == "raw" and got.carrier_hz == 38000
        assert await proxy.read_payload(5, 3) is None
        assert fake.write_calls[:2] == [("dump", 5, 2), ("dump", 5, 3)]

        await proxy.play(got)
        await proxy.play(got.blob)
        assert fake.write_calls[-2:] == [("play", raw.blob), ("play", raw.blob)]

        learned = await proxy.learn_ir(timeout=12)
        assert isinstance(learned, _pkg.IrPayload) and learned.kind == "descriptive"
        assert fake.write_calls[-1] == ("learn", 12)

        fake.learn_result = {"state": "timed_out", "timeout_s": 12}
        try:
            await proxy.learn_ir()
        except errors.IrLearnError as err:
            assert err.state == "timed_out"
        else:
            raise AssertionError("a timed-out learn must raise")
        fake.learn_result = {"state": "learned", "payload_hex": None}
        try:
            await proxy.learn_ir()
        except errors.IrLearnError as err:
            assert err.state == "undecodable"
        assert await proxy.cancel_learn() is True

    asyncio.run(main())


def test_ir_payload_constructors_and_command_row() -> None:
    pronto = _pkg.IrPayload.from_pronto("0000 006D 0002 0000 0158 00AB 0016 0016")
    assert pronto.kind == "raw" and pronto.descriptor is None
    assert 38000 <= pronto.carrier_hz <= 38100   # 0x006D pronto frequency word
    raw = _pkg.IrPayload.from_raw_timings([9000, 4500, 560, 560], 38000)
    assert raw.kind == "raw" and raw.carrier_hz == 38000
    assert _pkg.IrPayload.from_hex(raw.hex) == raw == _pkg.IrPayload.from_bytes(raw.blob)
    assert _pkg.IrPayload.from_hex("0x" + raw.blob.hex()) == raw

    desc = _pkg.IrPayload.from_descriptor("P:NEC1 D:4 S:5 F:21")
    assert desc.kind == "descriptive" and desc.descriptor == "P:NEC1 D:4 S:5 F:21"
    assert desc.carrier_hz is None
    row = desc.to_command_row(9, " Power ")
    assert row["command_id"] == 9 and row["name"] == "Power"
    assert row["restore_data"]["new"] is True and row["restore_data"]["library_type"] == 0x0D
    assert row["restore_data"]["decoded"] == {"class": "ir", "fields": {"descriptor": "P:NEC1 D:4 S:5 F:21"}}
    assert bytes.fromhex(row["restore_data"]["data_hex"]) == desc.blob
    assert "decoded" not in raw.to_command_row(3, "")["restore_data"]
    assert raw.to_command_row(3, "")["name"] == "Command 3"
    assert desc.to_dict()["kind"] == "descriptive"

    for bad in (b"", bytes(9)):
        try:
            _pkg.IrPayload.from_bytes(bad)
        except ValueError:
            pass
        else:
            raise AssertionError("short payloads are refused")
    try:
        _pkg.IrPayload.from_hex("zz")
    except ValueError:
        pass
    else:
        raise AssertionError("bad hex is refused")


def test_sync_progress_is_typed_write_progress_with_entity() -> None:
    async def main():
        class WithSync(FakeProxy):
            def sync_device(self, *, baseline, edited, device_id, progress_callback=None):
                progress_callback(phase="stale_check", message="Checking…", completed_steps=0,
                                  total_steps=2, current_device_id=device_id)
                progress_callback(phase="writing", message="Renaming", step_kind="device_rename",
                                  completed_steps=1, total_steps=2, current_device_id=device_id)
                return {"status": "failed", "failed_at": "device_rename (device 5)",
                        "message": "The hub rejected", "completed_steps": 1, "total_steps": 2}

        fake = WithSync()
        proxy = _wrap(fake)
        seen: list = []
        result = await proxy.sync_device(baseline={}, edited={}, device_id=5, progress=seen.append)
        await asyncio.sleep(0.02)
        assert [p.phase for p in seen] == ["stale_check", "writing"]
        assert seen[1].step_kind == "device_rename" and seen[1].entity_id == 5
        assert not result.ok and not result.wrote_nothing and result.completed_steps == 1

    asyncio.run(main())


def test_refresh_cancel_drains_the_in_flight_read_before_releasing_the_hub() -> None:
    # Review of 635ecfe, finding 4: a cancelled refresh used to release the
    # lock (and the job) while the engine thread was still reading.
    async def main():
        started, release, completed = threading.Event(), threading.Event(), threading.Event()

        class Slow(FakeProxy):
            def backup_device(self, device_id, **kwargs):
                if device_id == 5:
                    started.set()
                    assert release.wait(10)
                    completed.set()
                return super().backup_device(device_id, **kwargs)

        fake = Slow()
        fake._ready["devices"] = {5: {"name": "TV"}, 7: {"name": "Amp"}}
        fake._ready["activities"] = {}
        proxy = _wrap(fake)

        async def catalog(**kwargs):
            return []

        proxy.devices = proxy.activities = catalog     # skip the catalog bursts
        task = asyncio.ensure_future(proxy.refresh())
        assert await asyncio.get_running_loop().run_in_executor(None, started.wait, 2)
        task.cancel()
        await asyncio.sleep(0.05)
        # Still draining: the lock is held, the task is not done, nothing else runs.
        assert proxy._refresh_lock.locked() and not task.done() and not completed.is_set()
        second = asyncio.ensure_future(proxy.refresh(device_id=7))
        await asyncio.sleep(0.05)
        assert not second.done()
        release.set()
        try:
            await task
        except asyncio.CancelledError:
            pass
        assert completed.is_set()                      # the read landed before the task ended
        # The loop stopped between entities: device 7 was read by the SECOND
        # refresh only, which took the lock the moment the first released it.
        await asyncio.wait_for(second, 2)
        assert not proxy._refresh_lock.locked()
        assert [c[1] for c in fake.backup_calls] == [5, 7]

    asyncio.run(main())
