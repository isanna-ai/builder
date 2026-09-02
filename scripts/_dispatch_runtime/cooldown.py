"""Lane cooldown helpers."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from _dispatch_runtime.lane_executor import DispatchResult
from _dispatch_runtime.queue_store import LaneRecord, QueueStore


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _format_datetime(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _resolve_retry_after(retry_after: str | None, default_seconds: int) -> str:
    if retry_after is None:
        return _format_datetime(_utc_now() + timedelta(seconds=default_seconds))
    if retry_after.startswith("PT") and retry_after.endswith("S"):
        seconds = int(retry_after[2:-1])
        return _format_datetime(_utc_now() + timedelta(seconds=seconds))
    return retry_after


def open_lane_cooldown(
    store: QueueStore,
    lane_name: str,
    result: DispatchResult,
    cooldown_policy: dict[str, Any],
) -> LaneRecord:
    until = _resolve_retry_after(result.retry_after, int(cooldown_policy.get("default_seconds", 300)))
    return store.set_lane_cooldown(lane_name, until=until, reason="rate_limited")


def cooldown_remaining_seconds(record: LaneRecord, *, now: datetime | None = None) -> int:
    if record.cooldown_until is None:
        return 0
    current = now or _utc_now()
    remaining = int((_parse_datetime(record.cooldown_until) - current).total_seconds())
    return max(0, remaining)


def lane_available(record: LaneRecord | None, *, now: datetime | None = None) -> bool:
    if record is None or record.cooldown_until is None:
        return True
    return cooldown_remaining_seconds(record, now=now) == 0
