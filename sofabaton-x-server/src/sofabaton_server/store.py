"""The server's files: ``hubs.json`` (the hub records), one
``state-<hub_id>.json`` per hub (the library's cache document) and the
apply records under ``applies/<hub_id>/`` (phase 4 plan, decision 13).

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
from dataclasses import dataclass, field
from datetime import datetime, timezone
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


APPLIES_DIR = "applies"
APPLY_RECORD_KIND = "sofabaton_apply_record"


@dataclass
class ApplyRecord:
    """One document write as the server keeps it: the library's ``ApplyState``
    document plus what the server adds (the job, the idempotency key, a
    digest of the desired document to detect a reused key)."""

    apply_id: str
    hub_id: str
    state: dict[str, Any]
    idempotency_key: Optional[str] = None
    desired_digest: Optional[str] = None
    job_id: Optional[str] = None
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).replace(microsecond=0).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).replace(microsecond=0).isoformat())

    @property
    def status(self) -> str:
        return str(self.state.get("status") or "queued")

    @property
    def finished(self) -> bool:
        return self.status in ("success", "stopped", "cancelled")

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": APPLY_RECORD_KIND, "schema": 1,
            "apply_id": self.apply_id, "hub_id": self.hub_id,
            "idempotency_key": self.idempotency_key, "desired_digest": self.desired_digest,
            "job_id": self.job_id, "created_at": self.created_at, "updated_at": self.updated_at,
            "state": self.state,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ApplyRecord":
        if data.get("kind") != APPLY_RECORD_KIND or int(data.get("schema") or 0) != 1:
            raise ValueError("not an apply record")
        return cls(
            apply_id=str(data["apply_id"]), hub_id=str(data["hub_id"]), state=dict(data.get("state") or {}),
            idempotency_key=data.get("idempotency_key"), desired_digest=data.get("desired_digest"),
            job_id=data.get("job_id"), created_at=str(data.get("created_at") or ""),
            updated_at=str(data.get("updated_at") or ""),
        )


class ApplyStore:
    """``applies/<hub_id>/<apply_id>.json``; ``keep`` finished records per hub."""

    def __init__(self, data_dir: Path, *, keep: int = 20) -> None:
        self.data_dir = Path(data_dir)
        self.keep = max(0, int(keep))

    @staticmethod
    def _safe(name: str) -> str:
        return "".join(ch if ch.isalnum() or ch in "-._" else "_" for ch in name)

    def hub_dir(self, hub_id: str) -> Path:
        return self.data_dir / APPLIES_DIR / self._safe(hub_id)

    def path(self, hub_id: str, apply_id: str) -> Path:
        return self.hub_dir(hub_id) / f"{self._safe(apply_id)}.json"

    def save(self, record: ApplyRecord) -> None:
        _write_atomic(self.path(record.hub_id, record.apply_id), json.dumps(record.to_dict(), sort_keys=True))

    def load(self, hub_id: str, apply_id: str) -> Optional[ApplyRecord]:
        path = self.path(hub_id, apply_id)
        if not path.exists():
            return None
        try:
            return ApplyRecord.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError, KeyError):
            return None

    def list(self, hub_id: str) -> list[ApplyRecord]:
        """Every readable record of the hub, newest ``updated_at`` first."""

        folder = self.hub_dir(hub_id)
        if not folder.exists():
            return []
        records = []
        for path in folder.glob("*.json"):
            if path.name.startswith("."):
                continue  # an atomic write in progress
            try:
                records.append(ApplyRecord.from_dict(json.loads(path.read_text(encoding="utf-8"))))
            except (OSError, ValueError, KeyError):
                continue
        records.sort(key=lambda r: (r.updated_at, r.created_at), reverse=True)
        return records

    def find_by_key(self, hub_id: str, idempotency_key: str) -> Optional[ApplyRecord]:
        for record in self.list(hub_id):
            if record.idempotency_key == idempotency_key:
                return record
        return None

    def delete(self, hub_id: str, apply_id: str) -> None:
        path = self.path(hub_id, apply_id)
        if path.exists():
            path.unlink()

    def prune(self, hub_id: str) -> int:
        """Prune success/stopped/cancelled beyond ``keep``, including resumable records.

        Queued/running records remain; retention is not restart recovery.
        """

        finished = [r for r in self.list(hub_id) if r.finished]
        dropped = 0
        for record in finished[self.keep:]:
            self.delete(hub_id, record.apply_id)
            dropped += 1
        return dropped

    def delete_hub(self, hub_id: str) -> None:
        folder = self.hub_dir(hub_id)
        if folder.exists():
            for path in folder.glob("*.json"):
                path.unlink()
            try:
                folder.rmdir()
            except OSError:
                pass
