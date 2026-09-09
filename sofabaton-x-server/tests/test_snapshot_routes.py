"""Phase 3 S7 + S9: the snapshot document, the state file, jobs and their events."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sofabaton_server import API_PREFIX
from sofabaton_server.app import create_app
from sofabaton_server.config import Settings
from sofabaton_server.manager import HubManager
from sofabaton_server.store import StateStore

from fakes import Factory, no_network_discovery

HUBS = f"{API_PREFIX}/hubs"
EVENTS = f"{API_PREFIX}/events"
HOST = "192.168.1.50"


def _rig(tmp_path: Path):
    factory = Factory()
    settings = Settings(data_dir=tmp_path)
    manager = HubManager(settings, proxy_factory=factory)
    client = TestClient(create_app(settings, manager=manager, discovery=no_network_discovery(settings, manager)))
    return client, factory


def _wait_job(client, hub, job_id, *, status=("done", "failed", "cancelled"), timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = client.get(f"{HUBS}/{hub}/jobs/{job_id}").json()
        if job["status"] in status:
            return job
        time.sleep(0.01)
    raise AssertionError(f"job {job_id} did not reach {status}: {job}")


def test_snapshot_document_and_etag(tmp_path: Path) -> None:
    client, factory = _rig(tmp_path)
    with client:
        client.post(HUBS, json={"host": HOST})
        proxy = factory.latest(HOST)
        proxy.fetched = {1}
        r = client.get(f"{HUBS}/{HOST}/snapshot")
        assert r.status_code == 200
        doc = r.json()
        assert r.headers["ETag"] == f'"{doc["snapshot_id"]}"' and len(doc["snapshot_id"]) == 64
        assert doc["complete"] is False and doc["payload_profile"] == "structural"
        assert [d["device"]["device_id"] for d in doc["devices"]] == [1, 2]
        assert doc["devices"][0]["editable"] is True and doc["devices"][1]["editable"] is False
        assert doc["activities"][0]["device"]["name"] == "Watch TV"

        # Conditional read: same content, 304 with the ETag; changed content, 200.
        r2 = client.get(f"{HUBS}/{HOST}/snapshot", headers={"If-None-Match": r.headers["ETag"]})
        assert r2.status_code == 304 and r2.headers["ETag"] == r.headers["ETag"]
        proxy.fetched = {1, 2}
        r3 = client.get(f"{HUBS}/{HOST}/snapshot", headers={"If-None-Match": r.headers["ETag"]})
        assert r3.status_code == 200 and r3.json()["snapshot_id"] != doc["snapshot_id"]

        assert client.get(f"{HUBS}/nope/snapshot").status_code == 404
        client.post(f"{HUBS}/{HOST}/disable")
        assert client.get(f"{HUBS}/{HOST}/snapshot").json()["type"] == "hub_disabled"


def test_refresh_job_runs_reports_and_writes_the_state_file(tmp_path: Path) -> None:
    client, factory = _rig(tmp_path)
    with client:
        client.post(HUBS, json={"host": HOST})
        proxy = factory.latest(HOST)
        with client.websocket_connect(EVENTS) as ws:
            ws.receive_json()  # hello
            r = client.post(f"{HUBS}/{HOST}/snapshot/refresh", json={"activity_id": 101})
            assert r.status_code == 202
            job = r.json()
            assert job["kind"] == "refresh_entity" and job["cancellable"] is False
            assert job["status"] in ("queued", "running")
            done = _wait_job(client, HOST, job["job_id"])
            assert done["status"] == "done" and done["result"]["complete"] is False
            assert proxy.refresh_calls == [{"device_id": None, "activity_id": 101}]
            assert done["progress"]["entity_id"] == 101

            seen = [ws.receive_json() for _ in range(5)]
            job_msgs = [m for m in seen if m["type"] == "job_event"]
            assert [m["job"]["status"] for m in job_msgs][:2] == ["queued", "running"]
            assert job_msgs[-1]["job"]["status"] == "done"
            assert any(m["type"] == "hub_event" and m["event"]["kind"] == "snapshot_changed" for m in seen)

        # The snapshot change persisted the library's document.
        state = StateStore(tmp_path).load(HOST)
        assert state is not None and state["state"]["fetched"] == [101]
        assert client.get(f"{HUBS}/{HOST}/jobs").json()[0]["job_id"] == job["job_id"]
        assert client.get(f"{HUBS}/{HOST}/jobs/nope").status_code == 404


def test_state_file_is_imported_on_start_renamed_on_rekey_and_deleted_on_remove(tmp_path: Path) -> None:
    store = StateStore(tmp_path)
    store.save(HOST, {"kind": "sofabaton_state", "schema": 1, "state": {"fetched": [1, 2, 101, 102]}})
    client, factory = _rig(tmp_path)
    with client:
        # The manager seeds hubs.json from initial_hubs only; register by host.
        client.post(HUBS, json={"host": HOST})
        proxy = factory.latest(HOST)
        assert proxy.imported is not None and proxy.fetched == {1, 2, 101, 102}
        assert client.get(f"{HUBS}/{HOST}/snapshot").json()["complete"] is True

        client.portal.call(proxy.ready, "E2:6A:44:86:1B:45")
        time.sleep(0.05)
        assert not store.path(HOST).exists() and store.path("e26a44861b45").exists()

        client.post(f"{HUBS}/e26a44861b45/disable")
        assert proxy.exports >= 1  # written at stop too
        client.delete(f"{HUBS}/e26a44861b45")
        assert not store.path("e26a44861b45").exists()


def test_unreadable_state_file_starts_cold(tmp_path: Path) -> None:
    StateStore(tmp_path).save(HOST, {"kind": "something_else"})
    client, factory = _rig(tmp_path)
    with client:
        client.post(HUBS, json={"host": HOST})
        proxy = factory.latest(HOST)
        assert proxy.imported is None
        assert client.get(f"{HUBS}/{HOST}/snapshot").json()["complete"] is False


def test_one_job_per_hub_and_cancel(tmp_path: Path) -> None:
    client, factory = _rig(tmp_path)
    with client:
        client.post(HUBS, json={"host": HOST})
        proxy = factory.latest(HOST)
        proxy.refresh_gate = client.portal.call(asyncio.Event)
        r = client.post(f"{HUBS}/{HOST}/snapshot/refresh")
        assert r.status_code == 202 and r.json()["cancellable"] is True
        job_id = r.json()["job_id"]
        _wait_job(client, HOST, job_id, status=("running",))

        # A second job while one runs is refused; a read is not.
        r2 = client.post(f"{HUBS}/{HOST}/snapshot/refresh", json={"device_id": 1})
        assert r2.status_code == 409 and r2.json()["type"] == "hub_job_running"
        assert client.get(f"{HUBS}/{HOST}/devices").status_code == 200

        cancelled = client.delete(f"{HUBS}/{HOST}/jobs/{job_id}")
        assert cancelled.status_code == 200
        done = _wait_job(client, HOST, job_id)
        assert done["status"] == "cancelled" and done["finished_at"]
        assert client.delete(f"{HUBS}/{HOST}/jobs/{job_id}").json()["type"] == "job_not_cancellable"

        # The hub is free again.
        client.portal.call(proxy.refresh_gate.set)
        r3 = client.post(f"{HUBS}/{HOST}/snapshot/refresh", json={"device_id": 1})
        assert r3.status_code == 202 and _wait_job(client, HOST, r3.json()["job_id"])["status"] == "done"


def test_refresh_refused_up_front_and_failed_job_carries_a_problem(tmp_path: Path) -> None:
    from sofabaton import FetchTimeoutError

    client, factory = _rig(tmp_path)
    with client:
        client.post(HUBS, json={"host": HOST})
        proxy = factory.latest(HOST)
        proxy.refuse = True
        r = client.post(f"{HUBS}/{HOST}/snapshot/refresh")
        assert r.status_code == 409 and r.json()["type"] == "hub_busy"
        assert client.get(f"{HUBS}/{HOST}/jobs").json() == []

        proxy.refuse = False
        proxy.fail_with = FetchTimeoutError("no reply")
        r = client.post(f"{HUBS}/{HOST}/snapshot/refresh", json={"device_id": 2})
        assert r.status_code == 202
        failed = _wait_job(client, HOST, r.json()["job_id"])
        assert failed["status"] == "failed"
        assert failed["error"]["type"] == "hub_timeout" and failed["error"]["status"] == 504

        r = client.post(f"{HUBS}/{HOST}/snapshot/refresh", json={"device_id": 1, "activity_id": 101})
        assert r.status_code == 422 and r.json()["type"] == "invalid_request"
