"""The pure edit helpers (lib/edits.py) produce exactly the plan steps the
sync planners emit for the intended change, and nothing else."""

from __future__ import annotations

import copy
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
    name = "sofabaton_edits_test_pkg"
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
edits = importlib.import_module(f"{_pkg.__name__}.edits")
activity_sync = importlib.import_module(f"{_pkg.__name__}.activity_sync")

POWER_ON = int(_pkg.ButtonName.POWER_ON)
VOL_UP = int(_pkg.ButtonName.VOL_UP)


def _device(dev_id: int, name: str, commands: dict[int, str]) -> dict:
    return {
        "kind": "device_backup",
        "complete": True,
        "payload_profile": "structural",
        "device": {"device_id": dev_id, "name": name, "brand": "Acme",
                   "device_class": "tv", "idle_behavior": 0},
        "commands": [{"command_id": cid, "name": label} for cid, label in commands.items()],
        "key_sort": None,
        "input_record": None,
        "button_bindings": [],
        "macros": [],
    }


def _bundle() -> dict:
    return {
        "kind": "hub_bundle",
        "complete": True,
        "payload_profile": "structural",
        "hub": {"name": "Den"},
        "devices": [_device(5, "TV", {1: "Power", 2: "Mute"}), _device(7, "Amp", {3: "Vol+"})],
        "activities": [
            {
                "kind": "activity_backup",
                "complete": True,
                "device": {"device_id": 101, "name": "Watch TV", "entity_type": "activity"},
                "button_bindings": [
                    {"button_id": POWER_ON, "button_name": "POWER_ON", "device_id": 5, "command_id": 1,
                     "long_press_device_id": None, "long_press_command_id": None},
                ],
                "favorite_slots": [
                    {"button_id": 1, "device_id": 5, "command_id": 2},
                    {"button_id": 2, "device_id": 7, "command_id": 3},
                ],
                "favorites_order": [1, 2],
                "macros": [],
                "referenced_source_device_ids": [5, 7],
            }
        ],
    }


def _activity_plan(baseline, edited):
    return [(s.kind, s.payload) for s in activity_sync.build_activity_sync_plan(baseline, edited, 101)]


def _device_plan(baseline, edited, dev):
    return [(s.kind, s.payload) for s in activity_sync.build_device_sync_plan(baseline, edited, dev)]


def test_helpers_never_mutate_the_input() -> None:
    base = _bundle()
    before = copy.deepcopy(base)
    edits.rename_activity(base, 101, "Movie")
    edits.bind_button(base, 101, VOL_UP, 7, 3)
    edits.add_favorite(base, 101, 5, 1)
    edits.rename_command(base, 5, 1, "Standby")
    assert base == before


def test_rename_activity_plans_one_rename() -> None:
    base = _bundle()
    edited = edits.rename_activity(base, 101, " Movie ")
    assert _activity_plan(base, edited) == [
        ("activity_rename", {"activity_id": 101, "name": "Movie"}),
        ("remote_sync", {"activity_id": 101}),
    ]
    with pytest.raises(ValueError):
        edits.rename_activity(base, 101, "  ")
    with pytest.raises(KeyError):
        edits.rename_activity(base, 102, "x")


def test_bind_and_clear_button_plan_binding_steps() -> None:
    base = _bundle()
    edited = edits.bind_button(base, 101, VOL_UP, 7, 3, long_press=(5, 2))
    plan = _activity_plan(base, edited)
    assert plan[0] == ("binding_write", {
        "activity_id": 101, "button_id": VOL_UP, "device_id": 7, "command_id": 3,
        "long_press_device_id": 5, "long_press_command_id": 2,
    })
    assert [k for k, _ in plan] == ["binding_write", "remote_sync"]
    # Rebinding an existing button replaces it, clearing removes it.
    rebound = edits.bind_button(base, 101, POWER_ON, 7, 3)
    assert _activity_plan(base, rebound)[0][1]["device_id"] == 7
    cleared = edits.clear_button(base, 101, POWER_ON)
    assert _activity_plan(base, cleared)[0] == ("binding_delete", {"activity_id": 101, "button_id": POWER_ON})
    assert edits.clear_button(base, 101, VOL_UP) == base  # nothing bound: no change


