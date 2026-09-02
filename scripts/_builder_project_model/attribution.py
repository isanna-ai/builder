from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from _dispatch_runtime.paths import runtime_dir
from _dispatch_runtime.queue_store import QueueStore
from _dispatch_runtime.state_model import TERMINAL_STATES
from _yaml import yaml  # type: ignore


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _fsync_path(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    _fsync_path(tmp)
    os.replace(tmp, path)
    _fsync_path(path.parent)


def _read_yaml(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return data if isinstance(data, dict) else None


@dataclass(frozen=True)
class Membership:
    project_id: str
    release_name: str | None
    roadmap_index: int | None


@dataclass(frozen=True)
class AdmissionReceipt:
    data: dict[str, Any]

    @property
    def admission_id(self) -> str:
        return str(self.data["admission_id"])

    @property
    def repo_id(self) -> str:
        return str(self.data["repo_id"])

    @property
    def spec_id(self) -> str:
        return str(self.data["spec_id"])

    @property
    def project_id(self) -> str:
        return str(self.data["project_id"])

    @property
    def release_name(self) -> str | None:
        value = self.data.get("release_name")
        return None if value in (None, "") else str(value)

    @property
    def roadmap_index(self) -> int | None:
        value = self.data.get("roadmap_index")
        return int(value) if isinstance(value, int) else None

    @property
    def work_id(self) -> str | None:
        value = self.data.get("work_id")
        return None if value in (None, "") else str(value)

    @property
    def attempt_id(self) -> str | None:
        value = self.data.get("attempt_id")
        return None if value in (None, "") else str(value)

    @property
    def terminal(self) -> bool:
        return bool(self.data.get("terminal", False))

    @property
    def memberships(self) -> list[dict[str, Any]]:
        rows = self.data.get("memberships") or []
        return list(rows) if isinstance(rows, list) else []


class AdmissionStore:
    def __init__(self, home_root: Path):
        self.home_root = Path(home_root).resolve()
        self.admissions_dir = self.home_root / "state" / "admissions"

    def path_for(self, admission_id: str) -> Path:
        return self.admissions_dir / f"{admission_id}.json"

    def list_receipts(self) -> list[AdmissionReceipt]:
        if not self.admissions_dir.exists():
            return []
        rows: list[AdmissionReceipt] = []
        for path in sorted(self.admissions_dir.glob("*.json")):
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                rows.append(AdmissionReceipt(data))
        return rows

    def find_physical(self, repo_id: str, spec_id: str) -> list[AdmissionReceipt]:
        return [row for row in self.list_receipts() if row.repo_id == repo_id and row.spec_id == spec_id]

    def first_active(self, repo_id: str, spec_id: str) -> AdmissionReceipt | None:
        for row in self.find_physical(repo_id, spec_id):
            if not row.terminal:
                return row
        return None

    def write_receipt(
        self,
        *,
        admission_id: str,
        repo_id: str,
        spec_id: str,
        project_id: str,
        release_name: str | None,
        roadmap_index: int | None,
        work_id: str | None,
        attempt_id: str | None,
        membership: Membership,
    ) -> AdmissionReceipt:
        now = _utc_now()
        existing = self.first_active(repo_id, spec_id)
        if existing is not None:
            data = dict(existing.data)
            memberships = data.get("memberships") or []
            memberships.append(
                {
                    "project_id": membership.project_id,
                    "release_name": membership.release_name,
                    "roadmap_index": membership.roadmap_index,
                }
            )
            data["memberships"] = memberships
            data["updated_at"] = now
            _atomic_write_json(self.path_for(existing.admission_id), data)
            return AdmissionReceipt(data)
        data = {
            "schema_version": 1,
            "admission_id": admission_id,
            "repo_id": repo_id,
            "spec_id": spec_id,
            "project_id": project_id,
            "release_name": release_name,
            "roadmap_index": roadmap_index,
            "work_id": work_id,
            "attempt_id": attempt_id,
            "terminal": False,
            "memberships": [
                {
                    "project_id": membership.project_id,
                    "release_name": membership.release_name,
                    "roadmap_index": membership.roadmap_index,
                }
            ],
            "created_at": now,
            "updated_at": now,
        }
        _atomic_write_json(self.path_for(admission_id), data)
        return AdmissionReceipt(data)

    def mark_terminal(self, admission_id: str, *, attempt_id: str | None = None) -> None:
        path = self.path_for(admission_id)
        if not path.exists():
            return
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return
        data["terminal"] = True
        data["attempt_id"] = attempt_id
        data["updated_at"] = _utc_now()
        _atomic_write_json(path, data)


def queue_has_physical_spec(queue_root: Path, *, spec_id: str) -> bool:
    store = QueueStore(queue_root)
    snapshot = store.reconstruct()
    for item in snapshot.items.values():
        if str(item.task_ref.get("spec_id") or "") == spec_id:
            return True
    return False


def spec_is_satisfied(repo_root: Path, *, spec_id: str) -> bool:
    spec_yaml = runtime_dir(repo_root) / "specs" / spec_id / "spec.yaml"
    data = _read_yaml(spec_yaml)
    status = str((data or {}).get("status", "")).strip().lower()
    return status in {"verified", "archived"}


def should_enqueue_physical(
    *,
    home_root: Path,
    repo_id: str,
    repo_root: Path,
    queue_root: Path,
    spec_id: str,
) -> bool:
    if queue_has_physical_spec(queue_root, spec_id=spec_id):
        return False
    if spec_is_satisfied(repo_root, spec_id=spec_id):
        return False
    store = AdmissionStore(home_root)
    if store.find_physical(repo_id, spec_id):
        return False
    return True


def receipt_for_work(home_root: Path, *, repo_id: str, spec_id: str) -> AdmissionReceipt | None:
    return AdmissionStore(home_root).first_active(repo_id, spec_id)


def resolve_admission_repo(home_root: Path, *, admission_id: str) -> str:
    repo_ids = {receipt.repo_id for receipt in AdmissionStore(home_root).list_receipts() if receipt.admission_id == admission_id}
    if not repo_ids:
        raise ValueError(f"admission {admission_id!r} did not resolve to any repo")
    if len(repo_ids) != 1:
        raise ValueError(f"admission {admission_id!r} is ambiguous across repos: {sorted(repo_ids)}")
    return next(iter(repo_ids))


def physical_is_terminal(queue_root: Path, *, work_id: str) -> bool:
    store = QueueStore(queue_root)
    item = store.get_item(work_id)
    return bool(item is not None and item.state in TERMINAL_STATES)
