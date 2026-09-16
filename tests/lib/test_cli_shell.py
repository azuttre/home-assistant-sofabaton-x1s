"""Coverage for the async CLI shell (lib/cli.py AsyncShell).

Drives command handlers directly against a fake proxy wrapped in the
real AsyncXProxy facade — no stdin loop — to verify the UI wires
commands to facade/engine calls and degrades gracefully in observe mode.
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import sys
import types
from pathlib import Path

LIB_DIR = (
    Path(__file__).resolve().parents[2]
    / "custom_components"
    / "sofabaton_x1s"
    / "lib"
)


def _load_lib() -> types.ModuleType:
    name = "sofabaton_cli_test_pkg"
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
cli = importlib.import_module(f"{_pkg.__name__}.cli")
aio = importlib.import_module(f"{_pkg.__name__}.aio")
protocol_const = importlib.import_module(f"{_pkg.__name__}.protocol_const")


class _Transport:
    is_hub_connected = True
    is_client_connected = False


class _State:
    button_details: dict = {}
    current_activity: int | None = None
    activity_names: dict = {1: "Watch TV"}
    def get_activity_favorite_labels(self, act_lo):
        return []
    def get_activity_name(self, act_id):
        return self.activity_names.get(act_id & 0xFF) if act_id is not None else None
    def entities(self, kind):
        if kind == "activity":
            return {1: {"name": "Watch TV", "active": True}}
        return {5: {"name": "TV", "brand": "Sony"}}


class FakeProxy:
    def __init__(self, *, controllable=True):
        self.transport = _Transport()
        self._controllable = controllable
        self.hub_version = "X1"
        self.state = _State()
        self.sent: list[tuple[int, int]] = []
        self.found = 0
        self.proxy_enabled = True
        self._acts = {1: {"name": "Watch TV", "active": True}}
        self._cmds = {0xC6: "Power"}

    # listeners
    def on_hub_state_change(self, cb): pass
    def on_client_state_change(self, cb): pass
    def on_activity_change(self, cb): pass
    def on_activity_list_update(self, cb): pass
    def on_ota_update(self, cb): pass
    def on_app_activation(self, cb): pass

    def can_issue_commands(self): return self._controllable
    def get_proxy_status(self): return self.proxy_enabled
    def has_banner_identity(self): return True
    def get_banner_info(self):
        return {"model": "X1", "name": "Living Room", "mac": "AABBCCDDEEFF",
                "firmware_version": 7, "production_batch": "20240101"}
    def fetch_banner_info(self, *, force_refresh=True, timeout=2.0):
        return (self.get_banner_info(), True)

    # cached getters (ready=True when controllable; observe mode keeps cache)
    # Catalog getters gate on force_refresh, matching the real engine.
    def get_activities(self, *, force_refresh=True):
        return ({}, False) if force_refresh else (self._acts, True)
    def get_devices(self, *, force_refresh=False):
        return ({}, False) if force_refresh else ({5: {"name": "TV", "brand": "Sony"}}, True)
    def get_commands_for_entity(self, ent, *, fetch_if_missing=True):
        return (self._cmds, self._controllable)
    def get_buttons_for_entity(self, ent, *, fetch_if_missing=True): return ([0x58], self._controllable)
    def get_macros_for_activity(self, act, *, fetch_if_missing=True): return ([], True)
    def ensure_commands_for_activity(self, act, *, fetch_if_missing=True): return ({}, True)

    # actions
    def send_command(self, ent, btn): self.sent.append((ent, btn)); return self._controllable
    def find_remote(self): self.found += 1; return self._controllable
    def enable_proxy(self): self.proxy_enabled = True
    def disable_proxy(self): self.proxy_enabled = False


def _shell(fake):
    proxy = aio.AsyncXProxy.wrap(fake)
    return cli.AsyncShell(proxy)


def test_status_reads_cached_state(capsys):
    async def main():
        shell = _shell(FakeProxy())
        await shell.cmd_status("")
    asyncio.run(main())
    out = capsys.readouterr().out
    assert "hub connected   : True" in out
    assert "controllable    : True" in out
    assert "activities      : 1 cached" in out


def test_activities_lists_via_facade(capsys):
    async def main():
        await _shell(FakeProxy()).cmd_activities("")
    asyncio.run(main())
    out = capsys.readouterr().out
    assert "Watch TV" in out


def test_press_resolves_button_name_and_sends():
    fake = FakeProxy()
    async def main():
        await _shell(fake).cmd_press("101 POWER_ON")
    asyncio.run(main())
    assert fake.sent == [(101, protocol_const.ButtonName.POWER_ON)]


def test_start_and_stop_activity_use_power_codes():
    fake = FakeProxy()
    async def main():
        shell = _shell(fake)
        await shell.cmd_start("5")
        await shell.cmd_stop("5")
    asyncio.run(main())
    assert fake.sent == [
        (5, protocol_const.ButtonName.POWER_ON),
        (5, protocol_const.ButtonName.POWER_OFF),
    ]


def test_find_and_proxy_toggle():
    fake = FakeProxy()
    async def main():
        shell = _shell(fake)
        await shell.cmd_find("")
        await shell.cmd_proxy("off")
        await shell.cmd_proxy("on")
    asyncio.run(main())
    assert fake.found == 1
    assert fake.proxy_enabled is True


def test_press_refused_in_observe_mode(capsys):
    fake = FakeProxy(controllable=False)
    async def main():
        await _shell(fake).cmd_press("101 OK")
    asyncio.run(main())
    out = capsys.readouterr().out
    assert "refused" in out


def test_commands_lists_send_pairs_as_ints(capsys):
    fake = FakeProxy()
    fake._cmds = {12: "Sleep"}
    async def main():
        await _shell(fake).cmd_commands("5")
    asyncio.run(main())
    out = capsys.readouterr().out
    assert "command_id=12" in out and "Sleep" in out
    assert "press 5 <command_id>" in out  # tells the user how to send


def test_commands_reports_error_in_observe_mode(capsys):
    # Not controllable + not ready -> facade raises; shell reports cleanly.
    fake = FakeProxy(controllable=False)
    async def main():
        await _shell(fake).cmd_commands("5")
    asyncio.run(main())
    out = capsys.readouterr().out
    assert "[commands]" in out


def test_unknown_button_reports(capsys):
    async def main():
        await _shell(FakeProxy()).cmd_press("101 NOPENOTABUTTON")
    asyncio.run(main())
    assert "unknown button" in capsys.readouterr().out


def test_run_makes_proxy_discoverable_after_connect(monkeypatch):
    """`run` must make the proxy discoverable once the hub connects.

    The run path should express that intent through the facade gate
    (``wait_until_discoverable``) rather than poking the banner/advertising
    mechanics itself — that's what lets the official app find and attach to
    the proxy.
    """

    calls: list[str] = []

    class _FakeAsyncProxy:
        # The run banner reads status() only; no .sync access is needed.
        def __init__(self, **_kwargs):
            pass

        async def status(self):
            calls.append("status")
            return types.SimpleNamespace(controllable=True, hub_version="X1")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def wait_connected(self, timeout):
            calls.append("wait_connected")
            return True

        async def wait_until_discoverable(self, timeout):
            calls.append("wait_until_discoverable")
            return True

    class _NoopShell:
        def __init__(self, _proxy):
            pass

        async def loop(self):
            return None

    monkeypatch.setattr(cli, "AsyncXProxy", _FakeAsyncProxy)
    monkeypatch.setattr(cli, "AsyncShell", _NoopShell)

    asyncio.run(cli._main_run(["--hub-ip", "1.2.3.4", "--connect-timeout", "0.1"]))

    assert calls == ["wait_connected", "wait_until_discoverable", "status"]


# ---------------------------------------------------------------------------
# snapshot / edit verbs (phase 3, W5)
# ---------------------------------------------------------------------------


class EditFakeProxy(FakeProxy):
    """The minimal snapshot + sync surface the edit verbs drive."""

    _devices_catalog_ready = True
    _activities_catalog_ready = True

    def __init__(self, **kw):
        super().__init__(**kw)
        self.state.generation = 0
        self.state.detail_fetched_at = {"device": {5: "t"}, "activity": {101: "t"}}
        self.synced: list = []
        self.hub_name = "Living Room"

    def bump_cache_generation(self):
        self.state.generation += 1
        return self.state.generation

    def assemble_hub_bundle_from_state(self, *, hub_info=None, include_unfetched=False):
        return {
            "kind": "hub_bundle", "captured_at": "t", "complete": True,
            "payload_profile": "structural", "hub": {"name": self.hub_name},
            "devices": [{"kind": "device_backup", "complete": True, "editable": True,
                         "device": {"device_id": 5, "name": "TV", "brand": "Sony"},
                         "commands": [{"command_id": 1, "name": "Power"}], "button_bindings": [], "macros": []}],
            "activities": [{"kind": "activity_backup", "complete": True, "editable": True,
                            "device": {"device_id": 101, "name": "Watch TV", "entity_type": "activity"},
                            "button_bindings": [], "favorite_slots": [], "macros": []}],
        }

    def sync_activity(self, *, baseline, edited, activity_id, progress_callback=None):
        self.synced.append(("activity", activity_id, edited))
        return {"status": "success", "completed_steps": 1, "total_steps": 1}

    def sync_device(self, *, baseline, edited, device_id, progress_callback=None):
        self.synced.append(("device", device_id, edited))
        return {"status": "success", "completed_steps": 1, "total_steps": 1}

    def set_hub_name(self, name, *, timeout=5.0):
        self.hub_name = name
        return True

    def get_banner_info(self):
        return {**super().get_banner_info(), "name": self.hub_name}


def test_snapshot_verb_lists_entities(capsys):
    async def main():
        shell = _shell(EditFakeProxy())
        await shell.cmd_snapshot("")
    asyncio.run(main())
    out = capsys.readouterr().out
    assert "complete=True" in out and "device      5  TV" in out and "activity  101  Watch TV" in out


def test_rename_and_bind_verbs_edit_through_the_facade(capsys):
    fake = EditFakeProxy()

    async def main():
        shell = _shell(fake)
        await shell.cmd_rename("act 101 Movie night")
        await shell.cmd_bind("101 POWER_ON 5 1")
        await shell.cmd_rename("dev 5 Big TV")
        await shell.cmd_unbind("101 VOL_UP")
        await shell.cmd_bind("101 nope 5 1")
    asyncio.run(main())
    kinds = [(k, i) for k, i, _ in fake.synced]
    assert kinds == [("activity", 101), ("activity", 101), ("device", 5), ("activity", 101)]
    assert fake.synced[0][2]["activities"][0]["device"]["name"] == "Movie night"
    binding = fake.synced[1][2]["activities"][0]["button_bindings"][0]
    assert (binding["device_id"], binding["command_id"]) == (5, 1)
    assert fake.synced[2][2]["devices"][0]["device"]["name"] == "Big TV"
    out = capsys.readouterr().out
    assert out.count("done (1 step(s))") == 4 and "unknown button 'nope'" in out


def test_hubname_verb(capsys):
    fake = EditFakeProxy()

    async def main():
        await _shell(fake).cmd_hubname("Den")
    asyncio.run(main())
    assert fake.hub_name == "Den"
    assert "hub is now named 'Den'" in capsys.readouterr().out


def test_edit_verbs_refuse_in_observe_mode(capsys):
    fake = EditFakeProxy(controllable=False)
    fake.transport.is_client_connected = True

    async def main():
        await _shell(fake).cmd_rename("act 101 Movie")
    asyncio.run(main())
    assert fake.synced == [] and "[sync]" in capsys.readouterr().out



# ---------------------------------------------------------------------------
# phase 4 H4: snapshot out= and apply
# ---------------------------------------------------------------------------

import copy
import json

hub_apply = importlib.import_module(f"{_pkg.__name__}.hub_apply")
hub_sync = importlib.import_module(f"{_pkg.__name__}.hub_sync")
models = importlib.import_module(f"{_pkg.__name__}.models")


def _doc() -> dict:
    return {
        "kind": "hub_bundle", "complete": True, "payload_profile": "structural",
        "hub": {"name": "Den", "version": "X1S"},
        "devices": [{"kind": "device_backup", "complete": True, "editable": True,
                     "device": {"device_id": 5, "name": "TV", "device_class": "ir"},
                     "commands": [{"command_id": 1, "name": "Power"}], "button_bindings": [], "macros": [],
                     "input_record": None, "key_sort": None}],
        "activities": [{"kind": "activity_backup", "complete": True, "editable": True,
                        "device": {"device_id": 101, "name": "Watch", "entity_type": "activity"},
                        "button_bindings": [], "favorite_slots": [], "favorites_order": [], "macros": []}],
    }


def _fake_snapshot(doc: dict):
    bundle = copy.deepcopy(doc)
    return models.HubSnapshot(
        snapshot_id=models.snapshot_content_id(bundle), captured_at="2026-09-12T00:00:00Z", engine_generation=3,
        complete=True, hub=bundle["hub"],
        devices=[models.SnapshotEntity("device", 5, "TV", True, True, None)],
        activities=[models.SnapshotEntity("activity", 101, "Watch", True, True, None)],
        bundle=bundle,
    )


def test_snapshot_out_writes_the_document(tmp_path, capsys):
    out = tmp_path / "D0.json"

    async def fake_snapshot():
        return _fake_snapshot(_doc())

    async def main():
        shell = _shell(FakeProxy())
        shell.p.snapshot = fake_snapshot
        await shell.cmd_snapshot(f"out={out}")

    asyncio.run(main())
    written = json.loads(out.read_text(encoding="utf-8"))
    assert written["snapshot_id"] and written["devices"][0]["device"]["name"] == "TV"
    assert "wrote" in capsys.readouterr().out


def test_apply_plan_previews_without_the_proxy(tmp_path, capsys):
    base = _doc()
    desired = copy.deepcopy(base)
    desired["hub"]["name"] = "Loft"
    desired["devices"].append({"device": {"device_id": -1, "name": "Projector", "device_class": "ir"},
                               "commands": [], "button_bindings": [], "macros": []})
    d0, d1 = tmp_path / "D0.json", tmp_path / "D1.json"
    d0.write_text(json.dumps(base)); d1.write_text(json.dumps(desired))
    calls = []

    async def never(**kw):
        calls.append(kw)

    async def main():
        shell = _shell(FakeProxy())
        shell.p.sync_hub = never
        await shell.cmd_apply(f"{d1} baseline={d0} plan")
        await shell.cmd_apply(f"{d1}")  # no baseline: usage, nothing runs
        bad = copy.deepcopy(desired)
        bad["activities"][0]["button_bindings"].append(
            {"button_id": 1, "device_id": -9, "command_id": 1, "long_press_device_id": None, "long_press_command_id": None})
        (tmp_path / "bad.json").write_text(json.dumps(bad))
        await shell.cmd_apply(f"{tmp_path / 'bad.json'} baseline={d0} plan")

    asyncio.run(main())
    out = capsys.readouterr().out
    assert "3 item(s)" in out and "hub_rename" in out and "add_device" in out and "nothing written" in out
    assert "provisional ids: -1 -> 1" in out
    assert "usage: apply" in out
    assert "refused (dangling_reference)" in out
    assert calls == []


def test_apply_runs_sync_hub_and_writes_the_record_then_resumes(tmp_path, capsys):
    base = _doc()
    desired = copy.deepcopy(base)
    desired["hub"]["name"] = "Loft"
    desired["snapshot_id"] = "abc123"
    d0, d1 = tmp_path / "D0.json", tmp_path / "D1.json"
    d0.write_text(json.dumps(base)); d1.write_text(json.dumps(desired))
    seen = []

    async def fake_sync_hub(**kw):
        seen.append(kw)
        state = kw.get("state")
        if state is None:
            plan = hub_sync.build_hub_sync_plan(kw["baseline"], kw["desired"])
            state = hub_apply.ApplyState.new(kw["baseline"], kw["desired"], plan, hub_version="X1S")
            state.items[0].status = "partial"
            state.items[0].failed_at, state.items[0].message = "hub", "no ack"
            state.status, state.failed_at, state.message = "stopped", "item", "item 0 partial: no ack"
        else:
            state.items[0].status = "done"
            state.status = "success"
            state.remote_sync, state.snapshot_id = "not_needed", "def456"
        kw["on_state"](state)
        return hub_apply.HubSyncResult.from_state(state)

    record = tmp_path / "D1.json.apply.json"

    async def main():
        shell = _shell(FakeProxy())
        shell.p.sync_hub = fake_sync_hub
        await shell.cmd_apply(f"{d1} baseline={d0}")
        assert record.exists()
        await shell.cmd_apply(f"resume={record}")

    asyncio.run(main())
    out = capsys.readouterr().out
    assert seen[0]["baseline"] == base and seen[0]["desired"] == desired and seen[0]["snapshot_id"] == "abc123"
    assert "stopped (item: item 0 partial: no ack)" in out and "resume with: apply resume=<record>" in out
    assert isinstance(seen[1]["state"], hub_apply.ApplyState) and seen[1].get("baseline") is None
    assert "success" in out and "hub_rename" in out
    assert json.loads(record.read_text(encoding="utf-8"))["status"] == "success"
