"""Facade behaviour against the REAL engine (X1Proxy), no sockets.

The fake engine in test_aio.py models the lazy-fetch pattern but is
kinder than the real one in three ways a review caught on 2026-09-09:
the real catalog getters enqueue a request even with ``force_refresh``
off, strip the stored record body through the export view, and end a
burst on the idle timeout without anything having landed. These tests
drive ``AsyncXProxy`` over an unstarted ``X1Proxy`` whose transport
flags are poked directly, so the facade is judged against what the
engine really does.
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import sys
import time
import types
from pathlib import Path

import pytest

LIB_DIR = (
    Path(__file__).resolve().parents[2]
    / "custom_components"
    / "sofabaton_x1s"
    / "lib"
)


def _load_lib() -> types.ModuleType:
    name = "sofabaton_real_engine_test_pkg"
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
errors = importlib.import_module(f"{_pkg.__name__}.errors")
devices_mod = importlib.import_module(f"{_pkg.__name__}.devices")
x1_proxy_mod = importlib.import_module(f"{_pkg.__name__}.x1_proxy")


class _Sock:
    """Stands in for a connected hub socket: the transport only tests truthiness."""


def _engine(hub_version: str = "X1S") -> "x1_proxy_mod.X1Proxy":
    # Never started: no threads, no sockets, no mDNS. Outbound frames land
    # in the transport's local buffer and go nowhere.
    return x1_proxy_mod.X1Proxy("127.0.0.1", hub_version=hub_version, proxy_enabled=False)


def _hub_link(proxy, connected: bool) -> None:
    """Flip the hub side of the transport and fire the engine's listeners."""

    with proxy.transport._hub_lock:
        proxy.transport._hub_sock = _Sock() if connected else None
    proxy._notify_hub_state(connected)


def _end_burst_idle(proxy) -> None:
    """End the active burst the way the engine does on its idle timeout."""

    proxy._burst.tick(
        time.monotonic() + 3600.0,
        can_issue=proxy.can_issue_commands,
        sender=proxy._send_cmd_frame,
    )


def _pending_local_bytes(proxy) -> int:
    return len(proxy.transport._local_to_hub)


# ---------------------------------------------------------------------------
# (5) status() is a pure state read
# ---------------------------------------------------------------------------


def test_status_on_cold_controllable_engine_sends_nothing() -> None:
    async def main():
        engine = _engine()
        proxy = aio.AsyncXProxy.wrap(engine)
        _hub_link(engine, True)
        assert engine.can_issue_commands()
        before = _pending_local_bytes(engine)

        st = await proxy.status()

        assert st.mode == "control" and st.activities_cached == 0 and st.devices_cached == 0
        assert _pending_local_bytes(engine) == before, "status() enqueued a hub request"
        assert not engine._burst.active

    asyncio.run(main())


# ---------------------------------------------------------------------------
# (1) a burst that ends on idle is not a reply
# ---------------------------------------------------------------------------


def test_read_raises_when_burst_ends_without_a_reply() -> None:
    async def main():
        engine = _engine()
        proxy = aio.AsyncXProxy.wrap(engine)
        _hub_link(engine, True)

        read = asyncio.ensure_future(proxy.devices(timeout=2.0))
        await asyncio.sleep(0.05)
        assert engine._burst.active and engine._burst.kind == "devices"

        _end_burst_idle(engine)          # nothing landed
        with pytest.raises(errors.FetchTimeoutError):
            await read

    asyncio.run(main())


def test_initial_sync_does_not_report_ready_without_data() -> None:
    async def main():
        engine = _engine()
        proxy = aio.AsyncXProxy.wrap(engine, initial_sync=True)
        _hub_link(engine, True)
        await asyncio.sleep(0.05)
        # The banner read is a real hub exchange; with no frames it has to
        # give up before the catalogs are even requested. Wait for that.
        await asyncio.sleep(0.05)
        for _ in range(50):
            if engine._burst.active:
                _end_burst_idle(engine)
            await asyncio.sleep(0.05)
            task = proxy._initial_sync_task
            if task is not None and task.done():
                break

        assert not await proxy.wait_until_ready(timeout=0.2)
        assert not (await proxy.status()).catalog_ready
        assert not engine.get_banner_info()

    asyncio.run(main())


