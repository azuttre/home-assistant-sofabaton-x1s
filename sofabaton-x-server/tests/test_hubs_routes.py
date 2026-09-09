"""S1: the /api/v1/hubs routes over an injected manager with fake proxies."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from sofabaton_server import API_PREFIX
from sofabaton_server.app import create_app
from sofabaton_server.config import Settings
from sofabaton_server.manager import HubManager

from fakes import Factory, no_network_discovery

HUBS = f"{API_PREFIX}/hubs"


def _client(tmp_path: Path, factory: Factory) -> TestClient:
    settings = Settings(data_dir=tmp_path)
    manager = HubManager(settings, proxy_factory=factory)
    return TestClient(create_app(settings, manager=manager, discovery=no_network_discovery(settings, manager)))


def test_hub_crud_and_lifecycle_over_http(tmp_path: Path) -> None:
    factory = Factory()
    with _client(tmp_path, factory) as client:
        assert client.get(HUBS).json() == []
        assert client.get(f"{API_PREFIX}/server").json()["hubs"] == 0

        r = client.post(HUBS, json={"host": "192.168.1.50"})
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["hub_id"] == "192.168.1.50" and body["enabled"] is True
        assert body["config"]["host"] == "192.168.1.50" and body["config"]["port"] == 8102
        assert body["status"]["mode"] == "control" and body["status"]["catalog_ready"] is False
        assert factory.latest("192.168.1.50").started
        assert client.get(f"{API_PREFIX}/server").json()["hubs"] == 1

        r = client.post(f"{HUBS}/192.168.1.50/disable")
        assert r.status_code == 200 and r.json()["enabled"] is False and r.json()["status"] is None
        assert factory.latest("192.168.1.50").stops == [True]

        r = client.post(f"{HUBS}/192.168.1.50/enable")
        assert r.status_code == 200 and r.json()["enabled"] is True and r.json()["status"]["mode"] == "control"

        assert client.get(f"{HUBS}/192.168.1.50").status_code == 200
        assert client.delete(f"{HUBS}/192.168.1.50").status_code == 204
        assert client.get(HUBS).json() == []


def test_hub_errors_are_problem_bodies(tmp_path: Path) -> None:
    with _client(tmp_path, Factory()) as client:
        r = client.get(f"{HUBS}/nope")
        assert r.status_code == 404 and r.json()["type"] == "hub_not_found" and r.json()["hub_id"] == "nope"
        assert client.post(f"{HUBS}/nope/enable").status_code == 404
        assert client.delete(f"{HUBS}/nope").status_code == 404

        assert client.post(HUBS, json={"host": "192.168.1.50", "mac": "AA:BB:CC:DD:EE:FF"}).status_code == 201
        r = client.post(HUBS, json={"host": "192.168.1.50"})
        assert r.status_code == 409 and r.json()["type"] == "hub_conflict"
        assert r.json()["hub_id"] == "aabbccddeeff"

        r = client.post(HUBS, json={"host": "192.168.1.5", "is_proxy": True})
        assert r.status_code == 409 and "own proxies" in r.json()["detail"]

        r = client.post(HUBS, json={"host": ""})
        assert r.status_code == 422                       # pydantic: host required
        # Framework validation failures wear the same Problem shape as
        # every other error (review finding: they used to be FastAPI's
        # {"detail": [...]} list, which generated clients cannot decode).
        body = r.json()
        assert body["type"] == "validation_error" and body["status"] == 422
        assert body["title"] == "Invalid request" and "host" in body["detail"]
        assert set(body) == {"type", "title", "status", "detail", "hub_id", "mode"}
        r = client.post(HUBS, json={"host": "10.0.0.1", "port": 70000})
        assert r.status_code == 422 and r.json()["type"] == "invalid_hub_config"


def test_add_disabled_hub_does_not_start_it(tmp_path: Path) -> None:
    factory = Factory()
    with _client(tmp_path, factory) as client:
        r = client.post(HUBS, json={"host": "192.168.1.60", "enabled": False})
        assert r.status_code == 201 and r.json()["enabled"] is False and r.json()["status"] is None
        assert "192.168.1.60" not in factory.built


def test_openapi_lists_hub_operations_with_named_components(tmp_path: Path) -> None:
    with _client(tmp_path, Factory()) as client:
        spec = client.get(f"{API_PREFIX}/openapi.json").json()
    ops = {op["operationId"] for path in spec["paths"].values() for op in path.values()}
    assert {"listHubs", "addHub", "getHub", "removeHub", "enableHub", "disableHub"} <= ops
    schemas = spec["components"]["schemas"]
    assert {"HubView", "HubConfig", "HubStatus", "HubCreate", "Problem"} <= set(schemas)


def test_failed_proxy_start_is_503_and_enable_retries(tmp_path: Path) -> None:
    factory = Factory()
    factory.start_error = OSError(98, "address already in use")
    with _client(tmp_path, factory) as client:
        r = client.post(HUBS, json={"host": "192.168.1.70"})
        assert r.status_code == 503 and r.json()["type"] == "hub_start_failed"
        assert r.json()["hub_id"] == "192.168.1.70" and "address already in use" in r.json()["detail"]
        # The record exists (still enabled, as asked) but no proxy runs:
        # status is absent rather than a phantom "connected".
        view = client.get(f"{HUBS}/192.168.1.70").json()
        assert view["enabled"] is True and view["status"] is None
        assert factory.latest("192.168.1.70").started is False
        assert factory.latest("192.168.1.70").stops == [False]      # cleaned up, no release

        # Same failure on enable, then the port frees up and enable retries.
        r = client.post(f"{HUBS}/192.168.1.70/enable")
        assert r.status_code == 503 and r.json()["type"] == "hub_start_failed"
        factory.start_error = None
        r = client.post(f"{HUBS}/192.168.1.70/enable")
        assert r.status_code == 200 and r.json()["status"]["mode"] == "control"
        assert len(factory.built["192.168.1.70"]) == 3
