"""bench_190: the sofabaton-x facade against a real hub (phase 1 acceptance).

The phase 1 plan's completeness test is that a server can be written
against ``AsyncXProxy`` using root names only, never ``.sync``. This
bench IS that server sketch, run live: config record -> proxy -> initial
sync -> status / hub_info -> the six typed reads -> the event stream
while an activity is switched on and off -> device power state through
a forced catalog refresh. Everything it touches is a root export.

Usage (HA entry for the hub must be disabled first, single-session rule):
    .venv-py313\\Scripts\\python.exe scripts\\hub-bench\\bench_190_facade.py <ip> <X1|X1S|X2> <tag>

Writes out/bench_190_<tag>.json and a frame log under out/logs/.
"""

from __future__ import annotations

import asyncio
import sys
import time

import bench_common  # noqa: F401  (loads the lib under the x1slib alias)
from x1slib import (  # noqa: E402  root exports only, on purpose
    AsyncXProxy,
    FetchTimeoutError,
    HubBusyError,
    HubConfig,
    HubNotConnectedError,
)

HOST, HVER, TAG = sys.argv[1], sys.argv[2], sys.argv[3]
EVENT_SETTLE_S = 8.0


def _t0() -> float:
    return time.monotonic()


async def _collect_events(proxy: AsyncXProxy, sink: list, stop: asyncio.Event) -> None:
    async for event in proxy.events():
        sink.append({"t": round(_t0(), 3), **event.to_dict()})
        if stop.is_set():
            break


REPORT: dict = {"host": HOST, "hub_version": HVER, "steps": [], "problems": []}


