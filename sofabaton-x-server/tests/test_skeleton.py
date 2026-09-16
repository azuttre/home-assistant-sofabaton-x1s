"""S0: settings precedence, the server route, and the OpenAPI shape rules."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sofabaton_server import API_PREFIX, API_VERSION, __version__
from sofabaton_server.app import create_app
from sofabaton_server.cli import build_parser, main, settings_from_args
from sofabaton_server.config import Settings, load_settings
from sofabaton_server.manager import HubManager

from fakes import Factory, no_network_discovery


def _app(settings: Settings):
    manager = HubManager(settings, proxy_factory=Factory())
    return create_app(settings, manager=manager, discovery=no_network_discovery(settings, manager))


# -- settings -------------------------------------------------------------


def test_defaults_are_the_documented_ones(tmp_path: Path) -> None:
    s = load_settings(environ={}, data_dir=tmp_path)
    assert (s.bind, s.port, s.root_path, s.advertise_url) == ("0.0.0.0", 8480, "", None)
    assert s.data_dir == tmp_path and s.trusted_proxies == () and s.initial_hubs == ()


def test_precedence_cli_over_env_over_file(tmp_path: Path) -> None:
    (tmp_path / "server.json").write_text(
        json.dumps({"port": 9000, "bind": "127.0.0.1", "advertise_url": "https://file.example/"}),
        encoding="utf-8",
    )
    env = {"SOFABATON_PORT": "9100", "SOFABATON_HUBS": "192.168.1.5, 192.168.1.6"}
    s = load_settings(cli={"port": 9200}, environ=env, data_dir=tmp_path)
    assert s.port == 9200                                  # cli beats env beats file
    assert s.bind == "127.0.0.1"                           # file layer survives where nothing overrides
    assert s.advertise_url == "https://file.example"       # trailing slash stripped
    assert s.initial_hubs == ("192.168.1.5", "192.168.1.6")


def test_data_dir_from_env_locates_the_settings_file(tmp_path: Path) -> None:
    (tmp_path / "server.json").write_text(json.dumps({"port": 9001}), encoding="utf-8")
    s = load_settings(environ={"SOFABATON_DATA_DIR": str(tmp_path)})
    assert s.port == 9001 and s.data_dir == tmp_path


def test_validation_rejects_bad_values(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        Settings(port=0)
    with pytest.raises(ValueError):
        Settings(tls_cert=Path("a.pem"))                    # key missing
    with pytest.raises(ValueError):
        Settings(advertise_url="sofabaton.home")            # no scheme
    with pytest.raises(ValueError, match="unknown setting"):
        load_settings(cli={"prot": 1}, environ={}, data_dir=tmp_path)
    (tmp_path / "server.json").write_text(json.dumps({"hostname": "x"}), encoding="utf-8")
    with pytest.raises(ValueError, match="unknown setting"):
        load_settings(environ={}, data_dir=tmp_path)


def test_root_path_is_normalised() -> None:
    assert Settings(root_path="sofabaton/").root_path == "/sofabaton"
    assert Settings(root_path="/").root_path == ""


def test_cli_flags_map_onto_settings(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("SOFABATON_PORT", raising=False)
    args = build_parser().parse_args(
        ["--port", "8481", "--data-dir", str(tmp_path), "--hub", "10.0.0.1", "--hub", "10.0.0.2",
         "--trusted-proxy", "10.0.0.9", "--advertise-url", "https://x.example/sb/", "--root-path", "sb"]
    )
    s = settings_from_args(args)
    assert s.port == 8481 and s.initial_hubs == ("10.0.0.1", "10.0.0.2")
    assert s.trusted_proxies == ("10.0.0.9",) and s.advertise_url == "https://x.example/sb"
    assert s.root_path == "/sb"


def test_print_settings_exits_zero_without_serving(tmp_path: Path, capsys) -> None:
    rc = main(["--print-settings", "--data-dir", str(tmp_path), "--port", "8482"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["port"] == 8482 and out["data_dir"] == str(tmp_path)


def test_bad_flag_value_is_a_usage_error(tmp_path: Path, capsys) -> None:
    rc = main(["--print-settings", "--data-dir", str(tmp_path), "--port", "70000"])
    assert rc == 2 and "error:" in capsys.readouterr().err


# -- the one route + OpenAPI shape --------------------------------------------


def test_server_route_reports_identity(tmp_path: Path) -> None:
    app = _app(Settings(data_dir=tmp_path))
    with TestClient(app) as client:
        r = client.get(f"{API_PREFIX}/server")
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "sofabaton-x-server" and body["version"] == __version__
    assert body["api_version"] == API_VERSION and body["api_path"] == API_PREFIX
    assert body["hubs"] == 0 and body["base_url"] is None
    assert isinstance(body["library_version"], str) and body["library_version"]


def test_openapi_has_stable_operation_ids_and_named_components(tmp_path: Path) -> None:
    app = _app(Settings(data_dir=tmp_path))
    with TestClient(app) as client:
        spec = client.get(f"{API_PREFIX}/openapi.json").json()
    op = spec["paths"][f"{API_PREFIX}/server"]["get"]
    assert op["operationId"] == "getServerInfo"
    assert "ServerInfo" in spec["components"]["schemas"]
    # No anonymous inline response schema: the route references the component.
    ref = op["responses"]["200"]["content"]["application/json"]["schema"]
    assert ref == {"$ref": "#/components/schemas/ServerInfo"}
    assert "servers" not in spec                            # nothing advertised: no servers entry


def test_advertise_url_becomes_the_openapi_server_and_root_path_applies(tmp_path: Path) -> None:
    app = _app(Settings(data_dir=tmp_path, advertise_url="https://sofabaton.home.example", root_path="/sb"))
    with TestClient(app) as client:
        spec = client.get(f"{API_PREFIX}/openapi.json").json()
        body = client.get(f"{API_PREFIX}/server").json()
    assert spec["servers"][0]["url"] == "https://sofabaton.home.example"
    assert body["base_url"] == "https://sofabaton.home.example"
