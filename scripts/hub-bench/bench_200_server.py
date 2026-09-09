"""bench_200: sofabaton-x-server live program (server plan S6).

Drives a real server process over HTTP and WebSocket with BOTH hubs in
one instance: discovery (the hubs advertise while released from HA),
register one from the discovery table and one by host, initial sync and
re-key, every read on both, control on both with events, disable one hub
while the other stays connected (the shared-listener release must not
touch the other session, and the disabled hub must advertise itself
again within a minute so the official app could take it), enable it
again, remove a hub, and shut down.

Usage (HA entries for BOTH hubs must be disabled first):
    .venv-py313\\Scripts\\python.exe scripts\\hub-bench\\bench_200_server.py <tag>

Needs httpx and websockets in the venv. Writes out/bench_200_<tag>.json,
the server log and the WebSocket log under out/logs/.
"""

from __future__ import annotations

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
TAG = sys.argv[1] if len(sys.argv) > 1 else "run"
PORT = 8481
BASE = f"http://127.0.0.1:{PORT}/api/v1"
HUBS = {"X1S": "192.168.2.151", "X1": "192.168.2.108"}
SETTLE_S = 8.0
# On a host that also runs Home Assistant (its shared listener holds TCP
# 8200 and UDP 8102 for any enabled hub), register the hubs on other
# ports: BENCH_HUB_LISTEN_PORT=8201 BENCH_APP_PORT=8103. The CALL_ME tells
# the hub where to dial back, so any free port works.
HUB_LISTEN_PORT = int(os.environ.get("BENCH_HUB_LISTEN_PORT", "8200"))
APP_PORT = int(os.environ.get("BENCH_APP_PORT", "8102"))


def _ports(payload: dict) -> dict:
    return {**payload, "hub_listen_port": HUB_LISTEN_PORT, "app_discovery_port": APP_PORT}

REPORT: dict = {"steps": [], "problems": [], "ws": []}
T0 = time.monotonic()


def step(name: str, **fields) -> None:
    row = {"step": name, "t": round(time.monotonic() - T0, 1), **fields}
    REPORT["steps"].append(row)
    print(f"[{row['t']:6.1f}s] [{name}] " + ", ".join(f"{k}={v}" for k, v in fields.items()), flush=True)


def problem(text: str) -> None:
    REPORT["problems"].append(text)
    print("PROBLEM:", text, flush=True)


