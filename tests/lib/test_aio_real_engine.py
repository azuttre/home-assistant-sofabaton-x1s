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
import threading
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


# ---------------------------------------------------------------------------
# snapshot / state document on the real engine (phase 3 plan, W0)
# ---------------------------------------------------------------------------


def _seed_catalog(proxy) -> None:
    """A device and an activity in the catalog, as the initial sync leaves them."""

    proxy.state.devices[5] = {"name": "TV", "brand": "Acme", "device_class": "tv"}
    proxy.state.activities[101] = {"name": "Watch TV"}
    proxy._devices_catalog_ready = True
    proxy._activities_catalog_ready = True


def test_snapshot_on_cold_engine_sends_nothing_and_is_incomplete() -> None:
    async def main():
        engine = _engine()
        _hub_link(engine, True)
        proxy = aio.AsyncXProxy.wrap(engine)
        snap = await proxy.snapshot()
        assert _pending_local_bytes(engine) == 0
        assert snap.devices == [] and snap.activities == []
        # No catalog has been read: the empty projection is not "complete".
        assert snap.complete is False
        assert snap.bundle["payload_profile"] == "structural"

        _seed_catalog(engine)
        snap = await proxy.snapshot()
        assert _pending_local_bytes(engine) == 0
        assert [e.entity_id for e in snap.devices] == [5]
        assert [e.entity_id for e in snap.activities] == [101]
        tv = snap.entity("device", 5)
        assert tv.name == "TV" and not tv.complete and not tv.editable
        assert snap.bundle["devices"][0]["editable"] is False
        assert "stale_risk" not in snap.bundle["devices"][0]

    asyncio.run(main())


def test_state_document_round_trip_keeps_id_generation_and_stamps() -> None:
    async def main():
        engine = _engine()
        _seed_catalog(engine)
        # Pretend the activity was fetched.
        engine._note_detail_fetched("activity", 101)
        proxy = aio.AsyncXProxy.wrap(engine)
        origin = await proxy.snapshot()
        assert origin.entity("activity", 101).fetched_at
        assert origin.entity("device", 5).fetched_at is None  # never fetched
        doc = await proxy.export_state()
        assert "detail_stale_risk" not in doc["state"]
        assert doc["state"]["detail_fetched_at"]["activity"] == {"101": origin.entity("activity", 101).fetched_at}

        fresh = _engine()
        other = aio.AsyncXProxy.wrap(fresh)
        restored = await other.import_state(doc)
        assert restored.snapshot_id == origin.snapshot_id
        assert restored.entity("activity", 101).fetched_at == origin.entity("activity", 101).fetched_at
        assert restored.engine_generation > origin.engine_generation
        assert _pending_local_bytes(fresh) == 0

        # A document from before 2026-09-10 carries a stale-flag table: ignored.
        legacy = {**doc, "state": {**doc["state"], "detail_stale_risk": {"device": [], "activity": [101]}}}
        assert (await aio.AsyncXProxy.wrap(_engine()).import_state(legacy)).snapshot_id == origin.snapshot_id

        # A cache clear bumps the generation.
        before = fresh.state.generation
        fresh.clear_cached_entity_detail(101, kind="activity")
        assert fresh.state.generation > before

    asyncio.run(main())


def test_burst_end_bumps_generation() -> None:
    engine = _engine()
    before = engine.state.generation
    engine._burst.start("devices")
    _end_burst_idle(engine)
    assert engine.state.generation > before


def test_sync_refuses_unfetched_baseline_before_the_engine() -> None:
    async def main():
        engine = _engine()
        _hub_link(engine, True)
        _seed_catalog(engine)
        proxy = aio.AsyncXProxy.wrap(engine)
        snap = await proxy.snapshot()
        try:
            await proxy.sync_activity(baseline=snap.bundle, edited=snap.bundle, activity_id=101)
        except errors.SnapshotIncompleteError:
            pass
        else:
            raise AssertionError("an unfetched activity is not an editable baseline")
        assert _pending_local_bytes(engine) == 0

    asyncio.run(main())


def test_app_session_end_leaves_the_engine_cache_alone() -> None:
    # stale_risk removed 2026-09-10: an app session is not evidence the
    # cache is fresh or stale, so the engine records nothing about it.
    engine = _engine()
    _seed_catalog(engine)
    engine._note_detail_fetched("activity", 101)
    before = engine.state.generation
    engine._notify_client_state(True)
    engine._notify_client_state(False)
    assert engine.state.generation == before
    assert not hasattr(engine.state, "detail_stale_risk")

