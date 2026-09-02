"""Immutable dispatch event appenders for the queue control plane."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum

from _dispatch_runtime.queue_store import EventRecord, QueueStore


class EventType(StrEnum):
    ENQUEUE = "enqueue"
    LEASE_ACQUIRED = "lease_acquired"
    PROCESS_START = "process_start"
    HEARTBEAT = "heartbeat"
    RESULT = "result"
    COOLDOWN_OPEN = "cooldown_open"
    HUMAN_BLOCK = "human_block"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def append_enqueue_event(
    store: QueueStore,
    work_id: str,
    *,
    lane: str | None = None,
) -> EventRecord:
    payload: dict = {}
    if lane is not None:
        payload["lane"] = lane
    return store.append_event(work_id, EventType.ENQUEUE, payload)


def append_lease_acquired_event(
    store: QueueStore,
    work_id: str,
    *,
    lane: str,
    attempt_id: str,
) -> EventRecord:
    return store.append_event(work_id, EventType.LEASE_ACQUIRED, {"lane": lane, "attempt_id": attempt_id})


def append_process_start_event(
    store: QueueStore,
    work_id: str,
    *,
    attempt_id: str,
    pid: int | None = None,
) -> EventRecord:
    payload: dict = {"attempt_id": attempt_id}
    if pid is not None:
        payload["pid"] = pid
    return store.append_event(work_id, EventType.PROCESS_START, payload)


def append_heartbeat_event(
    store: QueueStore,
    work_id: str,
    *,
    attempt_id: str,
) -> EventRecord:
    return store.append_event(work_id, EventType.HEARTBEAT, {"attempt_id": attempt_id, "heartbeat_at": _now()})


def append_result_event(
    store: QueueStore,
    work_id: str,
    *,
    attempt_id: str,
    result_type: str,
) -> EventRecord:
    return store.append_event(work_id, EventType.RESULT, {"attempt_id": attempt_id, "result_type": result_type})


def append_cooldown_open_event(
    store: QueueStore,
    work_id: str,
    *,
    lane: str,
    until: str,
) -> EventRecord:
    return store.append_event(work_id, EventType.COOLDOWN_OPEN, {"lane": lane, "until": until})


def append_human_block_event(
    store: QueueStore,
    work_id: str,
    *,
    attempt_id: str,
) -> EventRecord:
    return store.append_event(work_id, EventType.HUMAN_BLOCK, {"attempt_id": attempt_id})
