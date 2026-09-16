"""Exercise the server starter against real routes with a fake hub."""

import importlib.util
import io
import json
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

from sofabaton import ButtonName
from sofabaton_server.app import create_app
from sofabaton_server.config import Settings
from sofabaton_server.manager import HubManager
from fakes import Factory, no_network_discovery

spec = importlib.util.spec_from_file_location("starter_example", Path(__file__).parents[1] / "examples/starter.py")
starter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(starter)

HUB_ID = "e26a44861b45"
HUB = "/hubs/" + HUB_ID


@pytest.fixture
def rig(tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path, callback_port=0)
    factory = Factory()
    manager = HubManager(settings, proxy_factory=factory)
    app = create_app(settings, manager=manager, discovery=no_network_discovery(settings, manager))
    with TestClient(app) as api:
        assert api.post("/api/v1/hubs", json={"host": "127.0.0.1"}).status_code == 201
        proxy = factory.latest("127.0.0.1")
        api.portal.call(proxy.ready, "E2:6A:44:86:1B:45")
        requests = []

        def urlopen(request, timeout):
            requests.append(request)
            response = api.request(request.method, urlsplit(request.full_url).path,
                                   content=request.data, headers=dict(request.headers))
            if response.status_code >= 400:
                raise HTTPError(request.full_url, response.status_code, "error", response.headers,
                                io.BytesIO(response.content))
            return io.BytesIO(response.content)

        monkeypatch.setattr(starter, "urlopen", urlopen)
        yield starter.Client("http://testserver"), proxy, requests


def test_catalog_selection_and_send_use_the_selected_pair(rig, capsys):
    client, proxy, requests = rig
    starter.run(client, SimpleNamespace(action="devices", hub_id=HUB_ID))
    devices = json.loads(capsys.readouterr().out)
    device_id = devices[1]["device_id"]
    starter.run(client, SimpleNamespace(action="commands", hub_id=HUB_ID, device=device_id))
    command_id = json.loads(capsys.readouterr().out)[1]["command_id"]
    starter.run(client, SimpleNamespace(action="send", hub_id=HUB_ID, device=device_id, command=command_id))
    assert proxy.sent == [("send", (device_id, command_id))]
    assert len([r for r in requests if r.method == "POST"]) == 1


def test_setup_follows_jobs_binds_returned_id_and_reuses_device(rig):
    client, proxy, requests = rig
    starter.setup_presses(client, HUB, 101, "PLAY")
    starter.setup_presses(client, HUB, 101, "PLAY")
    assert len(proxy.wifi_deploys) == 1
    device_id = proxy.wifi_deploys[0]["device_id"]
    snapshot = client.request("GET", HUB + "/snapshot")
    activity = next(a for a in snapshot["activities"] if a["device"]["device_id"] == 101)
    row = next(b for b in activity["button_bindings"] if b["button_id"] == int(ButtonName.PLAY))
    assert (row["device_id"], row["command_id"]) == (device_id, 1)
    assert (row["long_press_device_id"], row["long_press_command_id"]) == (device_id, 11)
    binding_requests = [r for r in requests if r.method == "PUT"]
    assert all(r.get_header("If-match", "").startswith('"') for r in binding_requests)


def test_failed_deploy_is_not_retried_or_followed_by_binding(rig):
    client, proxy, requests = rig
    proxy.reject_intents = True
    with pytest.raises(RuntimeError, match="Job did not succeed"):
        starter.setup_presses(client, HUB, 101, "PLAY")
    assert len([r for r in requests if r.method == "POST"]) == 1
    assert not any(r.method == "PUT" for r in requests)


def test_unknown_activity_stops_before_creating_a_callback(rig):
    client, proxy, requests = rig
    with pytest.raises(RuntimeError, match="Unknown activity"):
        starter.setup_presses(client, HUB, 199, "PLAY")
    assert not any(r.method != "GET" for r in requests)


def test_unknown_hub_is_not_mistaken_for_missing_callback(rig):
    client, proxy, requests = rig
    with pytest.raises(starter.ApiError) as error:
        starter.setup_presses(client, "/hubs/unknown", 101, "PLAY")
    assert error.value.problem["type"] == "hub_not_found"
    assert not any(r.method != "GET" for r in requests)


def test_listener_uses_prefix_and_dispatches_only_deployed_presses(monkeypatch, capsys):
    from websockets.sync import client as ws_client

    frames = [
        {"type": "hello", "instance_id": "test-instance"},
        {"type": "hub_event", "event": {"kind": "activity_changed"}},
        {"type": "press", "resolution": "deployed", "label": "Demo", "press_type": "short"},
        {"type": "press", "resolution": "stale", "label": "Old", "press_type": "long"},
        {"type": "dropped", "count": 1},
    ]

    class Connection:
        def __enter__(self): return iter(json.dumps(frame) for frame in frames)
        def __exit__(self, *args): pass

    def connect(url, **kwargs):
        assert url == "wss://server.example/prefix/api/v1/events?hub_id=hub+id"
        return Connection()

    monkeypatch.setattr(ws_client, "connect", connect)
    starter.listen(starter.Client("https://server.example/prefix/"), "hub id")
    output = capsys.readouterr().out
    assert "Connected to server instance test-instance" in output
    assert '"kind": "activity_changed"' in output
    assert "PRESS: Demo (short)" in output and "PRESS: Old" not in output
    assert "Events were lost" in output
