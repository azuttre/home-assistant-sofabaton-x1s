"""Tests for the hub configuration record (lib/config.py, phase 1 F1).

One record shape for every intake path: library discovery, a foreign
mDNS stack's advertisement, manual entry, and a dict from a REST body or
config file. The record must round-trip losslessly and turn into the
same facade keyword arguments the keyword constructor takes.
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import sys
import types
from pathlib import Path

import pytest

LIB_DIR = (
    Path(__file__).resolve().parents[2]
    / "custom_components"
    / "sofabaton_x1s"
    / "lib"
)


def _load_lib() -> types.ModuleType:
    name = "sofabaton_config_test_pkg"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(
        name, LIB_DIR / "__init__.py", submodule_search_locations=[str(LIB_DIR)]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_pkg = _load_lib()
config = importlib.import_module(f"{_pkg.__name__}.config")
discovery = importlib.import_module(f"{_pkg.__name__}.discovery")
hub_versions = importlib.import_module(f"{_pkg.__name__}.hub_versions")
aio = importlib.import_module(f"{_pkg.__name__}.aio")

HubConfig = config.HubConfig
X1HUB_TYPE = hub_versions.MDNS_SERVICE_TYPE_X1
X2HUB_TYPE = hub_versions.MDNS_SERVICE_TYPE_X2


def test_host_only_record_is_complete_and_maps_to_defaults() -> None:
    cfg = HubConfig(host=" 192.168.1.50 ")
    assert cfg.host == "192.168.1.50"
    assert cfg.port == 8102 and cfg.hub_listen_port == 8200 and cfg.app_discovery_port == 8102
    assert cfg.proxy_kwargs() == {
        "hub_ip": "192.168.1.50",
        "hub_port": 8102,
        "hub_listen_port": 8200,
        "app_discovery_port": 8102,
        "proxy_enabled": True,
    }


def test_record_validates_host_and_ports() -> None:
    with pytest.raises(ValueError):
        HubConfig(host="")
    with pytest.raises(ValueError):
        HubConfig(host="1.2.3.4", port=0)
    with pytest.raises(ValueError):
        HubConfig(host="1.2.3.4", hub_listen_port=70000)
    with pytest.raises(ValueError):
        HubConfig(host="1.2.3.4", app_discovery_port=True)  # bool is not a port


def test_dict_round_trip_is_lossless() -> None:
    cfg = HubConfig(
        host="10.0.0.9",
        name="Den",
        mac="AA:BB",
        txt={"HVER": "2", "NAME": "Den"},
        hub_version="X1S",
        hub_listen_port=8201,
        proxy_enabled=False,
        source="manual",
    )
    data = cfg.to_dict()
    assert data["txt"] == {"HVER": "2", "NAME": "Den"} and data["source"] == "manual"
    assert HubConfig.from_dict(data) == cfg
    # Coerces JSON-ish input: ports as strings, optional keys missing/None.
    assert HubConfig.from_dict({"host": "10.0.0.9", "port": "8102", "txt": None}) == HubConfig(host="10.0.0.9")


def test_from_dict_rejects_unknown_keys_and_bad_ports() -> None:
    with pytest.raises(ValueError, match="unknown field"):
        HubConfig.from_dict({"host": "1.2.3.4", "hostname": "x"})
    with pytest.raises(ValueError):
        HubConfig.from_dict({"host": "1.2.3.4", "port": "eight"})
    with pytest.raises(ValueError):
        HubConfig.from_dict("1.2.3.4")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "service_type,hver,expected_version",
    [
        (X1HUB_TYPE, hub_versions.HVER_X1, hub_versions.HUB_VERSION_X1),
        (X1HUB_TYPE, hub_versions.HVER_X1S, hub_versions.HUB_VERSION_X1S),
        (X2HUB_TYPE, hub_versions.HVER_X2, hub_versions.HUB_VERSION_X2),
    ],
)
def test_from_advertisement_for_each_hub_line(service_type, hver, expected_version) -> None:
    cfg = HubConfig.from_advertisement(
        service_type,
        f"SOFABATON.{service_type}",
        host="192.168.1.77",
        port=8102,
        properties={b"HVER": str(hver).encode(), b"MAC": b"AA:BB:CC", b"NAME": b"Den"},
    )
    assert cfg.host == "192.168.1.77" and cfg.port == 8102
    assert cfg.name == "Den" and cfg.mac == "AA:BB:CC"
    assert cfg.hub_version == expected_version
    assert cfg.txt["HVER"] == str(hver)
    assert cfg.is_proxy is False and cfg.source == "client"
    kwargs = cfg.proxy_kwargs()
    assert kwargs["mdns_instance"] == "Den" and kwargs["hub_version"] == expected_version
    assert kwargs["mdns_txt"]["HVER"] == str(hver)


def test_from_advertisement_marks_our_own_proxy_and_keeps_unknown_hver() -> None:
    proxy_cfg = HubConfig.from_advertisement(
        X1HUB_TYPE,
        f"PROXY.{X1HUB_TYPE}",
        host="192.168.1.5",
        port=8102,
        properties={b"HVER": b"1", hub_versions.PROXY_TXT_KEY.encode(): hub_versions.PROXY_TXT_VALUE.encode()},
    )
    assert proxy_cfg.is_proxy is True

    unknown = HubConfig.from_advertisement(
        X1HUB_TYPE, f"MYSTERY.{X1HUB_TYPE}", host="192.168.1.6", port=8102, properties={b"HVER": b"99"}
    )
    assert unknown.hub_version is None and unknown.txt == {"HVER": "99"}
    assert "hub_version" not in unknown.proxy_kwargs()


def test_from_advertisement_rejects_unusable_records() -> None:
    with pytest.raises(ValueError):
        HubConfig.from_advertisement("_printer._tcp.local.", "X._printer._tcp.local.", host="1.2.3.4", port=9100, properties={})
    with pytest.raises(ValueError):
        HubConfig.from_advertisement(X1HUB_TYPE, f"X.{X1HUB_TYPE}", host=None, port=8102, properties={})


def test_from_discovered_mirrors_discovery_result() -> None:
    hub = discovery.normalize_advertisement(
        X1HUB_TYPE, f"DEN.{X1HUB_TYPE}", host="192.168.1.8", port=8102,
        properties={b"HVER": b"2", b"NAME": b"Den", b"MAC": b"AA:BB"},
    )
    cfg = HubConfig.from_discovered(hub)
    assert cfg.source == "server"
    assert (cfg.host, cfg.name, cfg.mac, cfg.hub_version) == ("192.168.1.8", "Den", "AA:BB", hub.hub_version)
    # A discovery result without a port falls back to the protocol default.
    hub_noport = discovery.normalize_advertisement(
        X1HUB_TYPE, f"DEN.{X1HUB_TYPE}", host="192.168.1.8", port=None, properties={b"HVER": b"2"}
    )
    assert HubConfig.from_discovered(hub_noport).port == 8102


def test_from_config_constructs_engine_with_record_kwargs(monkeypatch) -> None:
    captured: dict = {}

    class _Recorder:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        # The facade arms its connect-time initial sync on construction.
        def on_hub_state_change(self, cb): ...
        def on_client_state_change(self, cb): ...

    monkeypatch.setattr(aio, "X1Proxy", _Recorder)

    async def main():
        cfg = HubConfig(host="192.168.1.50", name="Den", txt={"HVER": "2"}, hub_version="X1S", hub_listen_port=8201)
        proxy = aio.AsyncXProxy.from_config(cfg, diag_dump=False)
        assert isinstance(proxy, aio.AsyncXProxy)

    asyncio.run(main())
    assert captured == {
        "real_hub_ip": "192.168.1.50",
        "real_hub_udp_port": 8102,
        "hub_listen_base": 8201,
        "proxy_udp_port": 8102,
        "proxy_enabled": True,
        "mdns_instance": "Den",
        "mdns_txt": {"HVER": "2"},
        "hub_version": "X1S",
        "diag_dump": False,
    }
