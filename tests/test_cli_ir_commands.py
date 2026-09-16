"""Tests for the CLI's IR-payload command (``testir``).

``addir`` was retired in sofabaton-x 0.2.0 (persist_ir_blob left the facade);
it returns with the phase 3 payload path.
"""

from importlib import import_module
from pathlib import Path
import asyncio
import sys

import pytest

from tests._stub_packages import ensure_stub_package

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _cli():
    ensure_stub_package("custom_components", ROOT / "custom_components")
    ensure_stub_package(
        "custom_components.sofabaton_x1s",
        ROOT / "custom_components" / "sofabaton_x1s",
    )
    ensure_stub_package(
        "custom_components.sofabaton_x1s.lib",
        ROOT / "custom_components" / "sofabaton_x1s" / "lib",
    )
    return import_module("custom_components.sofabaton_x1s.lib.cli")


# 12 bytes: enough to clear the shell's 10-byte minimum.
PAYLOAD_HEX = "01 20 00 10 01 00 94 ac 00 00 23 0a"
PAYLOAD = bytes.fromhex(PAYLOAD_HEX)


class _StubProxy:
    """Bare-minimum AsyncXProxy stand-in for driving AsyncShell commands."""

    def __init__(self, *, devices=None, persist_result=None, play_ok=True):
        self.calls: list[tuple[str, dict]] = []
        self._devices = devices if devices is not None else {}
        self._persist_result = persist_result
        self._play_ok = play_ok

    # listener registrations done by AsyncShell.__init__
    def on_hub_state_change(self, cb) -> None: ...
    def on_client_state_change(self, cb) -> None: ...
    def on_activity_change(self, cb) -> None: ...

    async def play(self, payload):
        # The facade's play(): raises HubRejectedError when the hub refuses.
        self.calls.append(("play", {"blob": bytes(payload)}))
        if not self._play_ok:
            raise RuntimeError("the hub did not accept play")

    async def devices(self):
        self.calls.append(("devices", {}))
        # The facade returns typed Device rows (list, sorted by id); the
        # stub keeps its dict fixture and projects it the same way.
        models = import_module("custom_components.sofabaton_x1s.lib.models")
        return [
            models.Device(
                device_id=dev_id,
                name=row.get("name", ""),
                brand=row.get("brand"),
                device_class=row.get("device_class"),
                device_class_code=None,
                power_state=None,
                idle_behavior=None,
            )
            for dev_id, row in sorted(self._devices.items())
        ]

    async def commands(self, device_id):
        self.calls.append(("commands", {"device_id": device_id}))
        return []

    async def persist_ir_blob(self, **kwargs):
        self.calls.append(("persist_ir_blob", kwargs))
        return self._persist_result


def _run(coro):
    asyncio.run(coro)


def _called(proxy, name):
    return [kwargs for called, kwargs in proxy.calls if called == name]


# ----- parse_payload_hex ---------------------------------------------------


def test_parse_payload_hex_accepts_common_paste_formats() -> None:
    cli = _cli()
    expected = bytes.fromhex("01200010")
    assert cli.parse_payload_hex("01 20 00 10") == expected
    assert cli.parse_payload_hex("01200010") == expected
    assert cli.parse_payload_hex("01,20,00,10") == expected
    assert cli.parse_payload_hex("0x01 0x20 0x00 0x10") == expected
    assert cli.parse_payload_hex("01 20\n00 10\n") == expected


@pytest.mark.parametrize("text", ["zz", "01 2", "0xgg"])
def test_parse_payload_hex_rejects_bad_input(text: str) -> None:
    cli = _cli()
    with pytest.raises(ValueError):
        cli.parse_payload_hex(text)


# ----- testir --------------------------------------------------------------


def test_testir_plays_parsed_payload() -> None:
    cli = _cli()
    proxy = _StubProxy(play_ok=True)
    shell = cli.AsyncShell(proxy)

    _run(shell.cmd_testir(PAYLOAD_HEX))

    assert _called(proxy, "play") == [{"blob": PAYLOAD}]


def test_testir_refuses_short_or_invalid_payload() -> None:
    cli = _cli()
    proxy = _StubProxy()
    shell = cli.AsyncShell(proxy)

    _run(shell.cmd_testir("01 20"))          # too short
    _run(shell.cmd_testir("not hex"))        # not hex
    _run(shell.cmd_testir(""))               # usage

    assert proxy.calls == []
