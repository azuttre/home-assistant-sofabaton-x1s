"""S3: the /api/v1/events WebSocket relay."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from sofabaton import ActivityChanged

from sofabaton_server import API_PREFIX
from sofabaton_server.app import create_app
from sofabaton_server.config import Settings
from sofabaton_server.manager import HubManager

from fakes import Factory, no_network_discovery

HUBS = f"{API_PREFIX}/hubs"
EVENTS = f"{API_PREFIX}/events"


def _rig(tmp_path: Path, **app_kwargs):
    factory = Factory()
    settings = Settings(data_dir=tmp_path)
    manager = HubManager(settings, proxy_factory=factory)
    return TestClient(create_app(settings, manager=manager, discovery=no_network_discovery(settings, manager), **app_kwargs)), factory


def test_hello_then_hub_and_server_events_with_hub_id(tmp_path: Path) -> None:
    client, factory = _rig(tmp_path)
    with client:
        client.post(HUBS, json={"host": "192.168.1.50"})
        proxy = factory.latest("192.168.1.50")
        with client.websocket_connect(EVENTS) as ws:
            hello = ws.receive_json()
            assert hello["type"] == "hello" and hello["api_version"] == "1"
            assert hello["hubs"] == [{"hub_id": "192.168.1.50", "enabled": True}]

            # Emit on the app's loop, the way the engine thread's relay does.
            client.portal.call(proxy.emit, "activity_changed",
                               ActivityChanged(activity_id=101, previous_activity_id=None, name="Watch TV"))
            msg = ws.receive_json()
            assert msg["type"] == "hub_event" and msg["hub_id"] == "192.168.1.50"
            assert msg["event"]["kind"] == "activity_changed" and msg["event"]["seq"] == 1
            assert msg["event"]["payload"] == {"activity_id": 101, "previous_activity_id": None, "name": "Watch TV"}

            # A ready sync re-keys the hub; the event carries the new id.
            client.portal.call(proxy.ready, "E2:6A:44:86:1B:45")
            kinds = [ws.receive_json() for _ in range(2)]
            assert {m["type"] for m in kinds} == {"hub_event", "server_event"}
            assert all(m["hub_id"] == "e26a44861b45" for m in kinds)
            assert any(m.get("kind") == "hub_rekeyed" for m in kinds)

            client.post(f"{HUBS}/e26a44861b45/disable")
            msg = ws.receive_json()
            assert msg == {"type": "server_event", "hub_id": "e26a44861b45", "kind": "hub_disabled"}


def test_hub_id_filter_narrows_the_stream(tmp_path: Path) -> None:
    client, factory = _rig(tmp_path)
    with client:
        client.post(HUBS, json={"host": "10.0.0.1"})
        client.post(HUBS, json={"host": "10.0.0.2"})
        with client.websocket_connect(f"{EVENTS}?hub_id=10.0.0.2") as ws:
            ws.receive_json()                                    # hello
            client.portal.call(factory.latest("10.0.0.1").emit, "ota")
            client.portal.call(factory.latest("10.0.0.2").emit, "ota")
            msg = ws.receive_json()
            assert msg["hub_id"] == "10.0.0.2" and msg["event"]["kind"] == "ota"
            # The filtered-out hub's event never arrives: the next thing on
            # the wire is the server event for hub 2 only.
            client.post(f"{HUBS}/10.0.0.1/disable")
            client.post(f"{HUBS}/10.0.0.2/disable")
            msg = ws.receive_json()
            assert msg == {"type": "server_event", "hub_id": "10.0.0.2", "kind": "hub_disabled"}


def test_slow_client_gets_dropped_notice_and_newest_messages(tmp_path: Path) -> None:
    client, factory = _rig(tmp_path, ws_queue_size=2)
    with client:
        client.post(HUBS, json={"host": "10.0.0.1"})
        proxy = factory.latest("10.0.0.1")
        with client.websocket_connect(EVENTS) as ws:
            ws.receive_json()                                    # hello

            def flood() -> None:
                for _ in range(6):
                    proxy.emit("ota")

            client.portal.call(flood)
            # The relay queue (size 2) overflowed: at least one older message
            # was discarded, a 'dropped' notice precedes the survivors, and the
            # last survivor is the newest event.
            got = [ws.receive_json() for _ in range(3)]
            assert got[0]["type"] == "dropped" and got[0]["count"] >= 1
            assert all(m["type"] == "hub_event" for m in got[1:])
            assert got[-1]["event"]["seq"] == 6


def test_subscription_is_released_on_disconnect(tmp_path: Path) -> None:
    client, _ = _rig(tmp_path)
    with client:
        relay = client.app.state.event_relay
        with client.websocket_connect(EVENTS) as ws:
            ws.receive_json()
            assert relay.subscribers == 1
        # The handler's finally runs when the socket closes.
        for _ in range(20):
            if relay.subscribers == 0:
                break
            client.portal.call(__import__("asyncio").sleep, 0.01)
        assert relay.subscribers == 0


def test_ws_message_types_are_openapi_components(tmp_path: Path) -> None:
    client, _ = _rig(tmp_path)
    with client:
        spec = client.get(f"{API_PREFIX}/openapi.json").json()
    schemas = spec["components"]["schemas"]
    for name in ("WsHello", "WsHubEvent", "WsServerEvent", "WsDropped", "HubEvent",
                 "ActivityChanged", "ConnectionState", "StatusChanged", "CatalogReady"):
        assert name in schemas, name
    assert "/api/v1/events" in spec["info"]["description"]


def test_host_id_filter_follows_the_hub_to_its_mac(tmp_path: Path) -> None:
    # Review finding: a client that subscribed with ?hub_id=<host> right
    # after registering by host went silent once the ready sync re-keyed
    # the hub to its MAC. The filter migrates, so it sees hub_rekeyed and
    # everything after.
    client, factory = _rig(tmp_path)
    with client:
        client.post(HUBS, json={"host": "10.0.0.1"})
        client.post(HUBS, json={"host": "10.0.0.2"})
        proxy = factory.latest("10.0.0.1")
        with client.websocket_connect(f"{EVENTS}?hub_id=10.0.0.1") as ws:
            ws.receive_json()                                    # hello
            client.portal.call(proxy.ready, "E2:6A:44:86:1B:45")
            got = [ws.receive_json() for _ in range(2)]
            assert {m["type"] for m in got} == {"hub_event", "server_event"}
            assert all(m["hub_id"] == "e26a44861b45" for m in got)
            assert any(m.get("kind") == "hub_rekeyed" for m in got)

            client.portal.call(factory.latest("10.0.0.2").emit, "ota")   # still filtered out
            client.portal.call(proxy.emit, "ota")
            msg = ws.receive_json()
            assert msg["hub_id"] == "e26a44861b45" and msg["event"]["kind"] == "ota"
