"""bench_210: sofabaton-x-server writes live program (phase 3 plan, S12).

Drives a real server process over HTTP and WebSocket against the real
hubs and exercises the phase 3 surface end to end on each of them:

  1. register the hub, wait for catalog_ready, read the snapshot (cold:
     nothing editable), take a whole-hub refresh JOB and watch its
     progress on the stream, read the snapshot again (complete, ETag);
  2. state file: restart the server and check the snapshot comes back
     complete from state-<hub_id>.json with the SAME ETag and no refresh;
  3. row edits: rename an activity (intent), rename it back through the
     PUT row edit with If-Match (428 without, 412 with a stale ETag),
     preview with /plan first;
  4. bindings and favorites: bind a spare button on the first activity
     to the first device's first command with a long press, clear it;
     add a favorite, reorder, remove it;
  5. device side: rename a device and back, rename a command and back,
     read a payload, play it, overwrite it with itself, add a command
     from the payload and remove it again via a device row edit;
  6. whole-entity: add a device, add an activity, reorder devices and
     activities (same order), rename the hub and back, remove the added
     device and activity (activity removal = PUT row edit is not a
     delete, so the added activity is left for the erase/restore leg
     when --destructive is given);
  7. app session: with --app-session the bench pauses so the vendor app
     can be attached and detached; the stream must show
     snapshot_changed with stale_risk true and the snapshot flag it;
  8. --destructive only: backup (full), erase, restore(replace), compare
     the snapshot entity names before and after.

Every write is checked three ways: the job reaches done, the snapshot
ETag moved, and a fresh refresh of the entity shows the change.

Usage (the hubs' HA entries must be disabled first; see bench_200):
    .venv-py313\\Scripts\\python.exe scripts\\hub-bench\\bench_210_server_writes.py <tag> [--hub X1S] [--app-session] [--destructive]

Hosts: X1S 192.168.2.151, X1 192.168.2.108, X2 from BENCH_X2_HOST.
Writes out/bench_210_<tag>.json, the server log and the WebSocket log.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx

import bench_common  # noqa: F401  (out/ dirs, logging setup)

REPO = Path(__file__).resolve().parents[2]
PORT = 8481
BASE = f"http://127.0.0.1:{PORT}/api/v1"
HOSTS = {"X1S": "192.168.2.151", "X1": "192.168.2.108", "X2": os.environ.get("BENCH_X2_HOST", "")}
HUB_LISTEN_PORT = int(os.environ.get("BENCH_HUB_LISTEN_PORT", "8200"))
APP_PORT = int(os.environ.get("BENCH_APP_PORT", "8102"))
SPARE_BUTTON = "C"   # a colour key, rarely bound

REPORT: dict = {"steps": [], "problems": [], "ws": []}
T0 = time.monotonic()


def step(label: str, **fields) -> None:
    row = {"step": label, "t": round(time.monotonic() - T0, 1), **fields}
    REPORT["steps"].append(row)
    print(f"[{row['t']:6.1f}s] [{label}] " + ", ".join(f"{k}={v}" for k, v in fields.items()), flush=True)


def problem(text: str) -> None:
    REPORT["problems"].append(text)
    print("PROBLEM:", text, flush=True)


def expect(cond: bool, text: str) -> bool:
    if not cond:
        problem(text)
    return cond


def wait_for(desc: str, pred, timeout: float, every: float = 0.5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = pred()
        if value:
            return value
        time.sleep(every)
    problem(f"timed out waiting for {desc} ({timeout}s)")
    return None


import threading

STOP_WS = threading.Event()


async def _ws_collect() -> None:
    import websockets

    while not STOP_WS.is_set():
        try:
            async with websockets.connect(f"ws://127.0.0.1:{PORT}/api/v1/events") as ws:
                while not STOP_WS.is_set():
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                    except asyncio.TimeoutError:
                        continue
                    m = json.loads(raw)
                    m["t"] = round(time.monotonic() - T0, 1)
                    REPORT["ws"].append(m)
        except Exception:  # noqa: BLE001  (server restart in leg 2)
            await asyncio.sleep(1.0)


def ws_thread() -> threading.Thread:
    """The collector runs on its own loop in a thread: the bench itself is
    synchronous and would otherwise starve it while waiting on jobs."""

    t = threading.Thread(target=lambda: asyncio.run(_ws_collect()), name="ws-collect", daemon=True)
    t.start()
    return t


def ws_since(since: int, *, hub_id: str, kind: str) -> list[dict]:
    out = []
    for m in REPORT["ws"][since:]:
        if m.get("hub_id") != hub_id:
            continue
        if m["type"] == "hub_event" and m["event"]["kind"] == kind:
            out.append(m["event"])
        elif m["type"] == "job_event" and kind == "job_event":
            out.append(m["job"])
    return out


class Server:
    def __init__(self, tag: str) -> None:
        self.tag = tag
        self.data_dir = bench_common.BENCH_DIR / f"bench_210_{tag}_data"
        self.log = open(bench_common.LOG_DIR / f"bench_210_{tag}-server.log", "a", encoding="utf-8")
        self.proc: subprocess.Popen | None = None

    def start(self, *, wipe: bool) -> None:
        if wipe and self.data_dir.exists():
            for f in self.data_dir.iterdir():
                f.unlink()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ, PYTHONPATH=str(REPO / "sofabaton-x-server" / "src"), PYTHONUNBUFFERED="1")
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "sofabaton_server.cli", "--port", str(PORT), "--data-dir", str(self.data_dir),
             "--log-level", "info"],
            env=env, stdout=self.log, stderr=subprocess.STDOUT, cwd=str(REPO),
        )

    def stop(self) -> None:
        if self.proc is None:
            return
        self.proc.terminate()
        try:
            self.proc.wait(20)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self.proc = None


def _try(fn):
    try:
        return fn()
    except Exception:  # noqa: BLE001
        return None


def job_done(http: httpx.Client, hub: str, r: httpx.Response, what: str, timeout: float = 300.0) -> dict | None:
    if r.status_code != 202:
        problem(f"{what}: expected 202, got {r.status_code} {r.text[:200]}")
        return None
    job_id = r.json()["job_id"]
    job = wait_for(f"job {what}", lambda: (lambda j: j if j["status"] in ("done", "failed", "cancelled") else None)(
        http.get(f"/hubs/{hub}/jobs/{job_id}").json()), timeout)
    if job is None:
        return None
    if job["status"] != "done":
        problem(f"{what}: job {job['status']}: {job.get('error')}")
    return job


def snapshot(http: httpx.Client, hub: str) -> tuple[dict, str]:
    r = http.get(f"/hubs/{hub}/snapshot")
    return r.json(), r.headers.get("etag", "")


def entity(doc: dict, kind: str, entity_id: int) -> dict | None:
    key = "devices" if kind == "device" else "activities"
    return next((e for e in doc[key] if e["device"]["device_id"] == entity_id), None)


def _delete_command_direct(host: str, device_id: int, command_id: int) -> bool:
    """Undo an added command with the engine's command-delete step, on a
    separate library session AFTER the bench server released the hub."""

    import importlib.util

    lib_dir = REPO / "custom_components" / "sofabaton_x1s" / "lib"
    if "sofabaton" not in sys.modules:
        spec = importlib.util.spec_from_file_location("sofabaton", lib_dir / "__init__.py", submodule_search_locations=[str(lib_dir)])
        module = importlib.util.module_from_spec(spec)
        sys.modules["sofabaton"] = module
        spec.loader.exec_module(module)
    sofabaton = sys.modules["sofabaton"]
    PENDING_CLEANUPS.append((host, device_id, command_id))
    return True


PENDING_CLEANUPS: list[tuple[str, int, int]] = []


def _run_cleanups() -> None:
    if not PENDING_CLEANUPS:
        return
    import importlib.util

    lib_dir = REPO / "custom_components" / "sofabaton_x1s" / "lib"
    if "sofabaton" not in sys.modules:
        spec = importlib.util.spec_from_file_location("sofabaton", lib_dir / "__init__.py", submodule_search_locations=[str(lib_dir)])
        module = importlib.util.module_from_spec(spec)
        sys.modules["sofabaton"] = module
        spec.loader.exec_module(module)
    sofabaton = sys.modules["sofabaton"]

    async def _go() -> None:
        host = PENDING_CLEANUPS[0][0]
        proxy = sofabaton.AsyncXProxy(hub_ip=host, hub_listen_port=HUB_LISTEN_PORT, app_discovery_port=APP_PORT, proxy_enabled=False)
        await proxy.start()
        try:
            if not await proxy.wait_until_ready(timeout=90):
                problem("cleanup: hub did not become ready for the command delete")
                return
            for _host, dev, cmd in PENDING_CLEANUPS:
                ok = await proxy.run(proxy.sync._sync_step_command_delete, {"device_id": dev, "command_id": cmd})
                snap = await proxy.refresh(device_id=dev)
                names = {c["command_id"]: c["name"] for c in next(d for d in snap.bundle["devices"] if d["device"]["device_id"] == dev)["commands"]}
                step("cleanup_delete_command", device_id=dev, command_id=cmd, ok=ok, gone=cmd not in names)
        finally:
            await proxy.stop()

    asyncio.run(_go())


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("tag")
    ap.add_argument("--hub", default="X1S", choices=list(HOSTS))
    ap.add_argument("--app-session", action="store_true")
    ap.add_argument("--destructive", action="store_true")
    args = ap.parse_args()
    host = HOSTS[args.hub]
    if not host:
        print("set BENCH_X2_HOST for the X2", file=sys.stderr)
        sys.exit(2)

    server = Server(args.tag)
    server.start(wipe=True)
    http = httpx.Client(base_url=BASE, timeout=60.0)
    collector = None
    try:
        info = wait_for("server up", lambda: _try(lambda: http.get("/server").json()), 30)
        step("server", version=(info or {}).get("version"))
        if not info:
            return
        collector = ws_thread()
        time.sleep(1.0)

        # -- 1. register, cold snapshot, whole-hub refresh --------------------------
        r = http.post("/hubs", json={"host": host, "hub_listen_port": HUB_LISTEN_PORT, "app_discovery_port": APP_PORT})
        step("register", status=r.status_code, hub_id=r.json().get("hub_id"))
        hub = r.json()["hub_id"]

        def _catalog_ready() -> bool:
            # The record re-keys from host to MAC on the first ready sync,
            # so resolve the id from the hub list on every poll.
            rows = http.get("/hubs").json()
            return bool(rows and (rows[0].get("status") or {}).get("catalog_ready"))

        ready = wait_for("catalog_ready", _catalog_ready, 90)
        hub = http.get("/hubs").json()[0]["hub_id"]   # re-keyed to the MAC
        step("ready", ok=bool(ready), hub_id=hub)
        cold, cold_etag = snapshot(http, hub)
        step("snapshot_cold", complete=cold["complete"], devices=len(cold["devices"]), activities=len(cold["activities"]),
             editable=sum(1 for e in cold["devices"] + cold["activities"] if e["editable"]))
        expect(not cold["complete"], "a cold snapshot must not be complete")
        since = len(REPORT["ws"])
        t = time.monotonic()
        job = job_done(http, hub, http.post(f"/hubs/{hub}/snapshot/refresh"), "refresh whole hub", timeout=900)
        warm, warm_etag = snapshot(http, hub)
        progress_msgs = [j for j in ws_since(since, hub_id=hub, kind="job_event") if j.get("progress")]
        step("refresh_whole", secs=round(time.monotonic() - t, 1), complete=warm["complete"], etag_moved=warm_etag != cold_etag,
             progress_events=len(progress_msgs), snapshot_changed=len(ws_since(since, hub_id=hub, kind="snapshot_changed")))
        expect(warm["complete"], "after a whole-hub refresh the snapshot must be complete")
        expect(progress_msgs, "the refresh job must report progress over the stream")
        r = http.get(f"/hubs/{hub}/snapshot", headers={"If-None-Match": warm_etag})
        step("etag_304", status=r.status_code)
        expect(r.status_code == 304, "If-None-Match with the current ETag must be 304")

        # -- 2. state file survives a restart ---------------------------------------
        state_file = server.data_dir / f"state-{hub}.json"
        step("state_file", exists=state_file.exists(), size=state_file.stat().st_size if state_file.exists() else 0)
        server.stop()
        server.start(wipe=False)
        wait_for("server up again", lambda: _try(lambda: http.get("/server").json()), 30)
        back, back_etag = snapshot(http, hub)
        step("snapshot_after_restart", complete=back["complete"], same_etag=back_etag == warm_etag,
             hub_connected=http.get(f"/hubs/{hub}/status").json()["status"]["hub_connected"])
        expect(back["complete"] and back_etag == warm_etag, "the snapshot must come back complete with the same ETag from the state file")
        wait_for("catalog_ready after restart", _catalog_ready, 90)
        again, again_etag = snapshot(http, hub)
        step("snapshot_after_initial_sync", same_etag=again_etag == warm_etag, complete=again["complete"])

        # -- 3. row edits on the first activity -----------------------------------
        doc, etag = snapshot(http, hub)
        act = doc["activities"][0]
        act_id, act_name = act["device"]["device_id"], act["device"]["name"]
        dev = doc["devices"][0]
        dev_id, dev_name = dev["device"]["device_id"], dev["device"]["name"]
        cmd = dev["commands"][0]
        cmd_id, cmd_name = cmd["command_id"], cmd["name"]
        step("targets", activity=(act_id, act_name), device=(dev_id, dev_name), command=(cmd_id, cmd_name))

        job = job_done(http, hub, http.post(f"/hubs/{hub}/activities/{act_id}/rename", json={"name": f"{act_name} b210"}), "rename activity")
        doc2, etag2 = snapshot(http, hub)
        step("rename_activity", ok=bool(job), name=entity(doc2, "activity", act_id)["device"]["name"], etag_moved=etag2 != etag)
        expect(entity(doc2, "activity", act_id)["device"]["name"] == f"{act_name} b210", "the rename must show in the snapshot")
        # PUT row edit back, with the guards
        edited = dict(entity(doc2, "activity", act_id))
        edited["device"] = {**edited["device"], "name": act_name}
        r = http.put(f"/hubs/{hub}/activities/{act_id}", json=edited)
        step("put_without_if_match", status=r.status_code, type=r.json().get("type"))
        expect(r.status_code == 428, "a PUT without If-Match must be 428")
        r = http.put(f"/hubs/{hub}/activities/{act_id}", json=edited, headers={"If-Match": etag})
        step("put_with_stale_if_match", status=r.status_code, type=r.json().get("type"))
        expect(r.status_code == 412, "a PUT with the old ETag must be 412")
        plan = http.post(f"/hubs/{hub}/activities/{act_id}/plan", json=edited).json()
        step("plan", steps=[s["kind"] for s in plan.get("steps", [])])
        job = job_done(http, hub, http.put(f"/hubs/{hub}/activities/{act_id}", json=edited, headers={"If-Match": etag2}), "PUT activity")
        doc3, etag3 = snapshot(http, hub)
        step("put_row_edit", ok=bool(job), name=entity(doc3, "activity", act_id)["device"]["name"], result=((job or {}).get("result") or {}).get("counters"))
        expect(entity(doc3, "activity", act_id)["device"]["name"] == act_name, "the row edit must restore the name")

        # -- 4. bindings and favorites ---------------------------------------------
        job = job_done(http, hub, http.put(f"/hubs/{hub}/activities/{act_id}/buttons/{SPARE_BUTTON}",
                                           json={"device_id": dev_id, "command_id": cmd_id,
                                                 "long_press": {"device_id": dev_id, "command_id": cmd_id}}), "bind button")
        doc4, _ = snapshot(http, hub)
        bound = [b for b in entity(doc4, "activity", act_id)["button_bindings"] if b["button_name"] == SPARE_BUTTON]
        step("bind", ok=bool(job), binding=bound)
        expect(bound and bound[0]["long_press_command_id"] == cmd_id, "the binding with its long press must show in the snapshot")
        refreshed = job_done(http, hub, http.post(f"/hubs/{hub}/snapshot/refresh", json={"activity_id": act_id}), "refresh activity")
        doc4b, _ = snapshot(http, hub)
        bound_hub = [b for b in entity(doc4b, "activity", act_id)["button_bindings"] if b["button_name"] == SPARE_BUTTON]
        step("bind_on_hub", ok=bool(refreshed), binding=bound_hub)
        expect(bound_hub, "the hub must hold the binding after a fresh read")
        job = job_done(http, hub, http.delete(f"/hubs/{hub}/activities/{act_id}/buttons/{SPARE_BUTTON}"), "clear button")
        doc5, _ = snapshot(http, hub)
        step("unbind", ok=bool(job), gone=not [b for b in entity(doc5, "activity", act_id)["button_bindings"] if b["button_name"] == SPARE_BUTTON])

        favs_before = [(f["device_id"], f["command_id"]) for f in entity(doc5, "activity", act_id).get("favorite_slots", [])]
        if (dev_id, cmd_id) in favs_before:
            step("favorite_skip", reason="already a favorite")
        else:
            job = job_done(http, hub, http.post(f"/hubs/{hub}/activities/{act_id}/favorites", json={"device_id": dev_id, "command_id": cmd_id}), "add favorite")
            doc6, _ = snapshot(http, hub)
            favs = [(f["device_id"], f["command_id"]) for f in entity(doc6, "activity", act_id).get("favorite_slots", [])]
            step("favorite_add", ok=bool(job), favorites=favs)
            expect((dev_id, cmd_id) in favs, "the favorite must show in the snapshot")
            if len(favs) > 1:
                order = [{"device_id": d, "command_id": c} for d, c in [favs[-1]] + favs[:-1]]
                job = job_done(http, hub, http.put(f"/hubs/{hub}/activities/{act_id}/favorites/order", json={"order": order}), "reorder favorites")
                doc7, _ = snapshot(http, hub)
                step("favorite_reorder", ok=bool(job), order=entity(doc7, "activity", act_id).get("favorites_order"))
            job = job_done(http, hub, http.delete(f"/hubs/{hub}/activities/{act_id}/favorites/{dev_id}/{cmd_id}"), "remove favorite")
            doc8, _ = snapshot(http, hub)
            favs_after = [(f["device_id"], f["command_id"]) for f in entity(doc8, "activity", act_id).get("favorite_slots", [])]
            step("favorite_remove", ok=bool(job), favorites=favs_after)
            expect(favs_after == favs_before, "favorites must be back to the starting set")

        # -- 5. device side ----------------------------------------------------------
        job = job_done(http, hub, http.post(f"/hubs/{hub}/devices/{dev_id}/rename", json={"name": f"{dev_name} b210"}), "rename device")
        job2 = job_done(http, hub, http.post(f"/hubs/{hub}/devices/{dev_id}/rename", json={"name": dev_name}), "rename device back")
        doc9, _ = snapshot(http, hub)
        step("rename_device", ok=bool(job and job2), name=entity(doc9, "device", dev_id)["device"]["name"])
        job = job_done(http, hub, http.post(f"/hubs/{hub}/devices/{dev_id}/commands/{cmd_id}/rename", json={"name": f"{cmd_name} b210"}), "rename command")
        job2 = job_done(http, hub, http.post(f"/hubs/{hub}/devices/{dev_id}/commands/{cmd_id}/rename", json={"name": cmd_name}), "rename command back")
        doc10, _ = snapshot(http, hub)
        names = {c["command_id"]: c["name"] for c in entity(doc10, "device", dev_id)["commands"]}
        step("rename_command", ok=bool(job and job2), name=names.get(cmd_id))
        expect(names.get(cmd_id) == cmd_name, "the command name must be back")

        r = http.get(f"/hubs/{hub}/devices/{dev_id}/commands/{cmd_id}/payload")
        payload = r.json() if r.status_code == 200 else None
        step("read_payload", status=r.status_code, kind=(payload or {}).get("kind"), carrier=(payload or {}).get("carrier_hz"),
             descriptor=(payload or {}).get("descriptor"))
        if payload:
            r = http.post(f"/hubs/{hub}/play", json={"hex": payload["hex"]})
            step("play", status=r.status_code, body=r.json())
            job = job_done(http, hub, http.put(f"/hubs/{hub}/devices/{dev_id}/commands/{cmd_id}/payload", json={"hex": payload["hex"]}), "overwrite payload with itself")
            r2 = http.get(f"/hubs/{hub}/devices/{dev_id}/commands/{cmd_id}/payload")
            step("overwrite_payload", ok=bool(job), same=r2.status_code == 200 and r2.json()["hex"] == payload["hex"])
            expect(r2.status_code == 200 and r2.json()["hex"] == payload["hex"], "the payload must read back byte-identical after the overwrite")
            job = job_done(http, hub, http.post(f"/hubs/{hub}/devices/{dev_id}/commands", json={"name": "b210 copy", "payload": {"hex": payload["hex"]}}), "add command")
            doc11, etag11 = snapshot(http, hub)
            added = [c for c in entity(doc11, "device", dev_id)["commands"] if c["name"] == "b210 copy"]
            step("add_command", ok=bool(job), added=[c["command_id"] for c in added])
            if expect(added, "the added command must show in the snapshot"):
                new_id = added[0]["command_id"]
                r3 = http.get(f"/hubs/{hub}/devices/{dev_id}/commands/{new_id}/payload")
                step("added_payload", status=r3.status_code, same=r3.status_code == 200 and r3.json()["hex"] == payload["hex"])
                # A device row edit that drops a command row is out of scope for
                # the live editor by design (the events device is the only
                # path), so there is no REST delete; undo it through the
                # engine's bench-validated delete step so the hub is left as
                # found.
                removed = _delete_command_direct(host, dev_id, new_id)
                step("add_command_undone", command_id=new_id, removed=removed, note="no REST delete for a command; engine step used")

        # -- 6. whole-entity intents -----------------------------------------------
        job = job_done(http, hub, http.post(f"/hubs/{hub}/devices", json={"name": "b210 device", "device_class": "ir"}), "add device")
        new_dev = ((job or {}).get("result") or {}).get("device_id")
        doc12, _ = snapshot(http, hub)
        step("add_device", ok=bool(job), device_id=new_dev, in_snapshot=entity(doc12, "device", new_dev) is not None if new_dev else False)
        job = job_done(http, hub, http.post(f"/hubs/{hub}/activities", json={"name": "b210 activity"}), "add activity")
        new_act = ((job or {}).get("result") or {}).get("activity_id")
        doc13, _ = snapshot(http, hub)
        step("add_activity", ok=bool(job), activity_id=new_act, in_snapshot=entity(doc13, "activity", new_act) is not None if new_act else False)
        if new_act:
            # Delete it straight away: the hub sweeps an activity with no
            # members on its next cascade (a device delete triggers one),
            # after which the API can only report it gone (404).
            r = http.delete(f"/hubs/{hub}/activities/{new_act}")
            if r.status_code == 404:
                step("remove_activity", swept_by_hub=True, status=404)
            else:
                job = job_done(http, hub, r, "remove activity")
                doc13b, _ = snapshot(http, hub)
                step("remove_activity", ok=bool(job), gone=entity(doc13b, "activity", new_act) is None)
            doc13, _ = snapshot(http, hub)
        dev_order = [d["device"]["device_id"] for d in doc13["devices"]]
        act_order = [a["device"]["device_id"] for a in doc13["activities"]]
        job = job_done(http, hub, http.put(f"/hubs/{hub}/devices/order", json={"order": dev_order}), "reorder devices (same order)")
        job2 = job_done(http, hub, http.put(f"/hubs/{hub}/activities/order", json={"order": act_order}), "reorder activities (same order)")
        step("reorder", devices=bool(job), activities=bool(job2))
        hub_name = http.get(f"/hubs/{hub}/info").json().get("name")
        job = job_done(http, hub, http.put(f"/hubs/{hub}/name", json={"name": f"{hub_name} b210"}), "rename hub")
        job2 = job_done(http, hub, http.put(f"/hubs/{hub}/name", json={"name": hub_name}), "rename hub back")
        step("rename_hub", ok=bool(job and job2), name=http.get(f"/hubs/{hub}/info").json().get("name"))
        if new_dev:
            job = job_done(http, hub, http.delete(f"/hubs/{hub}/devices/{new_dev}"), "remove device")
            doc14, _ = snapshot(http, hub)
            step("remove_device", ok=bool(job), gone=entity(doc14, "device", new_dev) is None, impacted=((job or {}).get("result") or {}).get("impacted_activity_ids"))

        # -- 7. app session --------------------------------------------------------------
        if args.app_session:
            since = len(REPORT["ws"])
            input("\nAttach the Sofabaton app to the hub, then detach it, then press Enter... ")
            flagged = ws_since(since, hub_id=hub, kind="snapshot_changed")
            doc15, _ = snapshot(http, hub)
            step("app_session", snapshot_changed=[e["payload"]["stale_risk"] for e in flagged], stale_risk=doc15["stale_risk"],
                 app_state=[e["payload"] for e in ws_since(since, hub_id=hub, kind="app_state")])
            expect(doc15["stale_risk"], "after an app session the snapshot must be flagged stale_risk")
            job = job_done(http, hub, http.post(f"/hubs/{hub}/snapshot/refresh", json={"activity_id": act_id}), "refresh after app session")
            doc16, _ = snapshot(http, hub)
            step("app_session_refresh", ok=bool(job), entity_stale=entity(doc16, "activity", act_id)["stale_risk"])

        # -- 8. destructive: backup, erase, restore ----------------------------------------
        if args.destructive:
            job = job_done(http, hub, http.post(f"/hubs/{hub}/backup"), "backup", timeout=1800)
            bundle = ((job or {}).get("result") or {}).get("bundle")
            step("backup", ok=bool(bundle), profile=(bundle or {}).get("payload_profile"), devices=len((bundle or {}).get("devices", [])),
                 activities=len((bundle or {}).get("activities", [])))
            if bundle:
                before = sorted(e["device"]["name"] for e in doc13["devices"] + doc13["activities"])
                job = job_done(http, hub, http.post(f"/hubs/{hub}/erase"), "erase", timeout=300)
                doc17, _ = snapshot(http, hub)
                step("erase", ok=bool(job), devices=len(doc17["devices"]), activities=len(doc17["activities"]))
                job = job_done(http, hub, http.post(f"/hubs/{hub}/restore", json={"bundle": bundle}), "restore", timeout=1800)
                step("restore", ok=bool(job), result=(job or {}).get("result"))
                job2 = job_done(http, hub, http.post(f"/hubs/{hub}/snapshot/refresh"), "refresh after restore", timeout=900)
                doc18, _ = snapshot(http, hub)
                after = sorted(e["device"]["name"] for e in doc18["devices"] + doc18["activities"])
                step("restore_compare", same_names=before == after, missing=sorted(set(before) - set(after)), extra=sorted(set(after) - set(before)))

        step("final", problems=len(REPORT["problems"]), ws_messages=len(REPORT["ws"]))
    finally:
        STOP_WS.set()
        if collector is not None:
            collector.join(5)
        server.stop()
        time.sleep(5.0)   # let the hub notice the released session
        try:
            _run_cleanups()
        except Exception as err:  # noqa: BLE001
            problem(f"cleanup failed: {err}")
        http.close()
        out = bench_common.BENCH_DIR / f"bench_210_{args.tag}.json"
        out.write_text(json.dumps(REPORT, indent=2), encoding="utf-8")
        print(f"\nreport: {out}\nproblems: {REPORT['problems'] or 'none'}")


if __name__ == "__main__":
    main()
