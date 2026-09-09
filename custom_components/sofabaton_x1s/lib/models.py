# models.py: typed results returned by the asyncio facade.
#
# Plain stdlib dataclasses with a ``to_dict()`` so a consumer that speaks
# JSON (a REST/WebSocket server) can serialise them without knowing their
# shape, and a schema generator can derive components from the fields.
# The engine never sees these; aio.py builds them from engine state.
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Literal, Optional, Union

__all__ = [
    "HubMode",
    "HubStatus",
    "HubInfo",
    "RunningActivity",
    "Activity",
    "Device",
    "Command",
    "Button",
    "Macro",
    "Favorite",
    "EventKind",
    "ActivityChanged",
    "ConnectionState",
    "StatusChanged",
    "CatalogReady",
    "HubEvent",
]

# ``disconnected``: no hub session. ``observe``: hub connected but an app
# client holds it through the proxy (reads serve cache, sends refused).
# ``control``: the proxy owns the hub (reads fetch, sends work).
HubMode = Literal["disconnected", "observe", "control"]


@dataclass(frozen=True)
class RunningActivity:
    """The activity currently running on the hub."""

    activity_id: int
    name: Optional[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class HubStatus:
    """Live connection state of one proxied hub. Pure state read, no hub traffic."""

    hub_connected: bool
    app_connected: bool
    controllable: bool
    mode: HubMode
    hub_version: Optional[str]
    proxy_enabled: bool
    running_activity: Optional[RunningActivity]
    activities_cached: int
    devices_cached: int
    # True once the connect-time initial sync (banner, devices,
    # activities) has completed for the current hub session.
    catalog_ready: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class HubInfo:
    """Identity of the physical hub, as read from its connect banner.

    ``known`` is False until the banner has been read at least once; the
    other fields are then None.
    """

    known: bool
    model: Optional[str]
    name: Optional[str]
    mac: Optional[str]
    firmware_version: Optional[int]
    production_batch: Optional[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Catalog read results
# ---------------------------------------------------------------------------
#
# Everything is keyed on (entity_id, command_id): browse to get the ids,
# then ``send(entity_id, command_id)``. Each type carries its own id so a
# list is self-describing when serialised.


@dataclass(frozen=True)
class Activity:
    """One activity from the hub's catalog."""

    activity_id: int
    name: str
    active: bool
    needs_confirm: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Device:
    """One device from the hub's catalog.

    ``power_state`` is the hub's live power byte for the device (0 off,
    1 on) as of the last devices fetch; None when the row carried no
    parseable record. The hub commits it with a macro-runtime lag after a
    power fire, so it is not an instantaneous read. ``idle_behavior`` is
    the device's power-behaviour mode when known.
    """

    device_id: int
    name: str
    brand: Optional[str]
    device_class: Optional[str]
    device_class_code: Optional[int]
    power_state: Optional[int]
    idle_behavior: Optional[int]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Command:
    """A device command; send with ``send(device_id, command_id)``."""

    command_id: int
    label: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Button:
    """A remote button bound on an activity or device.

    ``button_code`` is what you send to that entity; ``device_id`` /
    ``command_id`` are the underlying target it maps to, None for an
    unbound slot. ``name`` is the ``ButtonName`` alias when the code has
    one.
    """

    button_code: int
    name: Optional[str]
    device_id: Optional[int]
    command_id: Optional[int]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Macro:
    """An activity macro; send with ``send(activity_id, command_id)``."""

    command_id: int
    label: Optional[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Favorite:
    """An activity favorite: a device command; send with ``send(device_id, command_id)``."""

    device_id: int
    command_id: int
    label: Optional[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Event stream
# ---------------------------------------------------------------------------
#
# One stream, one shape: every engine listener is folded into a
# ``HubEvent`` with a ``kind`` and a typed ``payload``, so a WebSocket
# relay (or any consumer) handles a single type. ``seq`` is a per-proxy
# monotonic counter: a gap means the consumer's queue overflowed and
# events were dropped (oldest first; see ``AsyncXProxy.events``).

EventKind = Literal[
    "activity_changed",
    "activity_list_updated",
    "hub_state",
    "app_state",
    "status_changed",
    "catalog_ready",
    "ota",
]


@dataclass(frozen=True)
class ActivityChanged:
    """The running activity changed (None = powered off / idle)."""

    activity_id: Optional[int]
    previous_activity_id: Optional[int]
    name: Optional[str]


@dataclass(frozen=True)
class ConnectionState:
    """A hub-side (``hub_state``) or app-side (``app_state``) link came up or went down."""

    connected: bool


@dataclass(frozen=True)
class StatusChanged:
    """The proxy's mode flipped (derived from hub and app connection state)."""

    mode: HubMode
    previous_mode: HubMode


@dataclass(frozen=True)
class CatalogReady:
    """The connect-time initial sync completed (True) or the session dropped (False)."""

    ready: bool


EventPayload = Optional[Union[ActivityChanged, ConnectionState, StatusChanged, CatalogReady]]


@dataclass(frozen=True)
class HubEvent:
    """One event from :meth:`AsyncXProxy.events`."""

    seq: int
    kind: EventKind
    payload: EventPayload = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "kind": self.kind,
            "payload": asdict(self.payload) if self.payload is not None else None,
        }
