"""Regression coverage for durable device-restore finalization.

The family-0x07 create acknowledgement allocates an id but does not prove that
the device row is durable. The official create flow commits that row with a
family-0x08 device update after replaying all device-owned records. These tests
model that persistence boundary explicitly so a restore cannot report success
from its local cache while a reconnect still sees an empty/reused catalog row.
"""

from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
from typing import Any

import pytest

from custom_components.sofabaton_x1s.const import (
    HUB_VERSION_X1,
    HUB_VERSION_X1S,
    HUB_VERSION_X2,
)
from custom_components.sofabaton_x1s.lib.devices import parse_device_record
from custom_components.sofabaton_x1s.lib.x1_proxy import X1Proxy
import custom_components.sofabaton_x1s.lib.x1_proxy as x1_proxy_module


def _device_backup(
    *,
    source_device_id: int,
    name: str,
    command_name: str | None = None,
    with_replay_records: bool = False,
) -> dict[str, Any]:
    commands: list[dict[str, Any]] = []
    if command_name is not None:
        commands.append(
            {
                "command_id": 1,
                "name": command_name,
                "restore_data": {
                    "transport": "hub_code_record",
                    "library_type": 0x0D,
                    "button_code": 0x4E21,
                    "data_hex": "00 01 02 03 04 05 06 07 08 09",
                },
            }
        )

    input_record = None
    button_bindings: list[dict[str, Any]] = []
    macros: list[dict[str, Any]] = []
    if with_replay_records:
        input_record = {
            "device_id": source_device_id,
            "source_id_byte": 1,
            "flag_a": 0,
            "flag_b": 0,
            "state_byte": 0,
            "entries": [
                {
                    "command_id": 1,
                    "input_index": 1,
                    "fid": 0x4E21,
                    "name": command_name or "Command",
                }
            ],
            "control_keys": {},
            "favorites": [],
        }
        button_bindings = [
            {
                "button_id": 0x58,
                "device_id": source_device_id,
                "command_id": 1,
                "long_press_device_id": None,
                "long_press_command_id": None,
            }
        ]
        macros = [
            {
                "button_id": 0xC6,
                "name": "POWER_ON",
                "steps": [
                    {
                        "device_id": source_device_id,
                        "command_id": 1,
                        "fid": 0x4E21,
                        "duration": 0,
                        "delay": 0xFF,
                    }
                ],
            }
        ]

    return {
        "kind": "device_backup",
        "schema_version": 4,
        "device": {
            "device_id": source_device_id,
            "name": name,
            "brand": "Fixture",
            "device_class": "ir",
            "device_class_code": 0x10,
            "icon": 1,
            "sort": 0,
            "code_type": 0x10,
            "device_type": 0x10,
            "code_id_hex": "00 " * 15 + "00",
            "hide": 0,
            "input_flag": 1 if with_replay_records else 0,
            "channel": 0,
            "power_state": 0,
            "ip_address": None,
            "poll_time": -1,
            "input_mode": 1 if with_replay_records else 0,
            "inputs_configured": with_replay_records,
            "power_mode": 1 if with_replay_records else 0,
            "power_style": 3 if with_replay_records else 2,
            "share_mode": 0,
            "tail_marker": 1,
            "extras": None,
        },
        "commands": commands,
        "key_sort": None,
        "input_record": input_record,
        "button_bindings": button_bindings,
        "macros": macros,
        "favorite_slots": [],
    }


def _proxy(monkeypatch: pytest.MonkeyPatch, hub_version: str) -> X1Proxy:
    proxy = X1Proxy(
        "127.0.0.1",
        proxy_enabled=False,
        diag_dump=False,
        diag_parse=False,
        hub_version=hub_version,
    )
    monkeypatch.setattr(proxy, "can_issue_commands", lambda: True)
    monkeypatch.setattr(proxy, "reset_ack_queues", lambda: None)
    if hub_version != HUB_VERSION_X1:
        monkeypatch.setattr(
            proxy,
            "_refresh_destination_catalog",
            lambda timeout=5.0: None,
        )
    return proxy


