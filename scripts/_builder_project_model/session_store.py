from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from .common import CANONICAL_PROVIDERS
from .session_schema import SESSION_STATES, parse_session_record


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _fsync_path(path: Path) -> None:
    # Best-effort durability barrier. os.fsync on a directory fd (used after os.replace to
    # persist the rename) is unsupported on some container mounts and raises EBADF/EINVAL; a
    # filesystem that cannot fsync a directory must NOT crash the daemon on every heartbeat.
    # Correctness comes from the atomic os.replace; the fsync only tightens durability.
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    payload = json.dumps(data, indent=2, sort_keys=False) + "\n"
    tmp.write_text(payload, encoding="utf-8")
    _fsync_path(tmp)
    os.replace(tmp, path)
    _fsync_path(path.parent)


def _atomic_unlink(path: Path) -> None:
    if not path.exists():
        return
    path.unlink()
    _fsync_path(path.parent)


@dataclass(frozen=True)
class SessionPaths:
    home_root: Path

    @property
    def state_root(self) -> Path:
        return self.home_root / "state"

    @property
    def sessions_dir(self) -> Path:
        return self.state_root / "sessions"

    @property
    def providers_dir(self) -> Path:
        return self.state_root / "providers"

    @property
    def allocation_path(self) -> Path:
        return self.state_root / "allocation.json"

    @property
    def daemon_path(self) -> Path:
        return self.state_root / "daemon.json"

    @property
    def scheduler_lock_path(self) -> Path:
        return self.state_root / "scheduler.lock"

    def session_path(self, slot_id: str) -> Path:
        return self.sessions_dir / f"{slot_id}.json"

    def launcher_path(self, slot_id: str) -> Path:
        return self.sessions_dir / f"{slot_id}.launcher.json"

    def provider_path(self, provider: str) -> Path:
        return self.providers_dir / f"{provider}.json"


def _read_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a JSON object")
    return data


def _parse_live_session(path: Path) -> dict[str, Any]:
    return parse_session_record(path).data


def read_provider_state(path: Path, *, provider: str) -> dict[str, Any]:
    if not path.exists():
        return {
            "schema_version": 1,
            "provider": provider,
            "cooldown_until": None,
            "reason_class": None,
            "source_repo_id": None,
            "source_attempt_id": None,
            "updated_at": _utc_now(),
        }
    data = _read_json(path)
    return data


