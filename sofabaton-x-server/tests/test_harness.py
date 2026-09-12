"""The development console at /harness: served by the server, outside the API contract."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from sofabaton_server import API_PREFIX
from sofabaton_server.app import create_app
from sofabaton_server.config import Settings
from sofabaton_server.manager import HubManager

from fakes import Factory, no_network_discovery


def test_harness_is_served_and_absent_from_the_openapi_document(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path)
    manager = HubManager(settings, proxy_factory=Factory())
    app = create_app(settings, manager=manager, discovery=no_network_discovery(settings, manager))
    with TestClient(app) as client:
        r = client.get("/harness")
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
        assert "sofabaton-x-server console" in r.text
        # Relative API base, so the page works under a root path too.
        assert 'new URL("api/v1/", location.href)' in r.text
        spec = client.get(f"{API_PREFIX}/openapi.json").json()
        assert not any(path.endswith("/harness") for path in spec["paths"])