@pytest.mark.parametrize(
    ("hub_version", "expected_post_families"),
    [
        (HUB_VERSION_X1, [0x41, 0x46, 0x08]),
        (HUB_VERSION_X1S, [0x41, 0x08]),
        (HUB_VERSION_X2, [0x41, 0x08]),
    ],
)
def test_empty_restore_keeps_profile_order_and_finishes_with_device_update(
    monkeypatch: pytest.MonkeyPatch,
    hub_version: str,
    expected_post_families: list[int],
) -> None:
    """Empty devices still cross the durable boundary on every profile."""

    proxy = _proxy(monkeypatch, hub_version)
    sequence_calls: list[list[Any]] = []

    def _run_create_sequence(_proxy: X1Proxy, steps: Any) -> SimpleNamespace:
        step_list = list(steps)
        sequence_calls.append(step_list)
        return SimpleNamespace(
            success=True,
            assigned_device_id=0x2A,
            failed_step=None,
            failed_index=None,
        )

    monkeypatch.setattr(x1_proxy_module, "run_create_sequence", _run_create_sequence)

    result = proxy.restore_device(
        _device_backup(source_device_id=0x0B, name="Empty")
    )

    assert result is not None and result["device_id"] == 0x2A
    assert [step.family for step in sequence_calls[1]] == expected_post_families
    finalize = sequence_calls[1][-1]
    assert finalize.label == "device-update"
    assert parse_device_record(
        finalize.payload[3:], hub_version=hub_version
    ).device_id == 0x2A


@pytest.mark.parametrize("hub_version", [HUB_VERSION_X1S, HUB_VERSION_X2])
def test_x1s_x2_finalize_after_all_replay_records_with_hub_assigned_id(
    monkeypatch: pytest.MonkeyPatch,
    hub_version: str,
) -> None:
    proxy = _proxy(monkeypatch, hub_version)
    sequence_calls: list[list[Any]] = []

    def _run_create_sequence(_proxy: X1Proxy, steps: Any) -> SimpleNamespace:
        step_list = list(steps)
        sequence_calls.append(step_list)
        return SimpleNamespace(
            success=True,
            assigned_device_id=0x23,
            failed_step=None,
            failed_index=None,
        )

    monkeypatch.setattr(x1_proxy_module, "run_create_sequence", _run_create_sequence)

    result = proxy.restore_device(
        _device_backup(
            source_device_id=0x0B,
            name="Full",
            command_name="Power",
            with_replay_records=True,
        )
    )

    assert result is not None and result["device_id"] == 0x23
    post_steps = sequence_calls[1]
    assert post_steps[-1].family == 0x08
    assert parse_device_record(
        post_steps[-1].payload[3:], hub_version=hub_version
    ).device_id == 0x23
    replay_families = {0x0E, 0x12, 0x3E, 0x46}
    assert replay_families.issubset({step.family for step in post_steps[:-1]})


@pytest.mark.parametrize("hub_version", [HUB_VERSION_X1S, HUB_VERSION_X2])
def test_failed_final_update_does_not_report_success_or_finalize_local_cache(
    monkeypatch: pytest.MonkeyPatch,
    hub_version: str,
) -> None:
    proxy = _proxy(monkeypatch, hub_version)
    deleted: list[int] = []
    call_count = 0

    def _run_create_sequence(_proxy: X1Proxy, steps: Any) -> SimpleNamespace:
        nonlocal call_count
        call_count += 1
        step_list = list(steps)
        if call_count == 1:
            return SimpleNamespace(
                success=True,
                assigned_device_id=0x31,
                failed_step=None,
                failed_index=None,
            )
        failed_step = step_list[-1]
        assert failed_step.label == "device-update"
        return SimpleNamespace(
            success=False,
            assigned_device_id=0x31,
            failed_step=failed_step,
            failed_index=len(step_list) - 1,
        )

    def _delete_device(device_id: int) -> dict[str, Any]:
        deleted.append(device_id)
        return {
            "device_id": device_id,
            "confirmed_activities": [],
            "status": "success",
        }

    monkeypatch.setattr(x1_proxy_module, "run_create_sequence", _run_create_sequence)
    monkeypatch.setattr(proxy, "delete_device", _delete_device)

    result = proxy.restore_device(
        _device_backup(
            source_device_id=0x0B,
            name="Rejected",
            command_name="Power",
        )
    )

    assert result is None
    assert deleted == [0x31]
    assert 0x31 not in proxy.state.devices
    assert 0x31 not in proxy.state.commands
    assert 0x31 not in proxy._commands_complete