def test_favorites_add_remove_reorder() -> None:
    base = _bundle()
    added = edits.add_favorite(base, 101, 5, 1, name="Power")
    plan = _activity_plan(base, added)
    assert plan[0] == ("favorite_add", {"activity_id": 101, "device_id": 5, "command_id": 1, "name": "Power"})
    assert [k for k, _ in plan] == ["favorite_add", "remote_sync"]
    with pytest.raises(ValueError):
        edits.add_favorite(base, 101, 5, 2)  # already there

    removed = edits.remove_favorite(base, 101, 5, 2)
    plan = _activity_plan(base, removed)
    assert plan[0] == ("favorite_delete", {"activity_id": 101, "button_id": 1, "device_id": 5, "command_id": 2})
    assert [k for k, _ in plan] == ["favorite_delete", "remote_sync"]
    with pytest.raises(ValueError):
        edits.remove_favorite(base, 101, 5, 1)

    reordered = edits.reorder_favorites(base, 101, [(7, 3), (5, 2)])
    plan = _activity_plan(base, reordered)
    assert plan[0] == ("favorite_order", {"activity_id": 101, "order": [
        {"kind": "favorite", "device_id": 7, "command_id": 3},
        {"kind": "favorite", "device_id": 5, "command_id": 2},
    ]})
    assert edits.reorder_favorites(base, 101, [(5, 2), (7, 3)]) == base
    with pytest.raises(ValueError):
        edits.reorder_favorites(base, 101, [(5, 2)])


def test_device_rename_command_rename_and_idle() -> None:
    base = _bundle()
    renamed = edits.rename_device(base, 5, "Big TV", brand="Sony")
    assert _device_plan(base, renamed, 5) == [
        ("device_rename", {"device_id": 5, "name": "Big TV", "brand": "Sony"}),
    ]
    command = edits.rename_command(base, 5, 1, "Standby")
    assert _device_plan(base, command, 5) == [
        ("command_rename", {"device_id": 5, "command_id": 1, "name": "Standby"}),
    ]
    with pytest.raises(KeyError):
        edits.rename_command(base, 5, 9, "x")
    idle = edits.set_idle_behavior(base, 5, 2)
    assert _device_plan(base, idle, 5) == [("idle_behavior", {"device_id": 5, "mode": 2})]
    with pytest.raises(ValueError):
        edits.set_idle_behavior(base, 5, 300)


def test_edits_stay_in_scope_for_the_planner() -> None:
    # An activity edit leaves the devices byte-identical, and vice versa,
    # so the planners' scope guards accept every helper's output.
    base = _bundle()
    edited = edits.bind_button(edits.add_favorite(base, 101, 5, 1), 101, VOL_UP, 7, 3)
    activity_sync.build_activity_sync_plan(base, edited, 101)
    edited = edits.set_idle_behavior(edits.rename_device(base, 5, "New"), 5, 1)
    activity_sync.build_device_sync_plan(base, edited, 5)


def test_payload_overwrite_and_add_command_plan_the_record_writes() -> None:
    base = _bundle()
    raw = _pkg.IrPayload.from_raw_timings([9000, 4500, 560, 560], 38000)
    edited = edits.set_command_payload(base, 5, 2, raw)
    plan = _device_plan(base, edited, 5)
    assert [k for k, _ in plan] == ["command_payload"]
    assert plan[0][1]["command_id"] == 2 and plan[0][1]["restore_data"]["data_hex"] == raw.blob.hex()
    with pytest.raises(KeyError):
        edits.set_command_payload(base, 5, 9, raw)

    desc = _pkg.IrPayload.from_descriptor("P:NEC1 D:4 S:5 F:21")
    added, slot = edits.add_command(base, 5, desc, "Input")
    assert slot == 3
    plan = _device_plan(base, added, 5)
    assert [k for k, _ in plan] == ["command_add"]
    assert plan[0][1]["command_id"] == 3 and plan[0][1]["command_name"] == "Input"
    assert plan[0][1]["restore_data"]["decoded"]["fields"]["descriptor"] == "P:NEC1 D:4 S:5 F:21"
    _, explicit = edits.add_command(base, 5, desc, "Input", command_id=40)
    assert explicit == 40
    with pytest.raises(ValueError):
        edits.add_command(base, 5, desc, "Input", command_id=1)