async def main() -> dict:
    report = REPORT

    def step(name: str, **fields) -> None:
        row = {"step": name, "t": round(_t0(), 3), **fields}
        report["steps"].append(row)
        print(f"[{name}] " + ", ".join(f"{k}={v}" for k, v in fields.items()))

    def problem(text: str) -> None:
        report["problems"].append(text)
        print("PROBLEM:", text)

    cfg = HubConfig(host=HOST, hub_version=HVER, proxy_enabled=False, source="manual")
    step("config", record=cfg.to_dict())
    assert HubConfig.from_dict(cfg.to_dict()) == cfg

    proxy = AsyncXProxy.from_config(cfg, diag_dump=True, diag_parse=True)
    events: list = []
    stop = asyncio.Event()
    collector = asyncio.ensure_future(_collect_events(proxy, events, stop))

    async with proxy:
        t_start = _t0()
        connected = await proxy.wait_connected(timeout=60)
        step("wait_connected", ok=connected, secs=round(_t0() - t_start, 2))
        if not connected:
            problem("hub never connected")
            return report

        ready = await proxy.wait_until_ready(timeout=60)
        st = await proxy.status()
        step("wait_until_ready", ok=ready, secs=round(_t0() - t_start, 2), status=st.to_dict())
        if not ready:
            problem(f"initial sync did not complete (mode={st.mode})")
        if st.mode != "control":
            problem(f"expected control mode, got {st.mode}")

        info = await proxy.hub_info()
        step("hub_info", info=info.to_dict())
        if not info.known or info.model != HVER:
            problem(f"hub_info unexpected: {info.to_dict()}")

        # -- typed catalogs (served from the initial sync, no new fetch) ----
        t = _t0()
        acts = await proxy.activities()
        devs = await proxy.devices()
        step("catalogs", activities=len(acts), devices=len(devs), secs=round(_t0() - t, 3),
             activity_rows=[a.to_dict() for a in acts], device_rows=[d.to_dict() for d in devs])
        if not acts or not devs:
            problem("empty catalog after initial sync")
        power_seen = [d.power_state for d in devs]
        if all(p is None for p in power_seen):
            problem("no device carried a power_state")

        # -- per-entity reads on the first device and first activity --------
        if devs:
            dev = devs[0]
            t = _t0()
            cmds = await proxy.commands(dev.device_id)
            btns = await proxy.buttons(dev.device_id)
            step("device_detail", device_id=dev.device_id, entity_name=dev.name, commands=len(cmds),
                 buttons=len(btns), secs=round(_t0() - t, 2),
                 sample_commands=[c.to_dict() for c in cmds[:5]], sample_buttons=[b.to_dict() for b in btns[:5]])
        if acts:
            act = acts[0]
            t = _t0()
            macros = await proxy.macros(act.activity_id)
            favs = await proxy.favorites(act.activity_id)
            abtns = await proxy.buttons(act.activity_id)
            step("activity_detail", activity_id=act.activity_id, entity_name=act.name, macros=len(macros),
                 favorites=len(favs), buttons=len(abtns), secs=round(_t0() - t, 2),
                 sample_favorites=[f.to_dict() for f in favs[:5]])

        # -- control + events: switch the first activity on, then off -------
        if acts:
            act = acts[0]
            before = len(events)
            t = _t0()
            ok = await proxy.start_activity(act.activity_id)
            await asyncio.sleep(EVENT_SETTLE_S)
            cur = await proxy.current_activity()
            st = await proxy.status()
            new_events = events[before:]
            kinds = [e["kind"] for e in new_events]
            step("start_activity", activity_id=act.activity_id, ok=ok, secs=round(_t0() - t, 2),
                 current=cur, running=st.running_activity.to_dict() if st.running_activity else None,
                 events=kinds)
            if "activity_changed" not in kinds:
                problem("no activity_changed event after start_activity")
            if not cur or cur.get("activity_id") != act.activity_id:
                problem(f"current_activity after start: {cur}")

            # Power state is live in the device row but the facade serves the
            # cached catalog; ask for a refresh (fetch-then-prune, the
            # cached list stays readable until the reply lands).
            t = _t0()
            devs2 = await proxy.devices(refresh=True)
            step("devices_after_start", secs=round(_t0() - t, 2),
                 power=[(d.device_id, d.name, d.power_state) for d in devs2])

            before = len(events)
            t = _t0()
            ok = await proxy.stop_activity(act.activity_id)
            await asyncio.sleep(EVENT_SETTLE_S)
            cur = await proxy.current_activity()
            kinds = [e["kind"] for e in events[before:]]
            step("stop_activity", ok=ok, secs=round(_t0() - t, 2), current=cur, events=kinds)
            if cur is not None:
                problem(f"current_activity after stop: {cur}")

            t = _t0()
            devs3 = await proxy.devices(refresh=True)
            step("devices_after_stop", secs=round(_t0() - t, 2),
                 power=[(d.device_id, d.name, d.power_state) for d in devs3])

        # -- typed errors are the only failure shape ------------------------
        try:
            await proxy.commands(0xEE, timeout=3.0)   # a device that does not exist
            step("missing_device_read", result="returned")
        except (HubBusyError, HubNotConnectedError, FetchTimeoutError) as err:
            step("missing_device_read", error=type(err).__name__, text=str(err))

        st = await proxy.status()
        step("final_status", status=st.to_dict(), events_total=len(events),
             events_dropped=proxy.events_dropped)

    stop.set()
    collector.cancel()
    report["events"] = events
    return report


if __name__ == "__main__":
    import faulthandler
    import functools
    import traceback

    # A hung shutdown (executor thread stuck, engine thread alive) dumps
    # every thread's stack and exits instead of sitting there forever.
    faulthandler.enable()
    faulthandler.dump_traceback_later(150, exit=True)
    print = functools.partial(print, flush=True)  # noqa: A001

    bench_common.setup_logging(f"bench_190_{TAG}")
    result: dict = {"host": HOST, "hub_version": HVER, "steps": [], "problems": []}
    try:
        result = asyncio.run(main())
    except BaseException as err:  # noqa: BLE001
        traceback.print_exc()
        result = REPORT
        result["problems"].append(f"crashed: {type(err).__name__}: {err}")
    path = bench_common.save_json(f"bench_190_{TAG}", result)
    print("problems:", result["problems"] or "none")
    print("saved", path)