# ---------------------------------------------------------------------------
# (2) cancellation releases the in-flight key
# ---------------------------------------------------------------------------


def test_cancelled_fetch_releases_inflight_key() -> None:
    async def main():
        engine = _engine()
        proxy = aio.AsyncXProxy.wrap(engine)
        _hub_link(engine, True)

        first = asyncio.ensure_future(proxy.devices(timeout=5.0))
        await asyncio.sleep(0)               # cancel while the request is being issued
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert "devices" not in proxy._inflight
        assert not proxy._burst_waiters.get("devices")

        # The next read must issue its own request rather than join a
        # fetch nobody owns.
        _end_burst_idle(engine)              # clear whatever the first one started
        second = asyncio.ensure_future(proxy.devices(timeout=1.0))
        await asyncio.sleep(0.05)
        assert "devices" in proxy._inflight
        assert engine._burst.active and engine._burst.kind == "devices"
        _end_burst_idle(engine)
        with pytest.raises(errors.FetchTimeoutError):
            await second

    asyncio.run(main())


# ---------------------------------------------------------------------------
# (3) power state survives the export view
# ---------------------------------------------------------------------------


def test_devices_project_power_state_from_engine_state_rows() -> None:
    async def main():
        engine = _engine("X1S")
        proxy = aio.AsyncXProxy.wrap(engine)
        cfg = devices_mod.DeviceConfig(name="TV", brand="Sony", power_state=1)
        body = devices_mod.build_device_create_payload(cfg, hub_version="X1S")[3:]
        # Commit a catalog the way the engine does after a devices burst.
        engine.state.devices = {
            5: {"name": "TV", "brand": "Sony", "device_class": "ir", "raw_body": body}
        }
        engine._devices_catalog_ready = True

        # Sanity: the getter's export view strips the body ...
        rows, ready = engine.get_devices(force_refresh=False)
        assert ready and "raw_body" not in rows[5]
        # ... and the facade still reports the power byte.
        devs = await proxy.devices()
        assert [(d.device_id, d.power_state) for d in devs] == [(5, 1)]

    asyncio.run(main())


# ---------------------------------------------------------------------------
# (4) a drop-and-reconnect that lands before the loop runs
# ---------------------------------------------------------------------------


def test_quick_reconnect_resets_readiness_and_resyncs() -> None:
    async def main():
        engine = _engine()
        proxy = aio.AsyncXProxy.wrap(engine, initial_sync=True)

        # Pretend an earlier session completed its sync.
        _hub_link(engine, True)
        await asyncio.sleep(0.02)
        if proxy._initial_sync_task is not None:
            proxy._initial_sync_task.cancel()
        proxy._set_catalog_ready(True)
        gen = proxy._session_gen

        # Disconnect + reconnect on the engine thread before the loop runs.
        _hub_link(engine, False)
        _hub_link(engine, True)
        await asyncio.sleep(0.05)

        assert proxy._session_gen == gen + 1
        assert not (await proxy.status()).catalog_ready
        # A new sync was started for the new session.
        assert proxy._initial_sync_task is not None and not proxy._initial_sync_task.done()
        proxy._initial_sync_task.cancel()

    asyncio.run(main())


def test_stale_session_sync_cannot_mark_new_session_ready() -> None:
    async def main():
        engine = _engine()
        proxy = aio.AsyncXProxy.wrap(engine, initial_sync=True)
        _hub_link(engine, True)
        stale_gen = proxy._session_gen
        proxy._session_gen += 1               # a disconnect happened meanwhile

        async def fake_sync():
            await proxy._run_initial_sync(stale_gen)

        # Monkeypatch the three steps to succeed instantly.
        engine.fetch_banner_info = lambda **kw: ({"model": "X1S"}, True)
        proxy._await_fetch = lambda *a, **kw: asyncio.sleep(0)
        await fake_sync()
        assert not proxy._catalog_ready

    asyncio.run(main())


# ---------------------------------------------------------------------------
# engine-thread callbacks must never re-enter the transport (live finding)
# ---------------------------------------------------------------------------


