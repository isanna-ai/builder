"""Retry backoff classification helpers."""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone
from typing import Any

from _dispatch_runtime.lane_executor import DispatchResult
from _dispatch_runtime.queue_store import QueueStore, WorkItem
from _dispatch_runtime.state_model import WorkItemState


def _format_datetime(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def apply_failure_backoff(
    store: QueueStore,
    work_id: str,
    result: DispatchResult,
    retry_policy: dict[str, Any],
    *,
    now: datetime | None = None,
) -> WorkItem:
    item = store.get_item(work_id)
    if item is None:
        raise KeyError(f"unknown work item: {work_id}")

    current_attempt = item.attempt
    if item.state == WorkItemState.QUEUED:
        current_attempt += 1
        item.attempt = current_attempt

    max_attempts = int(retry_policy.get("max_attempts", item.max_attempts))
    item.max_attempts = max_attempts
    item.task_ref["last_error"] = str(result.metadata.get("message") or result.result_type.value)

    if current_attempt >= max_attempts:
        item.state = WorkItemState.FAILED
        item.scheduled_after = None
        item.lease = {}
        return store.save_item(item)

    initial_seconds = int(retry_policy.get("initial_seconds", 30))
    max_seconds = int(retry_policy.get("max_seconds", 900))
    jitter_seconds = max(0, int(retry_policy.get("jitter_seconds", 0)))
    delay_seconds = min(max_seconds, initial_seconds * (2 ** max(0, current_attempt - 1)))
    if jitter_seconds:
        delay_seconds = min(max_seconds, delay_seconds + random.randint(0, jitter_seconds))
    effective_now = now or datetime.now(timezone.utc)
    item.state = WorkItemState.QUEUED
    item.scheduled_after = _format_datetime(effective_now + timedelta(seconds=delay_seconds))
    item.lease = {}
    return store.save_item(item)
