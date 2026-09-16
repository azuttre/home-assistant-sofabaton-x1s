"""``/harness`` was the development console; it redirects to the control panel."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from sofabaton_server import API_PREFIX
from sofabaton_server.app import create_app
from sofabaton_server.config import Settings
from sofabaton_server.manager import HubManager

from fakes import Factory, no_network_discovery


def test_harness_redirects_to_the_panel_and_stays_out_of_the_contract(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path)
    manager = HubManager(settings, proxy_factory=Factory())
    app = create_app(settings, manager=manager, discovery=no_network_discovery(settings, manager))
    with TestClient(app) as client:
        r = client.get("/harness", follow_redirects=False)
        assert r.status_code == 307 and r.headers["location"] == "/ui/"
        spec = client.get(f"{API_PREFIX}/openapi.json").json()
        assert not any(path.endswith("/harness") for path in spec["paths"])
