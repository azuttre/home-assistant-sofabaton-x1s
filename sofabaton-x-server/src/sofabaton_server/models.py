"""Server-side types: hub records, response views, request bodies, errors.

Responses are stdlib dataclasses (the library's own types are reused
unchanged); request bodies are pydantic models so a bad payload is a
422 with field detail rather than a 500.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from pydantic import BaseModel, Field

from sofabaton import HubConfig, HubStatus


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def mac_key(mac: str) -> str:
    """Normalise a MAC to the hub-id form: lower-case hex, no separators."""

    return "".join(ch for ch in mac.lower() if ch in "0123456789abcdef")


# -- persistence record ----------------------------------------------------


@dataclass
class HubRecord:
    """One configured hub as stored in ``hubs.json``.

    ``hub_id`` is the banner MAC (``mac_key`` form) once known, the host
    string before that (plan decision 9).
    """

    hub_id: str
    config: HubConfig
    enabled: bool = True
    added_at: str = field(default_factory=now_iso)
    last_seen: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "hub_id": self.hub_id,
            "config": self.config.to_dict(),
            "enabled": self.enabled,
            "added_at": self.added_at,
            "last_seen": self.last_seen,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "HubRecord":
        return cls(
            hub_id=str(data["hub_id"]),
            config=HubConfig.from_dict(data["config"]),
            enabled=bool(data.get("enabled", True)),
            added_at=str(data.get("added_at") or now_iso()),
            last_seen=data.get("last_seen"),
        )


# -- response views ----------------------------------------------------------


@dataclass(frozen=True)
class HubView:
    """What ``/hubs`` returns per hub: the record plus a status snapshot."""

    hub_id: str
    enabled: bool
    config: HubConfig
    added_at: str
    last_seen: Optional[str]
    status: Optional[HubStatus]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Problem:
    """Error body (RFC 9457 shape, minus the URI scheme)."""

    type: str
    title: str
    status: int
    detail: Optional[str] = None
    hub_id: Optional[str] = None
    mode: Optional[str] = None


# -- request bodies ------------------------------------------------------------


class HubCreate(BaseModel):
    """Body of ``POST /hubs``: the ``HubConfig`` fields, host required."""

    host: str = Field(min_length=1)
    port: int = 8102
    name: Optional[str] = None
    mac: Optional[str] = None
    txt: dict[str, str] = Field(default_factory=dict)
    hub_version: Optional[str] = None
    hub_listen_port: int = 8200
    app_discovery_port: int = 8102
    proxy_enabled: bool = True
    is_proxy: bool = False
    source: Optional[str] = None
    enabled: bool = True

    def to_config(self) -> HubConfig:
        data = self.model_dump()
        data.pop("enabled")
        return HubConfig.from_dict(data)