def test_app_session_end_reaches_facade_consumers_from_the_engine_thread() -> None:
    # An app session ending on the engine thread is delivered as app_state
    # only; the snapshot does not move (stale_risk removed 2026-09-10).
    async def main():
        engine = _engine()
        _seed_catalog(engine)
        engine._note_detail_fetched("activity", 101)
        _hub_link(engine, True)
        proxy = aio.AsyncXProxy.wrap(engine)
        before = await proxy.snapshot()

        async def session():
            await asyncio.sleep(0.01)
            def _engine_thread():
                engine._notify_client_state(True)
                engine._notify_client_state(False)
            t = threading.Thread(target=_engine_thread)
            t.start(); t.join()

        asyncio.ensure_future(session())
        agen = proxy.events()
        seen = []
        try:
            while not any(e.kind == "app_state" and e.payload.connected is False for e in seen):
                seen.append(await asyncio.wait_for(agen.__anext__(), 2))
            while True:
                try:
                    seen.append(await asyncio.wait_for(agen.__anext__(), 0.05))
                except asyncio.TimeoutError:
                    break
        finally:
            await agen.aclose()
        assert not any(e.kind == "snapshot_changed" for e in seen)
        after = await proxy.snapshot()
        assert after.snapshot_id == before.snapshot_id and after.engine_generation == before.engine_generation
        assert after.entity("activity", 101).fetched_at == before.entity("activity", 101).fetched_at
        assert _pending_local_bytes(engine) == 0

    asyncio.run(main())


# ---------------------------------------------------------------------------
# W2: intents fetch their own preconditions (phase 3 plan, P3.2)
# ---------------------------------------------------------------------------


def _ok_step(*args, **kwargs):
    ack = importlib.import_module(f"{_pkg.__name__}.ack")
    return ack.SendStepResult(outcome=ack.AckOutcome.acked, ack_opcode=0x0103, ack_payload=b"\x00")


def test_reorder_devices_reads_the_catalog_when_cold_and_refuses_without_it(monkeypatch) -> None:
    engine = _engine()
    _hub_link(engine, True)
    reads = []
    monkeypatch.setattr(engine, "_request_devices_and_wait", lambda **kw: reads.append(1) or False)
    sent = []
    monkeypatch.setattr(engine, "_send_step", lambda **kw: sent.append(kw) or _ok_step())
    assert engine.reorder_devices([5, 7]) is None
    assert reads == [1] and sent == []


def test_reorder_devices_never_writes_a_guessed_kind_byte(monkeypatch) -> None:
    engine = _engine()
    _hub_link(engine, True)
    engine._devices_catalog_ready = True
    engine.state.devices[5] = {"name": "TV", "raw_body": bytes([0, 0, 0, 0x21, 0])}
    engine.state.devices[7] = {"name": "Fresh"}          # created in place, no record yet
    sent = []
    monkeypatch.setattr(engine, "_send_step", lambda **kw: sent.append(kw) or _ok_step())

    def _catalog_read(**kw):
        engine.state.devices[7]["raw_body"] = bytes([0, 0, 0, 0x33, 0])
        return True

    monkeypatch.setattr(engine, "_request_devices_and_wait", _catalog_read)
    result = engine.reorder_devices([7, 5])
    assert result is not None
    sort = sent[0]["payload"]
    # Rows are (marker, id, position): the fetched record's kind byte, never 0x00.
    assert bytes([0x33, 7, 1, 0x21, 5, 2]) in sort


def test_reorder_activities_reads_the_catalog_when_cold(monkeypatch) -> None:
    engine = _engine()
    _hub_link(engine, True)
    reads = []

    def _catalog_read(**kw):
        reads.append(1)
        engine.state.activities[101] = {"name": "Watch TV"}
        engine._activities_catalog_ready = True
        return True

    monkeypatch.setattr(engine, "_request_activities_and_wait", _catalog_read)
    sent = []
    monkeypatch.setattr(engine, "_send_step", lambda **kw: sent.append(kw) or _ok_step())
    assert engine.reorder_activities([101]) is not None
    # One read before the write (the trailing refresh is the second), then sort + remote sync.
    assert reads and len(sent) == 2


