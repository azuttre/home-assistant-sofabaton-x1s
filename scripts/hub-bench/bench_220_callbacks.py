"""bench_220: sofabaton-x-server callback devices live program (callbacks plan, C6).

Drives a real server process over HTTP and WebSocket with BOTH hubs in
one instance and exercises the callback device end to end against the
real hubs: deploy (with a port conflict first), presses delivered by the
hub (REQ_ACTIVATE on the callback commands, which the hub answers with
exactly one HTTP callback each), bindings made with the generic routes
surviving an in-place rename, the X1 head address pinned across a
device rename, the declined update after a label edited outside the
server, the X1S power hook firing on an activity transition, delete
refused while referenced and forced, a purge from outside the server
going stale and redeploying, and adoption of a forgotten device across a
server restart.

Usage (HA entries for BOTH hubs must be disabled first):
    .venv-py313\\Scripts\\python.exe scripts\\hub-bench\\bench_220_callbacks.py <tag>

The server process imports the INSTALLED sofabaton wheel: rebuild and
reinstall it before running. Writes out/bench_220_<tag>.json, the server
log and the WebSocket log under out/logs/.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx

import bench_common  # noqa: F401  (out/ dirs, logging setup)

REPO = Path(__file__).resolve().parents[2]
TAG = sys.argv[1] if len(sys.argv) > 1 else "run"
PORT = 8481
CALLBACK_PORT = 8060
BASE = f"http://127.0.0.1:{PORT}/api/v1"
HUBS = {"X1S": "192.168.2.151", "X1": "192.168.2.108"}
HUB_LISTEN_PORT = int(os.environ.get("BENCH_HUB_LISTEN_PORT", "8200"))
APP_PORT = int(os.environ.get("BENCH_APP_PORT", "8102"))
N = 10
PRESS_WAIT_S = 12.0

REPORT: dict = {"steps": [], "problems": [], "ws": []}
T0 = time.monotonic()


def step(name: str, **fields) -> None:
    row = {"step": name, "t": round(time.monotonic() - T0, 1), **fields}
    REPORT["steps"].append(row)
    print(f"[{row['t']:6.1f}s] [{name}] " + ", ".join(f"{k}={v}" for k, v in fields.items()), flush=True)


def problem(text: str) -> None:
    REPORT["problems"].append(text)
    print("PROBLEM:", text, flush=True)


LOOP: asyncio.AbstractEventLoop | None = None


def wait_for(desc: str, pred, timeout: float, every: float = 1.0):
    """Poll ``pred``; while waiting the asyncio loop is spun so the
    WebSocket collector keeps receiving (a plain sleep would starve it)."""

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = pred()
        if value:
            return value
        if LOOP is not None and not LOOP.is_closed():
            LOOP.run_until_complete(asyncio.sleep(every))
        else:
            time.sleep(every)
    problem(f"timed out waiting for {desc} ({timeout}s)")
    return None


async def ws_collect(stop: asyncio.Event) -> None:
    import websockets

    while not stop.is_set():
        try:
            async with websockets.connect(f"ws://127.0.0.1:{PORT}/api/v1/events") as ws:
                while not stop.is_set():
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                    except asyncio.TimeoutError:
                        continue
                    m = json.loads(raw)
                    m["t"] = round(time.monotonic() - T0, 1)
                    REPORT["ws"].append(m)
        except Exception:  # noqa: BLE001  (the server restarts once in this program)
            await asyncio.sleep(1.0)


def presses(hub_id: str, since: int) -> list[dict]:
    return [m for m in REPORT["ws"][since:] if m.get("type") == "press" and m.get("hub_id") == hub_id]


def server_kinds(hub_id: str, since: int) -> list[str]:
    return [m["kind"] for m in REPORT["ws"][since:] if m.get("type") == "server_event" and m.get("hub_id") == hub_id]


class Server:
    def __init__(self, data_dir: Path, log_path: Path) -> None:
        self.data_dir = data_dir
        self.log_path = log_path
        self.proc = None
        self.log = None

    def start(self) -> None:
        self.log = open(self.log_path, "a", encoding="utf-8")
        env = dict(os.environ, PYTHONPATH=str(REPO / "sofabaton-x-server" / "src"), PYTHONUNBUFFERED="1")
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "sofabaton_server.cli", "--port", str(PORT), "--data-dir", str(self.data_dir),
             "--callback-port", str(CALLBACK_PORT), "--log-level", "info"],
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
        if self.log is not None:
            self.log.close()
            self.log = None


def main() -> None:
    log_dir = bench_common.LOG_DIR
    data_dir = bench_common.BENCH_DIR / f"bench_220_{TAG}_data"
    if data_dir.exists():
        for f in data_dir.iterdir():
            f.unlink()
    data_dir.mkdir(parents=True, exist_ok=True)
    server = Server(data_dir, log_dir / f"bench_220_{TAG}-server.log")
    # -- port conflict first: something else holds 8060 when the first deploy runs
    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    blocker.bind(("0.0.0.0", CALLBACK_PORT))
    blocker.listen(1)
    server.start()
    http = httpx.Client(base_url=BASE, timeout=30.0)
    global LOOP
    loop = asyncio.new_event_loop()
    LOOP = loop
    stop_ws = asyncio.Event()
    ws_task = None
    try:
        info = wait_for("server up", lambda: _try(lambda: http.get("/server").json()), 30)
        step("server", version=(info or {}).get("version"), instance_id=(info or {}).get("instance_id"),
             listener=(info or {}).get("callback_listener"))
        if not info:
            return
        ws_task = loop.create_task(ws_collect(stop_ws))
        _spin(loop, 1.0)

        # -- register both by host, wait for ready ------------------------------------
        ids: dict[str, str] = {}
        for model, host in HUBS.items():
            r = http.post("/hubs", json={"host": host, "hub_listen_port": HUB_LISTEN_PORT, "app_discovery_port": APP_PORT})
            body = _body(r)
            step("register", model=model, status=r.status_code, hub_id=body.get("hub_id"), detail=body.get("_text"))
            if r.status_code != 201:
                problem(f"registering {model} failed: {r.status_code} {body}")
                return
        ready = wait_for("both hubs catalog_ready", lambda: _all_ready(http), 120)
        for h in http.get("/hubs").json():
            model = next(m for m, host in HUBS.items() if host == h["config"]["host"])
            ids[model] = h["hub_id"]
        step("ready", ok=bool(ready), ids=ids)
        if not ready:
            return
        _spin(loop, 2.0)

        # -- leftovers from an earlier run: every m3tac0de device is ours, remove it
        for model, hub_id in ids.items():
            for dev in http.get(f"/hubs/{hub_id}/devices").json():
                if str(dev.get("brand") or "") == "m3tac0de":
                    gone = _job(http, hub_id, "delete", f"/hubs/{hub_id}/devices/{dev['device_id']}")
                    step("cleanup_leftover", model=model, device_id=dev["device_id"], device_name=dev.get("name"), removed=bool(gone))

        # -- X1S: deploy behind a taken port, then the listener recovers ------------------
        x1s = ids["X1S"]
        since = len(REPORT["ws"])
        rec = _job(http, x1s, "post", f"/hubs/{x1s}/callback-device",
                   json={"name": "Bench server", "slots": [{"label": "Play"}, {"label": "Pause", "long_label": "Hold pause"}],
                         "power_on_slot": 1, "input_slots": [2]})
        state = http.get("/server/callback-listener").json()
        step("x1s_deploy_port_taken", record=_short(rec), listener=state, server_events=server_kinds(x1s, since))
        if not rec or rec.get("device_id") is None:
            problem("X1S deploy failed")
            return
        if state["bound"]:
            problem("listener reported bound while the port was taken")
        blocker.close()
        state = http.post("/server/callback-listener/retry").json()
        step("listener_retry", listener=state)
        if not state["bound"] or state["bound_port"] != CALLBACK_PORT:
            problem(f"listener did not bind after retry: {state}")
            return
        _check_deployed(http, x1s, rec, model="X1S")

        # -- X1: deploy (hooks ignored there) ---------------------------------------------
        x1 = ids["X1"]
        rec_x1 = _job(http, x1, "post", f"/hubs/{x1}/callback-device",
                      json={"name": "Bench server", "slots": [{"label": "Play"}, {"label": "Pause", "long_label": "Hold pause"}],
                            "power_on_slot": 1, "input_slots": [2]})
        step("x1_deploy", record=_short(rec_x1))
        if not rec_x1 or rec_x1.get("device_id") is None:
            problem("X1 deploy failed")
            return
        _check_deployed(http, x1, rec_x1, model="X1")

        # -- presses through REQ_ACTIVATE on both -----------------------------------------
        for model, hub_id, record in (("X1S", x1s, rec), ("X1", x1, rec_x1)):
            dev = record["device_id"]
            for cid, expect in ((1, ("short", "Play")), (1 + N, ("long", "Play Long")), (2 + N, ("long", "Hold pause"))):
                since = len(REPORT["ws"])
                r = http.post(f"/hubs/{hub_id}/send", json={"entity_id": dev, "command_id": cid})
                got = wait_for(f"{model} press for command {cid}", lambda: presses(hub_id, since), PRESS_WAIT_S, every=0.2)
                first = (got or [None])[0]
                step("press", model=model, command=cid, send=_body(r).get("accepted", _body(r)),
                     press={k: first.get(k) for k in ("seq", "slot", "command_id", "label", "press_type", "resolution", "source")} if first else None,
                     count=len(got or []))
                if not first:
                    problem(f"{model}: no press arrived for command {cid}")
                    continue
                if (first["press_type"], first["label"]) != expect or first["resolution"] != "deployed":
                    problem(f"{model}: press for {cid} was {first}, expected {expect}")
                if first["source"] != HUBS[model]:
                    problem(f"{model}: press source {first['source']} is not the hub")
                if len(got) != 1:
                    problem(f"{model}: {len(got)} presses for one activation")
            page = http.get(f"/hubs/{hub_id}/presses").json()
            step("presses_ring", model=model, count=len(page["presses"]), last_seq=page["last_seq"], expired=page["expired"])
            last = page["presses"][-1]["seq"] if page["presses"] else 0
            if last:
                since_page = http.get(f"/hubs/{hub_id}/presses", params={"after": last - 1}).json()
                if [p["seq"] for p in since_page["presses"]] != [last] or since_page["expired"]:
                    problem(f"{model}: after-cursor page wrong: {since_page}")

        # -- bindings through the generic routes, then an in-place rename ----------------
        for model, hub_id, record in (("X1S", x1s, rec), ("X1", x1, rec_x1)):
            dev = record["device_id"]
            act = http.get(f"/hubs/{hub_id}/activities").json()[0]["activity_id"]
            # Row edits need the entity read in full; refresh the activity and the device.
            _job(http, hub_id, "post", f"/hubs/{hub_id}/snapshot/refresh", json={"activity_id": act})
            _job(http, hub_id, "post", f"/hubs/{hub_id}/snapshot/refresh", json={"device_id": dev})
            bind = _job(http, hub_id, "put", f"/hubs/{hub_id}/activities/{act}/buttons/VOL_UP",
                        json={"device_id": dev, "command_id": 1, "long_press": {"device_id": dev, "command_id": 1 + N}})
            fav = _job(http, hub_id, "post", f"/hubs/{hub_id}/activities/{act}/favorites",
                       json={"device_id": dev, "command_id": 2})
            step("bind", model=model, activity=act, bind=bool(bind), favorite=bool(fav))
            new_name = "Bench renamed" if model == "X1" else "Bench server"
            updated = _job(http, hub_id, "put", f"/hubs/{hub_id}/callback-device",
                           json={"name": new_name, "slots": [{"label": "Start"}, {"label": "Pause", "long_label": "Hold pause"}],
                                 "power_on_slot": 1 if model == "X1S" else None, "input_slots": [2] if model == "X1S" else []})
            step("update", model=model, record=_short(updated))
            if not updated:
                problem(f"{model}: in-place update failed")
                continue
            buttons = http.get(f"/hubs/{hub_id}/entities/{act}/buttons").json()
            favs = http.get(f"/hubs/{hub_id}/activities/{act}/favorites").json()
            kept_button = any(b.get("device_id") == dev and b.get("command_id") == 1 and str(b.get("name")) == "VOL_UP" for b in buttons)
            kept_fav = any(f.get("device_id") == dev and f.get("command_id") == 2 for f in favs)
            labels = {c["command_id"]: c["label"] for c in http.get(f"/hubs/{hub_id}/devices/{dev}/commands").json()}
            step("after_update", model=model, button_kept=kept_button, favorite_kept=kept_fav,
                 label_1=labels.get(1), label_11=labels.get(1 + N), device_name=updated["spec"]["name"])
            if not kept_button or not kept_fav:
                problem(f"{model}: a binding did not survive the in-place rename")
            if labels.get(1) != "Start" or labels.get(1 + N) != "Start Long":
                problem(f"{model}: labels after rename are {labels.get(1)!r} / {labels.get(1 + N)!r}")
            if model == "X1":
                block = _device_block(http, hub_id, dev)
                step("x1_head_after_rename", ip_address=block.get("ip_address"), device_name=block.get("name"),
                     target=updated["target"]["host"])
                if block.get("ip_address") != updated["target"]["host"]:
                    problem(f"X1: head address moved to {block.get('ip_address')} after the rename")
            since = len(REPORT["ws"])
            http.post(f"/hubs/{hub_id}/send", json={"entity_id": dev, "command_id": 1})
            got = wait_for(f"{model} press after rename", lambda: presses(hub_id, since), PRESS_WAIT_S, every=0.2)
            step("press_after_rename", model=model, label=(got or [{}])[0].get("label"))
            if not got or got[0].get("label") != "Start":
                problem(f"{model}: press after rename carried {got}")

        # -- declined: a label edited outside the callback record -----------------------------
        for model, hub_id, record in (("X1S", x1s, rec), ("X1", x1, rec_x1)):
            dev = record["device_id"]
            _job(http, hub_id, "post", f"/hubs/{hub_id}/snapshot/refresh", json={"device_id": dev})
            foreign = _job(http, hub_id, "post", f"/hubs/{hub_id}/devices/{dev}/commands/3/rename", json={"name": "Foreign"})
            r = http.put(f"/hubs/{hub_id}/callback-device",
                         json={"name": "Bench renamed" if model == "X1" else "Bench server",
                               "slots": [{"label": "Start"}, {"label": "Pause", "long_label": "Hold pause"}, {"label": "Third"}],
                               "power_on_slot": 1 if model == "X1S" else None, "input_slots": [2] if model == "X1S" else []})
            job = _wait_job(http, hub_id, r.json()["job_id"]) if r.status_code == 202 else None
            err = (job or {}).get("error") or {}
            step("declined", model=model, foreign=bool(foreign), status=r.status_code, job=(job or {}).get("status"),
                 type=err.get("type"), detail=err.get("detail"))
            if not job or job["status"] != "failed" or err.get("type") != "callback_update_declined" or "3" not in str(err.get("detail")):
                problem(f"{model}: the foreign edit was not declined as expected: {job}")
            # Put the label back so the record is in step again.
            _job(http, hub_id, "post", f"/hubs/{hub_id}/devices/{dev}/commands/3/rename", json={"name": "Button 3"})

        # -- X1S: the power hook fires on an activity transition -------------------------------
        act = http.get(f"/hubs/{x1s}/activities").json()[0]["activity_id"]
        since = len(REPORT["ws"])
        http.post(f"/hubs/{x1s}/activities/{act}/start")
        got = wait_for("X1S power-on hook press", lambda: presses(x1s, since), 20.0, every=0.2)
        _spin(loop, 6.0)
        http.post(f"/hubs/{x1s}/activities/{act}/stop")
        _spin(loop, 8.0)
        hook = [(p["slot"], p["press_type"], p["label"]) for p in presses(x1s, since)]
        step("x1s_activity_hooks", activity=act, presses=hook)
        if not got:
            problem("X1S: no press from the power-on hook on activity start")

        # -- remove: refused while referenced, then forced (X1) ----------------------------------
        r = http.delete(f"/hubs/{x1}/callback-device")
        step("x1_remove_refused", status=r.status_code, type=_body(r).get("type"), detail=_body(r).get("detail"))
        if r.status_code != 409:
            problem("X1: referenced delete was not refused")
        removed = _job(http, x1, "delete", f"/hubs/{x1}/callback-device", params={"force": "true"})
        gone = wait_for("X1 device gone from the snapshot",
                        lambda: _device_block(http, x1, rec_x1["device_id"]) is None, 30)
        step("x1_remove_forced", result=removed, gone=bool(gone),
             record=http.get(f"/hubs/{x1}/callback-device").status_code)

        # -- X1S: a purge from outside the server goes stale, then redeploys ---------------------
        dev = rec["device_id"]
        since = len(REPORT["ws"])
        purged = _job(http, x1s, "delete", f"/hubs/{x1s}/devices/{dev}")
        stale = wait_for("X1S record stale", lambda: _try(lambda: http.get(f"/hubs/{x1s}/callback-device").json()["stale"]), 30)
        state = http.get("/server/callback-listener").json()
        step("x1s_purged", purged=bool(purged), stale=bool(stale), listener_bound=state["bound"],
             server_events=server_kinds(x1s, since))
        if not stale:
            problem("X1S: the purged device was not marked stale")
        if not state["bound"]:
            problem("listener went down while a stale record exists")
        r = http.put(f"/hubs/{x1s}/callback-device", json={"name": "Bench server"})
        step("x1s_update_while_stale", status=r.status_code, type=_body(r).get("type"))
        if r.status_code != 409:
            problem("X1S: update on a stale record was not refused")
        redeployed = _job(http, x1s, "post", f"/hubs/{x1s}/callback-device/redeploy")
        step("x1s_redeploy", record=_short(redeployed))
        if not redeployed or redeployed.get("stale") or redeployed.get("device_id") is None:
            problem(f"X1S: redeploy failed: {redeployed}")
            return
        _check_deployed(http, x1s, redeployed, model="X1S")
        rec = redeployed

        # -- restart with the record lost: the next deploy adopts, no duplicate ---------------------
        before = len(http.get(f"/hubs/{x1s}/devices").json())
        stop_ws.set()
        loop.run_until_complete(asyncio.wait_for(ws_task, 5))
        server.stop()
        doc = json.loads((data_dir / "hubs.json").read_text(encoding="utf-8"))
        for row in doc["hubs"]:
            row.pop("callback_device", None)
        (data_dir / "hubs.json").write_text(json.dumps(doc), encoding="utf-8")
        stop_ws = asyncio.Event()
        server.start()
        info = wait_for("server up again", lambda: _try(lambda: http.get("/server").json()), 30)
        ws_task = loop.create_task(ws_collect(stop_ws))
        ready = wait_for("X1S ready again", lambda: _try(lambda: http.get(f"/hubs/{x1s}/status").json()["status"]["catalog_ready"]), 120)
        step("restart", up=bool(info), ready=bool(ready), record=http.get(f"/hubs/{x1s}/callback-device").status_code)
        adopted = _job(http, x1s, "post", f"/hubs/{x1s}/callback-device", json={"name": "Whatever"})
        after = len(http.get(f"/hubs/{x1s}/devices").json())
        step("adopt", record=_short(adopted), devices_before=before, devices_after=after)
        if not adopted or not adopted.get("adopted") or adopted.get("device_id") != rec["device_id"]:
            problem(f"X1S: the forgotten device was not adopted: {adopted}")
        if after != before:
            problem("X1S: a duplicate device was created instead of adopting")
        since = len(REPORT["ws"])
        http.post(f"/hubs/{x1s}/send", json={"entity_id": rec["device_id"], "command_id": 1})
        got = wait_for("X1S press after adoption", lambda: presses(x1s, since), PRESS_WAIT_S, every=0.2)
        step("press_after_adopt", press={k: got[0].get(k) for k in ("label", "resolution")} if got else None)
        if not got or got[0].get("resolution") != "deployed":
            problem("X1S: no deployed press after adoption")

        # -- cleanup: remove the X1S callback device -----------------------------------------------
        removed = _job(http, x1s, "delete", f"/hubs/{x1s}/callback-device", params={"force": "true"})
        state = wait_for("listener down", lambda: (lambda s: s if not s["wanted"] else None)(http.get("/server/callback-listener").json()), 15)
        step("x1s_cleanup", result=removed, listener=state)
        final = http.get("/server").json()
        step("final", hubs=final["hubs"], listener=final["callback_listener"], ws_messages=len(REPORT["ws"]),
             presses=len([m for m in REPORT["ws"] if m.get("type") == "press"]))
    finally:
        stop_ws.set()
        if ws_task is not None:
            try:
                loop.run_until_complete(asyncio.wait_for(ws_task, 5))
            except Exception:  # noqa: BLE001
                pass
        loop.close()
        server.stop()
        http.close()
        try:
            blocker.close()
        except OSError:
            pass


# -- helpers ---------------------------------------------------------------------------------


def _job(http: httpx.Client, hub_id: str, method: str, path: str, **kw):
    r = getattr(http, method)(path, **kw)
    if r.status_code != 202:
        problem(f"{method.upper()} {path}: {r.status_code} {_body(r)}")
        return None
    job = _wait_job(http, hub_id, r.json()["job_id"])
    if job["status"] != "done":
        problem(f"{method.upper()} {path}: job {job['status']}: {job.get('error')}")
        return None
    return job.get("result") if job.get("result") is not None else {}


def _wait_job(http: httpx.Client, hub_id: str, job_id: str, timeout: float = 400.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = http.get(f"/hubs/{hub_id}/jobs/{job_id}").json()
        if job["status"] in ("done", "failed", "cancelled"):
            return job
        # Spin the loop while a job runs, or the WebSocket collector's
        # keepalive lapses during a long deploy and the first press after
        # it is lost to a reconnect (run c6b).
        if LOOP is not None and not LOOP.is_closed():
            LOOP.run_until_complete(asyncio.sleep(0.5))
        else:
            time.sleep(0.5)
    problem(f"job {job_id} did not finish in {timeout}s")
    return job


def _check_deployed(http: httpx.Client, hub_id: str, record: dict, *, model: str) -> None:
    dev = record["device_id"]
    view = http.get(f"/hubs/{hub_id}/callback-device").json()
    commands = wait_for(f"{model} commands readable", lambda: _try(lambda: http.get(f"/hubs/{hub_id}/devices/{dev}/commands").json()), 30) or []
    labels = {c["command_id"]: c["label"] for c in commands}
    block = _device_block(http, hub_id, dev) or {}
    step("deployed", model=model, device_id=dev, commands=len(commands), label_1=labels.get(1), label_11=labels.get(1 + N),
         label_12=labels.get(2 + N), brand=block.get("brand"), head_ip=block.get("ip_address"),
         target=view.get("target"), effective=view.get("effective_destination"), hub_version=view.get("hub_version"),
         hooks=(view["spec"].get("power_on_slot"), view["spec"].get("input_slots")))
    if len(commands) != 2 * N:
        problem(f"{model}: {len(commands)} commands on the callback device, expected {2 * N}")
    if labels.get(1) != record["labels"].get("1") or labels.get(1 + N) != record["labels"].get(str(1 + N)):
        problem(f"{model}: hub labels differ from the record: {labels.get(1)!r} / {labels.get(1 + N)!r}")
    if view["target"]["host"] != view["effective_destination"]["host"] or view["target"]["port"] != CALLBACK_PORT:
        problem(f"{model}: target {view['target']} vs effective {view['effective_destination']}")
    if model == "X1" and block.get("ip_address") != view["target"]["host"]:
        problem(f"X1: head address {block.get('ip_address')} is not the target {view['target']['host']}")


def _device_block(http: httpx.Client, hub_id: str, device_id: int):
    """The device's head block after a fresh catalog read (the head IP lives
    in the catalog record; a device created this session has no raw record
    in the cache until the catalog is read again)."""

    _try(lambda: http.get(f"/hubs/{hub_id}/devices", params={"refresh": "true"}, timeout=60.0))
    snap = http.get(f"/hubs/{hub_id}/snapshot").json()
    for row in snap.get("devices") or []:
        if int(row["device"]["device_id"]) == device_id:
            return row["device"]
    return None


def _short(record) -> dict | None:
    if not record:
        return None
    return {k: record.get(k) for k in ("device_id", "stale", "adopted", "deployed", "target", "hub_version", "effective_destination")}


def _body(r: httpx.Response) -> dict:
    try:
        data = r.json()
        return data if isinstance(data, dict) else {"_text": str(data)}
    except ValueError:
        return {"_text": r.text[:300]}


def _try(fn):
    try:
        return fn()
    except Exception:  # noqa: BLE001
        return None


def _spin(loop: asyncio.AbstractEventLoop, seconds: float) -> None:
    loop.run_until_complete(asyncio.sleep(seconds))


def _all_ready(http: httpx.Client):
    hubs = http.get("/hubs").json()
    return hubs if hubs and len(hubs) == len(HUBS) and all(h["status"] and h["status"]["catalog_ready"] for h in hubs) else None


if __name__ == "__main__":
    import faulthandler
    import traceback

    faulthandler.enable()
    faulthandler.dump_traceback_later(1500, exit=True)
    try:
        main()
    except Exception:  # noqa: BLE001
        problem("driver crashed: " + traceback.format_exc()[-600:])
    out = bench_common.BENCH_DIR / f"bench_220_{TAG}.json"
    out.write_text(json.dumps(REPORT, indent=2), encoding="utf-8")
    print(f"\nreport: {out}\nproblems: {REPORT['problems'] or 'none'}", flush=True)
