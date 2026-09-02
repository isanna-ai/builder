"""Durable file-backed dispatch queue store."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from _yaml import yaml  # type: ignore

from _dispatch_runtime.state_model import WorkItemState, normalize_state, transition


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _read_yaml(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected mapping")
    return data


def _write_yaml(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    tmp_path.replace(path)


@dataclass
class WorkItem:
    id: str
    state: WorkItemState
    task_ref: dict[str, Any]
    priority: int = 0
    attempt: int = 0
    max_attempts: int = 3
    scheduled_after: str | None = None
    lane: str | None = None
    lease: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> "WorkItem":
        # Guard against a mis-serialised lease (e.g. the string "{}" from an old
        # writer): dict("{}") raises and would crash queue reconstruction.
        raw_lease = record.get("lease") or {}
        if not isinstance(raw_lease, dict):
            raw_lease = {}
        return cls(
            id=str(record["id"]),
            state=normalize_state(record["state"]),
            task_ref=dict(record.get("task_ref", {})),
            priority=int(record.get("priority", 0)),
            attempt=int(record.get("attempt", 0)),
            max_attempts=int(record.get("max_attempts", 3)),
            scheduled_after=record.get("scheduled_after"),
            lane=record.get("lane"),
            lease=dict(raw_lease),
            created_at=str(record.get("created_at") or _now()),
            updated_at=str(record.get("updated_at") or _now()),
        )

    def to_record(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "state": self.state.value,
            "priority": self.priority,
            "attempt": self.attempt,
            "max_attempts": self.max_attempts,
            "scheduled_after": self.scheduled_after,
            "lane": self.lane,
            "lease": self.lease,
            "task_ref": self.task_ref,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass
class AttemptRecord:
    attempt_id: str
    work_id: str
    lane: str
    metadata: dict[str, Any]
    created_at: str

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> "AttemptRecord":
        # Same guard as WorkItem.from_record: a mis-serialised metadata string
        # must not crash reconstruction.
        raw_meta = record.get("metadata") or {}
        if not isinstance(raw_meta, dict):
            raw_meta = {}
        return cls(
            attempt_id=str(record["attempt_id"]),
            work_id=str(record["work_id"]),
            lane=str(record["lane"]),
            metadata=dict(raw_meta),
            created_at=str(record.get("created_at") or _now()),
        )

    def to_record(self) -> dict[str, Any]:
        return {
            "attempt_id": self.attempt_id,
            "work_id": self.work_id,
            "lane": self.lane,
            "metadata": self.metadata,
            "created_at": self.created_at,
        }


@dataclass
class LaneRecord:
    lane: str
    cooldown_until: str | None = None
    reason: str | None = None
    updated_at: str = field(default_factory=_now)

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> "LaneRecord":
        return cls(
            lane=str(record["lane"]),
            cooldown_until=record.get("cooldown_until"),
            reason=record.get("reason"),
            updated_at=str(record.get("updated_at") or _now()),
        )

    def to_record(self) -> dict[str, Any]:
        return {
            "lane": self.lane,
            "cooldown_until": self.cooldown_until,
            "reason": self.reason,
            "updated_at": self.updated_at,
        }


@dataclass
class EventRecord:
    event_id: str
    work_id: str
    event_type: str
    payload: dict[str, Any]
    created_at: str

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> "EventRecord":
        return cls(
            event_id=str(record["event_id"]),
            work_id=str(record["work_id"]),
            event_type=str(record["event_type"]),
            payload=dict(record.get("payload") or {}),
            created_at=str(record.get("created_at") or _now()),
        )

    def to_record(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "work_id": self.work_id,
            "event_type": self.event_type,
            "payload": self.payload,
            "created_at": self.created_at,
        }


@dataclass
class QueueSnapshot:
    items: dict[str, WorkItem]
    attempts: dict[str, AttemptRecord]
    lanes: dict[str, LaneRecord]
    events: list[EventRecord]


class QueueStore:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.queue_dir = self.root / "queue"
        self.items_dir = self.queue_dir / "items"
        self.attempts_dir = self.queue_dir / "attempts"
        self.lanes_dir = self.queue_dir / "lanes"
        self.events_dir = self.queue_dir / "events"
        for directory in (self.items_dir, self.attempts_dir, self.lanes_dir, self.events_dir):
            directory.mkdir(parents=True, exist_ok=True)

    def enqueue(
        self,
        *,
        task_ref: dict[str, Any],
        priority: int = 0,
        lane: str | None = None,
        max_attempts: int = 3,
        scheduled_after: str | None = None,
    ) -> WorkItem:
        timestamp = _now()
        item = WorkItem(
            id=f"work-{uuid4().hex}",
            state=WorkItemState.QUEUED,
            task_ref=dict(task_ref),
            priority=priority,
            lane=lane,
            max_attempts=max_attempts,
            scheduled_after=scheduled_after,
            created_at=timestamp,
            updated_at=timestamp,
        )
        self._write_item(item)
        self.append_event(item.id, "enqueue", {"state": item.state.value})
        return item

    def get_item(self, work_id: str) -> WorkItem | None:
        path = self.items_dir / f"{work_id}.yaml"
        if not path.exists():
            return None
        return WorkItem.from_record(_read_yaml(path))

    def save_item(self, item: WorkItem) -> WorkItem:
        item.updated_at = _now()
        self._write_item(item)
        return item

    def transition_item(
        self,
        work_id: str,
        requested: WorkItemState | str,
        *,
        lease: dict[str, Any] | None = None,
    ) -> WorkItem:
        item = self.get_item(work_id)
        if item is None:
            raise KeyError(f"unknown work item: {work_id}")
        previous = item.state
        item.state = transition(item.state, requested)
        if lease is not None:
            item.lease = dict(lease)
            item.lane = lease.get("lane", item.lane)
        item.updated_at = _now()
        self._write_item(item)
        self.append_event(
            item.id,
            "state_transition",
            {"from": previous.value, "to": item.state.value, "lease": item.lease},
        )
        return item

    def record_attempt(
        self,
        work_id: str,
        *,
        attempt_id: str,
        lane: str,
        metadata: dict[str, Any] | None = None,
    ) -> AttemptRecord:
        record = AttemptRecord(
            attempt_id=attempt_id,
            work_id=work_id,
            lane=lane,
            metadata=dict(metadata or {}),
            created_at=_now(),
        )
        _write_yaml(self.attempts_dir / f"{attempt_id}.yaml", record.to_record())
        self.append_event(work_id, "attempt_recorded", {"attempt_id": attempt_id, "lane": lane})
        return record

    def set_lane_cooldown(self, lane: str, *, until: str, reason: str | None = None) -> LaneRecord:
        record = LaneRecord(lane=lane, cooldown_until=until, reason=reason, updated_at=_now())
        _write_yaml(self.lanes_dir / f"{lane}.yaml", record.to_record())
        return record

    def append_event(self, work_id: str, event_type: str, payload: dict[str, Any] | None = None) -> EventRecord:
        event = EventRecord(
            event_id=f"{_now().replace(':', '').replace('-', '')}-{uuid4().hex}",
            work_id=work_id,
            event_type=event_type,
            payload=dict(payload or {}),
            created_at=_now(),
        )
        _write_yaml(self.events_dir / f"{event.event_id}.yaml", event.to_record())
        return event

    def reconstruct(self) -> QueueSnapshot:
        items = {
            path.stem: WorkItem.from_record(_read_yaml(path))
            for path in sorted(self.items_dir.glob("*.yaml"))
        }
        attempts = {
            path.stem: AttemptRecord.from_record(_read_yaml(path))
            for path in sorted(self.attempts_dir.glob("*.yaml"))
        }
        lanes = {
            path.stem: LaneRecord.from_record(_read_yaml(path))
            for path in sorted(self.lanes_dir.glob("*.yaml"))
        }
        events = [
            EventRecord.from_record(_read_yaml(path))
            for path in sorted(self.events_dir.glob("*.yaml"))
        ]
        return QueueSnapshot(items=items, attempts=attempts, lanes=lanes, events=events)

    def _write_item(self, item: WorkItem) -> None:
        _write_yaml(self.items_dir / f"{item.id}.yaml", item.to_record())