def test_persist_ir_blob_reads_the_command_table_before_allocating(monkeypatch) -> None:
    engine = _engine()
    _hub_link(engine, True)
    fetched = []

    def _fetch(key, kick, ready, *, timeout):
        fetched.append(key)
        engine.state.commands[5] = {1: "Power", 2: "Mute"}   # the hub's table
        engine._commands_complete.add(5)
        return True

    monkeypatch.setattr(engine, "_fetch_and_wait", _fetch)
    written = {}

    def _write(**kw):
        written.update(kw)
        return {"status": "success", "page_count": 1, "command_id": kw["command_id"]}

    monkeypatch.setattr(engine, "_run_persist_write", _write)
    monkeypatch.setattr(engine, "_register_command_in_device_sort", lambda **kw: None)
    result = engine.persist_ir_blob(device_id=5, command_name="Input", blob=bytes(16))
    assert result is not None
    assert fetched == ["commands:5"]
    assert written["command_id"] == 3          # next free slot, not 1


def test_persist_refuses_when_the_command_table_cannot_be_read(monkeypatch) -> None:
    engine = _engine()
    _hub_link(engine, True)
    monkeypatch.setattr(engine, "_fetch_and_wait", lambda *a, **kw: False)
    written = []
    monkeypatch.setattr(engine, "_run_persist_write", lambda **kw: written.append(kw))
    assert engine.persist_ir_blob(device_id=5, command_name="Input", blob=bytes(16)) is None
    assert engine.persist_command_record(
        device_id=5, command_name="Pair", library_type=0x03, command_data=b"\x01"
    ) is None
    assert written == []


def test_persist_skips_the_read_when_the_table_is_complete(monkeypatch) -> None:
    engine = _engine()
    _hub_link(engine, True)
    engine.state.commands[5] = {1: "Power"}
    engine._commands_complete.add(5)
    monkeypatch.setattr(engine, "_fetch_and_wait", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("no read expected")))
    written = {}
    monkeypatch.setattr(engine, "_run_persist_write", lambda **kw: written.update(kw) or {"status": "success", "page_count": 1})
    monkeypatch.setattr(engine, "_register_command_in_device_sort", lambda **kw: None)
    assert engine.persist_ir_blob(device_id=5, command_name="Input", blob=bytes(16)) is not None
    assert written["command_id"] == 2


def test_delete_device_scope_needs_no_prefetch() -> None:
    # Audit result recorded as a test: the scan only names activities whose
    # detail IS cached (unfetched detail cannot be stale), and the confirm
    # scope comes from the hub's own activities burst inside delete_device.
    engine = _engine()
    engine.state.activities[101] = {"name": "Watch TV"}
    engine.state.activities[102] = {"name": "Music"}
    engine.state.activity_members[101] = {5}
    assert engine.activities_referencing_device(5) == [101]


def test_state_document_keeps_the_favorites_order() -> None:
    # Found live (bench_210, X1): the quick-access slot order was not in
    # the state document, so the projected activity lost its
    # ``favorites_order`` after a restart and the snapshot id moved.
    async def main():
        engine = _engine()
        _seed_catalog(engine)
        engine.state.activity_favorite_slots[101] = [
            {"button_id": 1, "device_id": 5, "command_id": 2},
            {"button_id": 2, "device_id": 5, "command_id": 3},
        ]
        engine.state.activity_favorites_order[101] = [(2, 0), (1, 1)]
        engine._note_detail_fetched("activity", 101)
        proxy = aio.AsyncXProxy.wrap(engine)
        origin = await proxy.snapshot()
        assert _bundle_activity(origin, 101)["favorites_order"] == [2, 1]

        fresh = _engine()
        other = aio.AsyncXProxy.wrap(fresh)
        restored = await other.import_state(await proxy.export_state())
        assert _bundle_activity(restored, 101)["favorites_order"] == [2, 1]
        assert restored.snapshot_id == origin.snapshot_id

    asyncio.run(main())


def _bundle_activity(snap, activity_id):
    return next(a for a in snap.bundle["activities"] if a["device"]["device_id"] == activity_id)


def test_key_sort_timeout_on_a_network_device_records_an_empty_row() -> None:
    # Found live (bench_210, X1): the hub never answers the key-sort read
    # for a wifi_sonos device, which left the capture incomplete forever.
    export = importlib.import_module(f"{_pkg.__name__}.proxy_backup_export")
    empty = {"device_id": 4, "msg_hex": ""}
    assert export._key_sort_row_or_fallback(4, "wifi_sonos", None) == empty
    assert export._key_sort_row_or_fallback(4, "wifi_hue", None) == empty
    assert export._key_sort_row_or_fallback(1, "ir", None) is None          # a real gap
    assert export._key_sort_row_or_fallback(1, None, None) is None          # class unknown: no guess
    row = {"device_id": 1, "msg_hex": "01 ff"}
    assert export._key_sort_row_or_fallback(1, "wifi_sonos", row) is row    # a reply always wins