class SessionStore:
    def __init__(self, home_root: Path):
        self.paths = SessionPaths(Path(home_root).resolve())

    def ensure_layout(self) -> None:
        self.paths.sessions_dir.mkdir(parents=True, exist_ok=True)
        self.paths.providers_dir.mkdir(parents=True, exist_ok=True)

    def list_sessions(self) -> list[dict[str, Any]]:
        self.ensure_layout()
        sessions: list[dict[str, Any]] = []
        for path in sorted(self.paths.sessions_dir.glob("*.json")):
            if path.name.endswith(".launcher.json"):
                continue
            sessions.append(_parse_live_session(path))
        return sessions

    def load_session(self, slot_id: str) -> dict[str, Any]:
        return _parse_live_session(self.paths.session_path(slot_id))

    def write_session(self, data: dict[str, Any]) -> None:
        _atomic_write_json(self.paths.session_path(str(data["slot_id"])), data)

    def write_launcher_state(self, slot_id: str, data: dict[str, Any]) -> None:
        _atomic_write_json(self.paths.launcher_path(slot_id), data)

    def read_launcher_state(self, slot_id: str) -> dict[str, Any] | None:
        path = self.paths.launcher_path(slot_id)
        if not path.exists():
            return None
        return _read_json(path)

    def close_session(self, slot_id: str) -> None:
        _atomic_unlink(self.paths.session_path(slot_id))
        launcher = self.paths.launcher_path(slot_id)
        if launcher.exists():
            _atomic_unlink(launcher)

    def consuming_sessions(self, provider: str) -> list[dict[str, Any]]:
        if provider not in CANONICAL_PROVIDERS:
            raise ValueError(f"unknown provider {provider!r}")
        return [
            row for row in self.list_sessions()
            if row["provider"] == provider and row["state"] in {"starting", "active", "reaping"}
        ]

    def capacity_remaining(self, provider: str, *, max_sessions: int) -> int:
        return max(0, max_sessions - len(self.consuming_sessions(provider)))

    def reserve_slot(
        self,
        *,
        provider: str,
        max_sessions: int,
        daemon_instance_id: str,
        owner_pid: int,
        owner_pid_start_ticks: int,
        repo_id: str,
        queue_root: Path,
        work_id: str,
        attempt_id: str,
        lane: str,
        project_attribution: str,
        release_name: str | None,
        slot_id: str | None = None,
        now: str | None = None,
        crash_hook=None,
    ) -> dict[str, Any]:
        self.ensure_layout()
        if provider not in CANONICAL_PROVIDERS:
            raise ValueError(f"unknown provider {provider!r}")
        if self.capacity_remaining(provider, max_sessions=max_sessions) <= 0:
            raise RuntimeError(f"provider {provider} has no available session slots")
        stamp = now or _utc_now()
        record = {
            "schema_version": 1,
            "slot_id": slot_id or str(uuid4()),
            "provider": provider,
            "state": "starting",
            "daemon_instance_id": daemon_instance_id,
            "owner_pid": owner_pid,
            "owner_pid_start_ticks": owner_pid_start_ticks,
            "repo_id": repo_id,
            "queue_root": str(Path(queue_root).resolve()),
            "work_id": work_id,
            "attempt_id": attempt_id,
            "lane": lane,
            "project_attribution": project_attribution,
            "release_name": release_name,
            "reserved_at": stamp,
            "updated_at": stamp,
            "pgid": None,
            "pgid_leader_start_ticks": None,
            "executable": None,
            "command_digest": None,
        }
        if crash_hook is not None:
            crash_hook("before_reservation_write", record)
        self.write_session(record)
        if crash_hook is not None:
            crash_hook("after_reservation_write", record)
        return record

    def update_state(
        self,
        slot_id: str,
        *,
        state: str,
        previous_state: str | None = None,
        pgid: int | None = None,
        pgid_leader_start_ticks: int | None = None,
        executable: str | None = None,
        command_digest: str | None = None,
        updated_at: str | None = None,
    ) -> dict[str, Any]:
        if state not in SESSION_STATES:
            raise ValueError(f"unknown session state {state!r}")
        record = self.load_session(slot_id)
        record["state"] = state
        if previous_state is not None:
            record["previous_state"] = previous_state
        if pgid is not None:
            record["pgid"] = pgid
        if pgid_leader_start_ticks is not None:
            record["pgid_leader_start_ticks"] = pgid_leader_start_ticks
        if executable is not None:
            record["executable"] = executable
        if command_digest is not None:
            record["command_digest"] = command_digest
        record["updated_at"] = updated_at or _utc_now()
        self.write_session(record)
        return record

    def read_provider(self, provider: str) -> dict[str, Any]:
        return read_provider_state(self.paths.provider_path(provider), provider=provider)

    def write_provider(
        self,
        provider: str,
        *,
        cooldown_until: str | None,
        reason_class: str | None,
        source_repo_id: str | None,
        source_attempt_id: str | None,
        updated_at: str | None = None,
    ) -> dict[str, Any]:
        if provider not in CANONICAL_PROVIDERS:
            raise ValueError(f"unknown provider {provider!r}")
        record = {
            "schema_version": 1,
            "provider": provider,
            "cooldown_until": cooldown_until,
            "reason_class": reason_class,
            "source_repo_id": source_repo_id,
            "source_attempt_id": source_attempt_id,
            "updated_at": updated_at or _utc_now(),
        }
        _atomic_write_json(self.paths.provider_path(provider), record)
        return record

    def read_allocation(self) -> dict[str, Any]:
        if not self.paths.allocation_path.exists():
            return {
                "schema_version": 1,
                "providers": {},
            }
        return _read_json(self.paths.allocation_path)

    def write_allocation(self, data: dict[str, Any]) -> None:
        _atomic_write_json(self.paths.allocation_path, data)