def wait_for(desc: str, pred, timeout: float, every: float = 1.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = pred()
        if value:
            return value
        time.sleep(every)
    problem(f"timed out waiting for {desc} ({timeout}s)")
    return None


async def ws_collect(stop: asyncio.Event) -> None:
    import websockets

    async with websockets.connect(f"ws://127.0.0.1:{PORT}/api/v1/events") as ws:
        while not stop.is_set():
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            m = json.loads(raw)
            m["t"] = round(time.monotonic() - T0, 1)
            REPORT["ws"].append(m)


def ws_kinds(hub_id: str, since: int) -> list[str]:
    out = []
    for m in REPORT["ws"][since:]:
        if m.get("hub_id") != hub_id:
            continue
        out.append(m["event"]["kind"] if m["type"] == "hub_event" else m.get("kind", m["type"]))
    return out


def main() -> None:
    log_dir = bench_common.LOG_DIR
    data_dir = bench_common.BENCH_DIR / f"bench_200_{TAG}_data"
    if data_dir.exists():
        for f in data_dir.iterdir():
            f.unlink()
    data_dir.mkdir(parents=True, exist_ok=True)
    server_log = open(log_dir / f"bench_200_{TAG}-server.log", "w", encoding="utf-8")
    env = dict(os.environ, PYTHONPATH=str(REPO / "sofabaton-x-server" / "src"), PYTHONUNBUFFERED="1")
    server = subprocess.Popen(
        [sys.executable, "-m", "sofabaton_server.cli", "--port", str(PORT), "--data-dir", str(data_dir),
         "--log-level", "info"],
        env=env, stdout=server_log, stderr=subprocess.STDOUT, cwd=str(REPO),
    )
    http = httpx.Client(base_url=BASE, timeout=20.0)
    loop = asyncio.new_event_loop()
    stop_ws = asyncio.Event()
    ws_task = None
    try:
        info = wait_for("server up", lambda: _try(lambda: http.get("/server").json()), 30)
        step("server", info=info)
        if not info:
            return
        ws_task = loop.create_task(ws_collect(stop_ws))
        _spin(loop, 1.0)

        # -- 1. discovery sees both released hubs -----------------------------
        seen = wait_for("both hubs in discovery", lambda: _both_present(http), 60)
        step("discovery", present=[(s["key"], s["config"]["host"], s["config"]["hub_version"]) for s in (seen or [])])
        if not seen:
            return
        by_host = {s["config"]["host"]: s for s in seen}

        # -- 2. register: X1S from the table, X1 by host only -------------------
        step("ports", hub_listen_port=HUB_LISTEN_PORT, app_discovery_port=APP_PORT)
        r = http.post("/hubs", json=_ports(by_host[HUBS["X1S"]]["config"]))
        body = _body(r)
        step("register_x1s_from_table", status=r.status_code, hub_id=body.get("hub_id"), detail=body.get("_text"))
        if r.status_code != 201:
            problem(f"registering X1S failed: {r.status_code} {body.get('_text') or body}")
            return
        r = http.post("/hubs", json=_ports({"host": HUBS["X1"]}))
        body = _body(r)
        step("register_x1_by_host", status=r.status_code, hub_id=body.get("hub_id"), detail=body.get("_text"))
        if r.status_code != 201:
            problem(f"registering X1 failed: {r.status_code} {body.get('_text') or body}")
            return
        r = http.post("/hubs", json={"host": HUBS["X1"]})
        step("duplicate_refused", status=r.status_code, type=_body(r).get("type"))
        if r.status_code != 409:
            problem("duplicate registration was not refused")

        # -- 3. both ready, re-keyed to MAC ----------------------------------------
        ready = wait_for("both hubs catalog_ready", lambda: _all_ready(http), 60)
        hubs = http.get("/hubs").json()
        ids = {h["config"]["host"]: h["hub_id"] for h in hubs}
        step("ready", ok=bool(ready), hubs=[(h["hub_id"], h["config"]["host"], h["status"]["mode"],
                                         h["status"]["activities_cached"], h["status"]["devices_cached"]) for h in hubs])
        for host, hub_id in ids.items():
            if len(hub_id) != 12:
                problem(f"{host} not re-keyed to MAC: {hub_id}")
        _spin(loop, 2.0)
        for host, hub_id in ids.items():
            kinds = ws_kinds(hub_id, 0)
            step("ws_after_ready", hub=hub_id, kinds=kinds)
            if "catalog_ready" not in kinds:
                problem(f"{hub_id}: no catalog_ready on the stream")

        # -- 4. reads on both ---------------------------------------------------------
        for host, hub_id in ids.items():
            h = f"/hubs/{hub_id}"
            t = time.monotonic()
            info = http.get(f"{h}/info").json()
            acts = http.get(f"{h}/activities").json()
            devs = http.get(f"{h}/devices").json()
            first_dev = devs[0]["device_id"]
            first_act = acts[0]["activity_id"]
            cmds = http.get(f"{h}/devices/{first_dev}/commands").json()
            btns = http.get(f"{h}/entities/{first_act}/buttons").json()
            macros = http.get(f"{h}/activities/{first_act}/macros").json()
            favs = http.get(f"{h}/activities/{first_act}/favorites").json()
            missing = http.get(f"{h}/devices/238/commands")
            step("reads", hub=hub_id, model=info.get("model"), activities=len(acts), devices=len(devs),
                 commands=len(cmds), buttons=len(btns), macros=len(macros), favorites=len(favs),
                 power=[d["power_state"] for d in devs][:6], missing=missing.status_code,
                 secs=round(time.monotonic() - t, 2))
            if missing.status_code != 404:
                problem(f"{hub_id}: unknown device did not 404")
            if not acts or not devs:
                problem(f"{hub_id}: empty catalog")

        # -- 5. control on both --------------------------------------------------
        for host, hub_id in ids.items():
            h = f"/hubs/{hub_id}"
            act = http.get(f"{h}/activities").json()[0]["activity_id"]
            since = len(REPORT["ws"])
            r = http.post(f"{h}/activities/{act}/start").json()
            _spin(loop, SETTLE_S)
            running = http.get(f"{h}/activity").json()
            fresh = http.get(f"{h}/devices?refresh=true").json()
            r2 = http.post(f"{h}/activities/{act}/stop").json()
            _spin(loop, SETTLE_S)
            after = http.get(f"{h}/activity").json()
            kinds = ws_kinds(hub_id, since)
            step("control", hub=hub_id, activity=act, start=r, running=running, stop=r2, after=after,
                 power_refreshed=[(d["name"], d["power_state"]) for d in fresh][:4],
                 activity_events=kinds.count("activity_changed"))
            if not (running and running.get("activity_id") == act):
                problem(f"{hub_id}: activity did not start")
            if after is not None:
                problem(f"{hub_id}: activity did not stop")
            if kinds.count("activity_changed") != 2:
                problem(f"{hub_id}: expected 2 activity_changed, got {kinds}")

        # -- 6. disable X1S while X1 stays: release must not touch X1 -------------
        x1s, x1 = ids[HUBS["X1S"]], ids[HUBS["X1"]]
        since = len(REPORT["ws"])
        r = http.post(f"/hubs/{x1s}/disable").json()
        step("disable_x1s", enabled=r["enabled"], status=r["status"])
        _spin(loop, 3.0)
        x1_status = http.get(f"/hubs/{x1}/status").json()["status"]
        step("x1_during_release", mode=x1_status["mode"], ready=x1_status["catalog_ready"],
             x1_ws=ws_kinds(x1, since))
        if x1_status["mode"] != "control":
            problem("X1 lost its session during the X1S release")
        if "hub_state" in ws_kinds(x1, since):
            problem("X1 saw a link event during the X1S release")
        # The released hub must advertise itself again (the app could take it).
        # It only gives up its dial-back loop on a REFUSED connection, and a
        # Windows host with the firewall's stealth mode never answers a SYN
        # to a closed port (it times out instead), so this is only
        # verifiable on a Linux host (the server's target).
        back = _wait_advertised(http, HUBS["X1S"], "X1S advertising again")
        step("x1s_discoverable_while_disabled", ok=back, secs=round(time.monotonic() - T0, 1))
        r = http.get(f"/hubs/{x1s}/activities")
        step("disabled_read", status=r.status_code, type=r.json().get("type"))
        if r.status_code != 409:
            problem("read on a disabled hub was not 409")

        # -- 7. enable again ----------------------------------------------------
        since = len(REPORT["ws"])
        r = http.post(f"/hubs/{x1s}/enable").json()
        ready = wait_for("X1S ready after enable", lambda: http.get(f"/hubs/{x1s}/status").json()["status"] and http.get(f"/hubs/{x1s}/status").json()["status"]["catalog_ready"], 60)
        _spin(loop, 1.0)
        step("enable_x1s", ok=bool(ready), ws=ws_kinds(x1s, since))

        # -- 8. remove X1: it must be advertised again --------------------------
        r = http.delete(f"/hubs/{x1}")
        step("remove_x1", status=r.status_code)
        back = _wait_advertised(http, HUBS["X1"], "X1 advertising again after removal")
        step("x1_discoverable_after_removal", ok=back)

        # -- 9. final ---------------------------------------------------------------
        final = http.get("/server").json()
        drops = [m for m in REPORT["ws"] if m["type"] == "dropped"]
        step("final", hubs=final["hubs"], features=final["features"], ws_messages=len(REPORT["ws"]), dropped=len(drops))
    finally:
        stop_ws.set()
        if ws_task is not None:
            try:
                loop.run_until_complete(asyncio.wait_for(ws_task, 5))
            except Exception:  # noqa: BLE001
                pass
        loop.close()
        server.terminate()
        try:
            server.wait(10)
        except subprocess.TimeoutExpired:
            server.kill()
        server_log.close()
        http.close()


def _wait_advertised(http: httpx.Client, host: str, desc: str):
    """True/False on Linux; the string 'unverifiable on win32' on Windows."""

    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if _present(http, host):
            return True
        time.sleep(1.0)
    if sys.platform == "win32":
        note = f"{desc}: not observed within 60s; unverifiable on win32 (closed ports do not refuse)"
        REPORT.setdefault("caveats", []).append(note)
        print("CAVEAT:", note, flush=True)
        return "unverifiable on win32"
    problem(f"timed out waiting for {desc} (60s)")
    return False


def _body(r: httpx.Response) -> dict:
    """The JSON body, or {'_text': ...} for a non-JSON reply (a 500 page)."""

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


def _present(http: httpx.Client, host: str):
    for s in http.get("/discovery/hubs").json():
        if s["config"]["host"] == host and s["present"]:
            return s
    return None


def _both_present(http: httpx.Client):
    seen = [s for s in http.get("/discovery/hubs").json() if s["present"]]
    hosts = {s["config"]["host"] for s in seen}
    return seen if set(HUBS.values()) <= hosts else None


def _all_ready(http: httpx.Client):
    hubs = http.get("/hubs").json()
    return hubs if hubs and all(h["status"] and h["status"]["catalog_ready"] for h in hubs) else None


if __name__ == "__main__":
    import faulthandler
    import traceback

    faulthandler.enable()
    faulthandler.dump_traceback_later(420, exit=True)
    try:
        main()
    except BaseException as err:  # noqa: BLE001
        traceback.print_exc()
        REPORT["problems"].append(f"crashed: {type(err).__name__}: {err}")
    path = bench_common.save_json(f"bench_200_{TAG}", REPORT)
    print("problems:", REPORT["problems"] or "none", flush=True)
    print("saved", path, flush=True)