# ---------------------------------------------------------------------------
# restore: preflight before erase, and the engine's real result shape
# (review of 635ecfe, findings 1 and 2)
# ---------------------------------------------------------------------------


def _full_bundle(**extra) -> dict:
    export = importlib.import_module(f"{_pkg.__name__}.backup_export")
    return {
        "kind": "hub_bundle", "schema_version": export.HUB_BUNDLE_SCHEMA_VERSION,
        "payload_profile": "full_backup",
        "devices": [{"kind": "device_backup", "schema_version": export.DEVICE_BACKUP_SCHEMA_VERSION,
                     "device": {"device_id": 5, "name": "TV", "device_class": "ir"}, "commands": []}],
        "activities": [],
        **extra,
    }


def _activity(**extra) -> dict:
    export = importlib.import_module(f"{_pkg.__name__}.backup_export")
    return {
        "kind": "activity_backup", "schema_version": export.ACTIVITY_BACKUP_SCHEMA_VERSION,
        "device": {"entity_type": "activity", "device_id": 0x65, "name": "Watch TV"},
        "button_bindings": [], "favorite_slots": [],
        "macros": [{"button_id": 0xC6, "steps": [{"device_id": 5, "command_id": 1}]}],
        **extra,
    }


def test_restore_preflight_rejects_a_bad_bundle_before_any_write(monkeypatch) -> None:
    async def main():
        engine = _engine()
        _hub_link(engine, True)
        writes = []
        monkeypatch.setattr(engine, "erase_configuration", lambda **kw: writes.append("erase") or True)
        monkeypatch.setattr(engine, "restore_device", lambda payload, **kw: writes.append("restore_device") or {"status": "success", "device_id": 9})
        monkeypatch.setattr(engine, "resync_remote", lambda *a, **kw: True)
        proxy = aio.AsyncXProxy.wrap(engine)
        bad = [
            _full_bundle(schema_version=999),
            _full_bundle(payload_profile="structural"),
            {**_full_bundle(), "devices": [{"kind": "device_backup", "schema_version": 999, "device": {"device_id": 5}}]},
            {**_full_bundle(), "devices": [{"kind": "device_backup", "schema_version": 1, "device": {"device_id": 5, "device_class": "no_such_class"}}]},
            # Activities are checked too (review of ce9f205, P1): schema,
            # marker, and references outside the bundle.
            _full_bundle(activities=[_activity(schema_version=999)]),
            _full_bundle(activities=[_activity(device={"device_id": 0x65, "name": "A"})]),
            _full_bundle(activities=[_activity(macros=[{"button_id": 0xC6, "steps": [{"device_id": 0x20, "command_id": 1}]}])]),
            _full_bundle(activities=[_activity(macros=[{"button_id": 0xC6, "steps": [{"device_id": 0x70, "command_id": 1}]}])]),
        ]
        for bundle in bad:
            try:
                await proxy.restore(bundle, replace=True)
            except ValueError:
                pass
            else:
                raise AssertionError(f"must refuse {bundle}")
        assert writes == []                              # never erased, never restored
        assert _pending_local_bytes(engine) == 0

    asyncio.run(main())


def test_restore_adapts_the_engine_result_shape(monkeypatch) -> None:
    async def main():
        engine = _engine()
        _hub_link(engine, True)
        monkeypatch.setattr(engine, "restore_device", lambda payload, **kw: {"status": "success", "device_id": 9, "restored_commands": 0})
        monkeypatch.setattr(engine, "resync_remote", lambda *a, **kw: True)
        proxy = aio.AsyncXProxy.wrap(engine)
        result = await proxy.restore(_full_bundle())      # the REAL restore_hub_bundle
        assert result.ok and result.restored_devices == 1 and result.restored_activities == 0
        assert result.device_id_map == {5: 9} and result.restored["devices"][0]["device_id"] == 9
        assert result.to_dict()["restored_devices"] == 1

        # A first-entity failure keeps the counts honest and is not a success.
        monkeypatch.setattr(engine, "restore_device", lambda payload, **kw: None)
        failed = await proxy.restore(_full_bundle())
        assert not failed.ok and failed.failed_at == ("device", 5) and failed.wrote_nothing

    asyncio.run(main())
