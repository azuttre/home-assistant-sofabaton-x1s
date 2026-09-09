"""``hubs.json``: the hub records, written atomically on every change.

The only persistence the server has (plan section 5). One JSON object:
``{"schema": 1, "hubs": [record, ...]}``. Records are the library's
``HubConfig`` plus the server's own fields; see ``models.HubRecord``.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

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
