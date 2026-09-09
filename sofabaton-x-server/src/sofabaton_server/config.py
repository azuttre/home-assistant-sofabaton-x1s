"""Server settings: defaults < config file < environment < CLI flags.

One place for every operator-facing knob (plan sections 8 and 9). Hub
records are NOT here: they live in ``hubs.json`` and are managed by the
hub manager (S1). This module only knows where the data directory is.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields, replace
from pathlib import Path
from typing import Any, Mapping, Optional

DEFAULT_PORT = 8480
DEFAULT_BIND = "0.0.0.0"
SETTINGS_FILE = "server.json"
ENV_PREFIX = "SOFABATON_"


@dataclass(frozen=True)
class Settings:
    """Everything the operator can set. Frozen; build a new one with ``with_``."""

    bind: str = DEFAULT_BIND
    port: int = DEFAULT_PORT
    data_dir: Path = field(default_factory=lambda: Path.cwd() / "data")
    # Reverse proxy (plan section 8): published as TXT ``base_url`` and
    # as the OpenAPI document's servers[0].url when set.
    advertise_url: Optional[str] = None
    root_path: str = ""
    # Addresses / CIDRs whose X-Forwarded-* headers are trusted.
    trusted_proxies: tuple[str, ...] = ()
    # Escape hatch for operators who bring their own certificate.
    tls_cert: Optional[Path] = None
    tls_key: Optional[Path] = None
    # Hosts to register on first start when hubs.json is empty.
    initial_hubs: tuple[str, ...] = ()
    log_level: str = "info"

    def __post_init__(self) -> None:
        if isinstance(self.port, bool) or not isinstance(self.port, int) or not (0 < self.port < 65536):
            raise ValueError(f"port must be a port number, got {self.port!r}")
        if not isinstance(self.bind, str) or not self.bind.strip():
            raise ValueError("bind must be a non-empty address")
        if (self.tls_cert is None) != (self.tls_key is None):
            raise ValueError("tls_cert and tls_key must be set together")
        if self.advertise_url is not None:
            url = self.advertise_url.strip().rstrip("/")
            if not url.startswith(("http://", "https://")):
                raise ValueError("advertise_url must start with http:// or https://")
            object.__setattr__(self, "advertise_url", url)
        root = (self.root_path or "").strip()
        if root and not root.startswith("/"):
            root = "/" + root
        object.__setattr__(self, "root_path", root.rstrip("/"))
        object.__setattr__(self, "data_dir", Path(self.data_dir))
        object.__setattr__(self, "trusted_proxies", tuple(str(p).strip() for p in self.trusted_proxies if str(p).strip()))
        object.__setattr__(self, "initial_hubs", tuple(str(h).strip() for h in self.initial_hubs if str(h).strip()))
        if self.tls_cert is not None:
            object.__setattr__(self, "tls_cert", Path(self.tls_cert))
            object.__setattr__(self, "tls_key", Path(self.tls_key))  # type: ignore[arg-type]

    # -- serialisation -----------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        for key in ("data_dir", "tls_cert", "tls_key"):
            if data[key] is not None:
                data[key] = str(data[key])
        data["trusted_proxies"] = list(self.trusted_proxies)
        data["initial_hubs"] = list(self.initial_hubs)
        return data

    def with_(self, **changes: Any) -> "Settings":
        return replace(self, **changes)


_FIELD_NAMES = {f.name for f in fields(Settings)}
_LIST_FIELDS = {"trusted_proxies", "initial_hubs"}


def _coerce(name: str, value: Any) -> Any:
    if value is None:
        return None
    if name == "port":
        return int(value)
    if name in _LIST_FIELDS:
        if isinstance(value, str):
            return tuple(v.strip() for v in value.split(",") if v.strip())
        return tuple(value)
    return value


def _filtered(overrides: Mapping[str, Any]) -> dict[str, Any]:
    """Keep known, non-None keys; reject unknown ones loudly."""

    unknown = sorted(set(overrides) - _FIELD_NAMES)
    if unknown:
        raise ValueError(f"unknown setting(s): {unknown}")
    return {k: _coerce(k, v) for k, v in overrides.items() if v is not None}


def settings_from_file(path: Path) -> dict[str, Any]:
    """Read ``server.json``; a missing file is an empty layer."""

    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a JSON object")
    return _filtered(data)


def settings_from_env(environ: Mapping[str, str] = os.environ) -> dict[str, Any]:
    """``SOFABATON_<FIELD>`` variables; ``SOFABATON_HUBS`` feeds ``initial_hubs``."""

    aliases = {"HUBS": "initial_hubs"}
    found: dict[str, Any] = {}
    for key, value in environ.items():
        if not key.startswith(ENV_PREFIX):
            continue
        name = key[len(ENV_PREFIX):]
        name = aliases.get(name, name.lower())
        if name in _FIELD_NAMES:
            found[name] = value
    return _filtered(found)


def load_settings(
    *,
    cli: Optional[Mapping[str, Any]] = None,
    environ: Mapping[str, str] = os.environ,
    data_dir: Optional[Path] = None,
) -> Settings:
    """Compose the layers. ``data_dir`` decides where ``server.json`` is read from.

    Precedence, highest first: CLI flags, environment, ``server.json`` in
    the data directory, defaults. The data directory itself may come from
    any layer, so it is resolved first from CLI, then environment, then
    the default.
    """

    cli_layer = _filtered(cli or {})
    env_layer = settings_from_env(environ)
    resolved_dir = Path(
        data_dir
        or cli_layer.get("data_dir")
        or env_layer.get("data_dir")
        or Settings().data_dir
    )
    file_layer = settings_from_file(resolved_dir / SETTINGS_FILE)
    merged: dict[str, Any] = {}
    for layer in (file_layer, env_layer, cli_layer):
        merged.update(layer)
    merged["data_dir"] = resolved_dir
    return Settings(**merged)
