"""The server's files: ``hubs.json`` (the hub records) and one
``state-<hub_id>.json`` per hub (the library's cache document).

``hubs.json`` is one JSON object, ``{"schema": 1, "hubs": [record, ...]}``,
written atomically on every change (plan section 5). The state files
(phase 3 plan, S7) hold ``AsyncXProxy.export_state()`` verbatim: they are
the library's own document and the server never reads inside them. A
warm state file is what makes the snapshot complete straight after a
restart without a minutes-long hub read.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Optional

SCHEMA = 1
HUBS_FILE = "hubs.json"


class HubStore:
    """Load and save the hub records under ``data_dir``."""

    def __init__(self, data_dir: Path) -> None:
        self.path = Path(data_dir) / HUBS_FILE

    def exists(self) -> bool:
        return self.path.exists()

    def load(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("schema") != SCHEMA:
            raise ValueError(f"{self.path}: unsupported hubs.json (schema {data.get('schema') if isinstance(data, dict) else '?'})")
        hubs = data.get("hubs")
        if not isinstance(hubs, list):
            raise ValueError(f"{self.path}: 'hubs' must be a list")
        return [dict(row) for row in hubs]

    def save(self, records: list[dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"schema": SCHEMA, "hubs": records}, indent=2, sort_keys=True)
        # Write beside the target and replace, so a crash mid-write leaves
        # the previous file intact.
        fd, tmp = tempfile.mkstemp(prefix=".hubs-", suffix=".json", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)


def _write_atomic(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.stem}-", suffix=".json", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


class StateStore:
    """One ``state-<hub_id>.json`` per hub under ``data_dir``."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = Path(data_dir)

    def path(self, hub_id: str) -> Path:
        safe = "".join(ch if ch.isalnum() or ch in "-._" else "_" for ch in hub_id)
        return self.data_dir / f"state-{safe}.json"

    def load(self, hub_id: str) -> Optional[dict[str, Any]]:
        path = self.path(hub_id)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def save(self, hub_id: str, document: dict[str, Any]) -> None:
        _write_atomic(self.path(hub_id), json.dumps(document, sort_keys=True))

    def rename(self, old_id: str, new_id: str) -> None:
        old, new = self.path(old_id), self.path(new_id)
        if old.exists() and old != new:
            os.replace(old, new)

    def delete(self, hub_id: str) -> None:
        path = self.path(hub_id)
        if path.exists():
            path.unlink()