class _DurableCatalogHub:
    """In-memory hub whose family-0x07 allocation is provisional.

    A create ACK selects the lowest id absent from the durable catalog. Command
    rows attach to that provisional record, but only family 0x08 promotes it.
    A catalog refresh/reconnect exposes durable records only, matching the
    failure mode from the Office incident.
    """

    def __init__(self, *, hub_version: str) -> None:
        self.hub_version = hub_version
        self.durable: dict[int, dict[str, Any]] = {}
        self.provisional: dict[int, dict[str, Any]] = {}
        self.current_assigned_id: int | None = None
        self.finalized_ids: list[int] = []

    def refresh_proxy_catalog(self, proxy: X1Proxy, timeout: float = 5.0) -> None:
        del timeout
        proxy.state.devices = {
            device_id: {
                "name": row["name"],
                "brand": row["brand"],
                "device_class": "ir",
                "device_class_code": 0x10,
            }
            for device_id, row in self.durable.items()
        }
        proxy.state.activities = {}

    def run_create_sequence(
        self,
        _proxy: X1Proxy,
        steps: Any,
    ) -> SimpleNamespace:
        step_list = list(steps)
        if len(step_list) == 1 and step_list[0].family == 0x07:
            assigned_id = next(
                candidate
                for candidate in range(1, 0x65)
                if candidate not in self.durable
            )
            config = parse_device_record(
                step_list[0].payload[3:],
                hub_version=self.hub_version,
            )
            self.provisional[assigned_id] = {
                "name": config.name,
                "brand": config.brand,
                "commands": {},
            }
            self.current_assigned_id = assigned_id
            return SimpleNamespace(
                success=True,
                assigned_device_id=assigned_id,
                failed_step=None,
                failed_index=None,
            )

        assert self.current_assigned_id is not None
        for step in step_list:
            if step.family == 0x0E:
                device_id = step.payload[6]
                command_id = step.payload[7]
                label = step.payload[15:75].decode(
                    "utf-16-be", errors="ignore"
                ).rstrip("\x00")
                self.provisional[device_id]["commands"][command_id] = label
            elif step.family == 0x08:
                config = parse_device_record(
                    step.payload[3:],
                    hub_version=self.hub_version,
                )
                assert config.device_id == self.current_assigned_id
                self.durable[config.device_id] = deepcopy(
                    self.provisional[config.device_id]
                )
                self.finalized_ids.append(config.device_id)

        return SimpleNamespace(
            success=True,
            assigned_device_id=self.current_assigned_id,
            failed_step=None,
            failed_index=None,
        )

    def read_complete_catalog(self) -> dict[int, dict[str, Any]]:
        return deepcopy(self.durable)


@pytest.fixture
def durable_catalog_hub() -> _DurableCatalogHub:
    return _DurableCatalogHub(hub_version=HUB_VERSION_X1S)


def test_two_device_restore_survives_cache_discard_and_complete_catalog_read(
    monkeypatch: pytest.MonkeyPatch,
    durable_catalog_hub: _DurableCatalogHub,
) -> None:
    """Two provisional allocations become distinct durable catalog rows."""

    proxy = _proxy(monkeypatch, HUB_VERSION_X1S)
    monkeypatch.setattr(
        proxy,
        "_refresh_destination_catalog",
        lambda timeout=5.0: durable_catalog_hub.refresh_proxy_catalog(
            proxy, timeout
        ),
    )
    monkeypatch.setattr(
        x1_proxy_module,
        "run_create_sequence",
        durable_catalog_hub.run_create_sequence,
    )

    bundle = {
        "kind": "hub_bundle",
        "schema_version": 5,
        "devices": [
            _device_backup(
                source_device_id=7,
                name="Television",
                command_name="Power",
            ),
            _device_backup(
                source_device_id=8,
                name="Receiver",
                command_name="Volume Up",
            ),
        ],
        "activities": [],
    }

    result = proxy.restore_hub_bundle(bundle)

    assert result["status"] == "success"
    assert result["device_id_map"] == {"7": 1, "8": 2}
    assert durable_catalog_hub.finalized_ids == [1, 2]

    # Discard every local optimistic cache entry. A fresh connection's full
    # catalog read must recover both finalized rows and their command tables.
    reconnected = X1Proxy(
        "127.0.0.1",
        proxy_enabled=False,
        diag_dump=False,
        diag_parse=False,
        hub_version=HUB_VERSION_X1S,
    )
    complete_catalog = durable_catalog_hub.read_complete_catalog()
    reconnected.state.devices = {
        device_id: {"name": row["name"], "brand": row["brand"]}
        for device_id, row in complete_catalog.items()
    }
    reconnected.state.commands = {
        device_id: dict(row["commands"])
        for device_id, row in complete_catalog.items()
    }

    assert reconnected.state.devices == {
        1: {"name": "Television", "brand": "Fixture"},
        2: {"name": "Receiver", "brand": "Fixture"},
    }
    assert reconnected.state.commands == {
        1: {1: "Power"},
        2: {1: "Volume Up"},
    }
