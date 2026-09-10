"""Value types for a managed Wifi Device deployed by a library consumer.

A *managed* Wifi Device is one the consumer created and keeps a record
of: N command slots (each a short and a long press record), optional
power and activity-start hooks, and one callback target every record
points at. The Home Assistant integration expresses the same thing
through its command-config store; this module is the store-free form a
server or a script uses with :meth:`AsyncXProxy.deploy_wifi_device` and
:meth:`AsyncXProxy.update_wifi_device`.

Three rules keep deploy and update in step (callbacks plan, C0a):

* :func:`snapshot_from_spec` is the ONE normalization. Deploy builds the
  create profile from it, update builds the desired and the deployed
  expansions from it. An unchanged spec therefore plans nothing.
* Every slot is always written, defaults included: shorts at ``1..N``
  and longs at ``N+1..2N`` with ``N`` fixed at :data:`WIFI_SLOT_COUNT`
  (the long-record id law the in-place planner and the remote rely on).
* The spec carries no favorites, hard buttons or activity memberships.
  Those are made with the generic edit intents against the device's
  command ids and the planner's ownership rule leaves them alone.

Nothing here talks to a hub.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence

from .hub_versions import HUB_VERSION_X1
from .wifi_inplace_plan import (
    WIFI_COMMAND_LONG_PRESS_OFFSET,
    WIFI_COMMAND_SLOT_COUNT,
    ManagedWifiSnapshot,
    WifiCommandSlot,
)

__all__ = [
    "DEFAULT_WIFI_BRAND",
    "WIFI_SLOT_COUNT",
    "X1_CALLBACK_PORT",
    "WifiSlotSpec",
    "WifiDeviceSpec",
    "WifiTarget",
    "WifiDeployment",
    "snapshot_from_spec",
    "labels_from_spec",
    "command_defs_from_spec",
]

#: Slots per managed device; longs live at ``slot + WIFI_SLOT_COUNT``.
WIFI_SLOT_COUNT = WIFI_COMMAND_SLOT_COUNT
#: Brand written on the device head; consumers may override per spec.
DEFAULT_WIFI_BRAND = "m3tac0de"
#: The X1 Roku replay always calls port 8060 (the head carries no port).
X1_CALLBACK_PORT = 8060
#: Label width the hub keeps (30 ASCII on X1, 60 UTF-16 bytes elsewhere);
#: the spec caps at the shorter so labels round-trip on every hub.
MAX_SLOT_LABEL_LEN = 30
MAX_DEVICE_NAME_LEN = 30

_LONG_PRESS_OFFSET = WIFI_COMMAND_LONG_PRESS_OFFSET


def _clean(text: Any, *, what: str, limit: int) -> str:
    clean = " ".join(str(text or "").split())
    if not clean:
        raise ValueError(f"{what} needs a name")
    if len(clean) > limit:
        raise ValueError(f"{what} {clean!r} is longer than {limit} characters")
    return clean


def _slot_index(value: Any, *, what: str) -> int:
    try:
        index = int(value)
    except (TypeError, ValueError) as err:
        raise ValueError(f"{what} must be a slot number 1..{WIFI_SLOT_COUNT}") from err
    if index < 1 or index > WIFI_SLOT_COUNT:
        raise ValueError(f"{what} {index} is outside 1..{WIFI_SLOT_COUNT}")
    return index


@dataclass(frozen=True)
class WifiSlotSpec:
    """One command slot: the short-press label and the long-press label.

    ``long_label`` ``None`` means ``"<label> Long"``; the long record always
    exists, a consumer that does not want long presses ignores them.
    """

    label: str
    long_label: Optional[str] = None

    def normalized(self, index: int) -> "WifiSlotSpec":
        label = _clean(self.label or f"Button {index}", what=f"slot {index}", limit=MAX_SLOT_LABEL_LEN)
        long_label = self.long_label
        if long_label is None or not str(long_label).strip():
            long_label = f"{label} Long"
        long_label = _clean(long_label, what=f"slot {index} long press", limit=MAX_SLOT_LABEL_LEN)
        return WifiSlotSpec(label=label, long_label=long_label)

    def to_dict(self) -> dict[str, Any]:
        return {"label": self.label, "long_label": self.long_label}

    @classmethod
    def from_dict(cls, data: Any) -> "WifiSlotSpec":
        if isinstance(data, str):
            return cls(label=data)
        if not isinstance(data, Mapping):
            raise ValueError("a slot is a label or a {label, long_label} mapping")
        return cls(label=str(data.get("label") or ""), long_label=data.get("long_label"))


@dataclass(frozen=True)
class WifiDeviceSpec:
    """What a consumer asks for. Persist the :meth:`normalized` form.

    ``slots`` may be shorter than :data:`WIFI_SLOT_COUNT`; the rest are
    ``Button n``. ``power_on_slot`` / ``power_off_slot`` are the slots the
    hub fires on an activity's power transitions, ``input_slots`` the
    slots offered as activity-start inputs (both 1-based; X1S/X2 only,
    the X1 firmware fires one power and one input callback per
    transition regardless and these are ignored there). A slot cannot be
    both a power hook and an input, as in the Home Assistant editor.
    """

    name: str
    slots: tuple[WifiSlotSpec, ...] = ()
    power_on_slot: Optional[int] = None
    power_off_slot: Optional[int] = None
    input_slots: tuple[int, ...] = ()
    brand: str = DEFAULT_WIFI_BRAND

    def normalized(self) -> "WifiDeviceSpec":
        """The canonical form: names cleaned, every slot present, hooks checked.

        Idempotent; ``spec.normalized() == spec.normalized().normalized()``.
        Raises ``ValueError`` for anything a hub would refuse or a later
        update could not reproduce.
        """

        name = _clean(self.name, what="a wifi device", limit=MAX_DEVICE_NAME_LEN)
        brand = _clean(self.brand or DEFAULT_WIFI_BRAND, what="the brand", limit=MAX_DEVICE_NAME_LEN)
        raw_slots = list(self.slots or ())
        if len(raw_slots) > WIFI_SLOT_COUNT:
            raise ValueError(f"a wifi device has at most {WIFI_SLOT_COUNT} slots, got {len(raw_slots)}")
        slots: list[WifiSlotSpec] = []
        for index in range(1, WIFI_SLOT_COUNT + 1):
            raw = raw_slots[index - 1] if index - 1 < len(raw_slots) else WifiSlotSpec(label=f"Button {index}")
            if not isinstance(raw, WifiSlotSpec):
                raw = WifiSlotSpec.from_dict(raw)
            slots.append(raw.normalized(index))

        power_on = None if self.power_on_slot is None else _slot_index(self.power_on_slot, what="power_on_slot")
        power_off = None if self.power_off_slot is None else _slot_index(self.power_off_slot, what="power_off_slot")
        inputs: list[int] = []
        for raw_input in self.input_slots or ():
            index = _slot_index(raw_input, what="an input slot")
            if index in inputs:
                raise ValueError(f"input slot {index} is listed twice")
            inputs.append(index)
        for hook in (power_on, power_off):
            if hook is not None and hook in inputs:
                raise ValueError(f"slot {hook} cannot be both a power hook and an input")
        return WifiDeviceSpec(
            name=name,
            slots=tuple(slots),
            power_on_slot=power_on,
            power_off_slot=power_off,
            input_slots=tuple(inputs),
            brand=brand,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "slots": [slot.to_dict() for slot in self.slots],
            "power_on_slot": self.power_on_slot,
            "power_off_slot": self.power_off_slot,
            "input_slots": list(self.input_slots),
            "brand": self.brand,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "WifiDeviceSpec":
        if not isinstance(data, Mapping):
            raise ValueError("a wifi device spec is a mapping")
        return cls(
            name=str(data.get("name") or ""),
            slots=tuple(WifiSlotSpec.from_dict(row) for row in (data.get("slots") or ())),
            power_on_slot=data.get("power_on_slot"),
            power_off_slot=data.get("power_off_slot"),
            input_slots=tuple(data.get("input_slots") or ()),
            brand=str(data.get("brand") or DEFAULT_WIFI_BRAND),
        )


@dataclass(frozen=True)
class WifiTarget:
    """Where the hub calls back: an IPv4 host, a port, and the hub's action id.

    The records store a packed IPv4 address, so ``host`` must be dotted
    decimal. ``action_id`` is the hub's stable identifier (its MAC) that the
    library puts into every callback path; the facade fills it in from
    the engine, consumers read it back from the deployment.
    """

    host: str
    port: int
    action_id: str = ""

    def __post_init__(self) -> None:
        host = str(self.host or "").strip()
        try:
            ipaddress.IPv4Address(host)
        except (ipaddress.AddressValueError, ValueError) as err:
            raise ValueError(f"callback host {self.host!r} is not a dotted-decimal IPv4 address") from err
        object.__setattr__(self, "host", host)
        port = self.port
        if isinstance(port, bool) or not isinstance(port, int) or not (0 < port < 65536):
            raise ValueError(f"callback port must be 1..65535, got {self.port!r}")
        object.__setattr__(self, "action_id", str(self.action_id or "").strip())

    def to_dict(self) -> dict[str, Any]:
        return {"host": self.host, "port": self.port, "action_id": self.action_id}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "WifiTarget":
        if not isinstance(data, Mapping):
            raise ValueError("a wifi target is a mapping")
        return cls(host=str(data.get("host") or ""), port=int(data.get("port") or 0),
                   action_id=str(data.get("action_id") or ""))


@dataclass(frozen=True)
class WifiDeployment:
    """What :meth:`AsyncXProxy.deploy_wifi_device` returns and
    :meth:`AsyncXProxy.update_wifi_device` takes: everything a consumer must
    keep to edit the device later.

    ``labels`` is the ``command_id -> label`` map exactly as written (all
    ``2 * WIFI_SLOT_COUNT`` records); the update's drift gate compares the
    live device against it. ``spec`` is the normalized spec that produced
    them; the planner's ownership rule is scoped by its expansion.
    """

    device_id: int
    spec: WifiDeviceSpec
    target: WifiTarget
    labels: Mapping[int, str] = field(default_factory=dict)
    hub_version: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "device_id": int(self.device_id),
            "spec": self.spec.to_dict(),
            "target": self.target.to_dict(),
            "labels": {str(int(cid)): str(label) for cid, label in sorted(self.labels.items())},
            "hub_version": self.hub_version,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "WifiDeployment":
        if not isinstance(data, Mapping):
            raise ValueError("a wifi deployment is a mapping")
        labels_raw = data.get("labels") or {}
        if not isinstance(labels_raw, Mapping):
            raise ValueError("deployment labels must be a mapping of command id to label")
        return cls(
            device_id=int(data.get("device_id") or 0),
            spec=WifiDeviceSpec.from_dict(data.get("spec") or {}).normalized(),
            target=WifiTarget.from_dict(data.get("target") or {}),
            labels={int(cid): str(label) for cid, label in labels_raw.items()},
            hub_version=str(data.get("hub_version") or ""),
        )


def _hooks(spec: WifiDeviceSpec, hub_version: Optional[str]) -> tuple[Optional[int], Optional[int], tuple[int, ...]]:
    """Power and input hooks as command ids, or none on the X1."""

    if str(hub_version or "") == HUB_VERSION_X1:
        return None, None, ()
    return spec.power_on_slot, spec.power_off_slot, tuple(spec.input_slots)


def labels_from_spec(spec: WifiDeviceSpec) -> dict[int, str]:
    """The ``command_id -> label`` map a deploy writes (shorts then longs)."""

    normalized = spec.normalized()
    labels: dict[int, str] = {}
    for index, slot in enumerate(normalized.slots, start=1):
        labels[index] = slot.label
        labels[index + _LONG_PRESS_OFFSET] = str(slot.long_label)
    return labels


def snapshot_from_spec(
    spec: WifiDeviceSpec,
    *,
    device_id: int,
    hub_version: Optional[str] = None,
    target_host: Optional[str] = None,
) -> ManagedWifiSnapshot:
    """The planner-side view of ``spec``: desired for an update, deployed
    for the ownership scope, and the source of the create profile.

    No activity references and no device-page bindings: the consumer
    makes those with the generic intents, and the planner never touches
    what the deployed expansion did not create.
    """

    normalized = spec.normalized()
    power_on, power_off, inputs = _hooks(normalized, hub_version)
    slots: dict[int, WifiCommandSlot] = {}
    for cid, label in labels_from_spec(normalized).items():
        slots[cid] = WifiCommandSlot(
            command_id=cid,
            label=label,
            press_type="long" if cid > _LONG_PRESS_OFFSET else "short",
        )
    return ManagedWifiSnapshot(
        device_id=int(device_id) & 0xFF,
        device_name=normalized.name,
        brand=normalized.brand,
        power_on_command_id=power_on,
        power_off_command_id=power_off,
        input_command_ids=inputs,
        slots=slots,
        activities={},
        device_bindings=(),
        target_host=str(target_host or "") or None,
    )


def command_defs_from_spec(spec: WifiDeviceSpec) -> list[dict[str, Any]]:
    """The ``slots`` rows of the wifi create profile: shorts then longs,
    in the shape the Home Assistant deploy path hands the engine."""

    normalized = spec.normalized()
    defs: list[dict[str, Any]] = []
    for index, slot in enumerate(normalized.slots):
        defs.append({
            "display_name": slot.label,
            "trigger_name": slot.label,
            "press_type": "short",
            "command_index": index,
        })
    for index, slot in enumerate(normalized.slots):
        defs.append({
            "display_name": str(slot.long_label),
            "trigger_name": slot.label,
            "press_type": "long",
            "command_index": index,
        })
    return defs