def test_event_listeners_do_not_deadlock_under_transport_lock() -> None:
    """bench_190 on the X1S hung at shutdown: transport.stop() notifies hub
    state while holding its socket lock, and the facade's listener read
    can_issue_commands() on that same thread, which takes the lock."""

    import threading

    async def main():
        engine = _engine()
        proxy = aio.AsyncXProxy.wrap(engine, initial_sync=True)
        agen = proxy.events()                       # arms the event listeners
        pending = asyncio.ensure_future(agen.__anext__())
        await asyncio.sleep(0.01)

        done = threading.Event()

        def engine_thread():
            # Exactly what TransportBridge.stop() does.
            with engine.transport._hub_lock:
                engine._notify_hub_state(False)
            done.set()

        t = threading.Thread(target=engine_thread, daemon=True)
        t.start()
        for _ in range(100):
            if done.is_set():
                break
            await asyncio.sleep(0.02)
        assert done.is_set(), "hub-state listener deadlocked under the transport lock"

        first = await asyncio.wait_for(pending, 1.0)
        assert first.kind == "hub_state" and first.payload.connected is False
        pending.cancel()
        await agen.aclose()

    asyncio.run(main())


# ---------------------------------------------------------------------------
# review 2026-09-09: a sync cancelled by a quick reconnect must run again
# ---------------------------------------------------------------------------


def test_reconnect_during_active_sync_restarts_the_sync() -> None:
    # The first session's sync is mid-fetch (its task alive) when the hub
    # drops and returns before the loop turns. The reconnect used to see
    # the old task still pending and skip starting a new one; the old
    # task then died of the cancellation and nothing ever synced.
    async def main():
        engine = _engine()
        proxy = aio.AsyncXProxy.wrap(engine, initial_sync=True)
        _hub_link(engine, True)
        await asyncio.sleep(0.05)
        first = proxy._initial_sync_task
        assert first is not None and not first.done()
        banner_requests = _pending_local_bytes(engine)
        gen = proxy._session_gen

        _hub_link(engine, False)
        _hub_link(engine, True)
        await asyncio.sleep(0.05)

        assert proxy._session_gen == gen + 1
        assert first.cancelled()
        second = proxy._initial_sync_task
        assert second is not None and second is not first and not second.done()
        # The new session asked the hub for its banner again.
        assert _pending_local_bytes(engine) > banner_requests
        second.cancel()

    asyncio.run(main())


def test_sync_that_ends_without_readiness_is_retried_later(monkeypatch) -> None:
    # The banner request never lands (the engine's own wait times out).
    # The sync ends without readiness; the hub is still connected, so
    # nothing else would ever start a new one. The done-callback does,
    # after a pause.
    async def main():
        monkeypatch.setattr(aio, "INITIAL_SYNC_RETRY_S", 0.08)
        engine = _engine()
        proxy = aio.AsyncXProxy.wrap(engine, initial_sync=True)
        banner_attempts = 0

        def no_banner(**kw):
            nonlocal banner_attempts
            banner_attempts += 1
            return ({}, False)

        engine.fetch_banner_info = no_banner
        _hub_link(engine, True)
        await asyncio.sleep(0.02)
        first = proxy._initial_sync_task
        assert first is not None and first.done() and not first.cancelled()
        assert banner_attempts == 1 and not proxy._catalog_ready

        await asyncio.sleep(0.1)                 # past one retry pause, short of two
        second = proxy._initial_sync_task
        assert second is not None and second is not first
        assert banner_attempts == 2 and not proxy._catalog_ready
        # Once the session ends, the pending retry finds no link and does nothing.
        _hub_link(engine, False)
        await asyncio.sleep(0.2)
        assert banner_attempts == 2

    asyncio.run(main())


def test_refused_refresh_keeps_the_committed_catalog_on_the_real_engine() -> None:
    async def main():
        engine = _engine("X1S")
        proxy = aio.AsyncXProxy.wrap(engine)
        engine.state.devices = {5: {"name": "TV", "brand": "Sony", "device_class": "ir", "raw_body": b""}}
        engine._devices_catalog_ready = True
        _hub_link(engine, True)
        assert [d.name for d in await proxy.devices()] == ["TV"]

        with engine.transport._app_lock:
            engine.transport._app_sock = _Sock()         # the app takes the hub
        with pytest.raises(errors.HubBusyError):
            await proxy.devices(refresh=True)
        assert [d.name for d in await proxy.devices()] == ["TV"]
        assert not engine._burst.active

    asyncio.run(main())
