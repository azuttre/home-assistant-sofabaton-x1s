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

        # delete_device is a delegated PROXY_METHODS name.
        class WithProvision(FakeProxy):
            def delete_device(self, *args, **kwargs):
                call_thread["t"] = threading.get_ident()
                return {"ok": True}

        proxy = _wrap(WithProvision())
        assert await proxy.delete_device(5) == {"ok": True}
        assert call_thread["t"] != threading.get_ident()

    asyncio.run(main())


def test_cache_snapshot_serializers_are_not_on_facade() -> None:
    # Cache-snapshot (de)serialization is intentionally off the public
    # async surface; only reachable through .sync.
    async def main():
        proxy = _wrap(FakeProxy())
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

        def on_progress(**payload) -> None:
            progress_threads.append(threading.get_ident())
            done.set()

        result = await proxy.sync_activity(
            baseline={}, edited={}, activity_id=0x65, progress_callback=on_progress
        )
        assert result["status"] == "success"
        assert call_threads["activity"] != loop_thread

        await asyncio.wait_for(done.wait(), 5)
        assert progress_threads == [loop_thread]

        result = await proxy.sync_device(baseline={}, edited={}, device_id=3)
        assert result == {"status": "success", "completed_steps": 0, "total_steps": 0}
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
