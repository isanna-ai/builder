"""Read-only status snapshot derived entirely from persisted queue records."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from _dispatch_runtime.cooldown import cooldown_remaining_seconds
from _dispatch_runtime.queue_store import EventRecord, QueueStore
from _dispatch_runtime.state_model import WorkItemState


def _parse_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


RECENT_EVENTS_LIMIT = 20


@dataclass
class StatusSnapshot:
    queue_depth: dict[str, int] = field(default_factory=dict)
    lane_inflight: dict[str, int] = field(default_factory=dict)
    lane_cooldown_remaining: dict[str, int] = field(default_factory=dict)
    attempt_heartbeats: dict[str, int] = field(default_factory=dict)
    attempt_log_refs: dict[str, str] = field(default_factory=dict)
    recent_events: list[EventRecord] = field(default_factory=list)
    outcome_counts: dict[str, int] = field(default_factory=dict)


def build_status_snapshot(store: QueueStore) -> StatusSnapshot:
    """Derive all status fields from persisted records without mutating any state."""
    snap = store.reconstruct()
    now = _utc_now()

    # Queue depth by state
    queue_depth: dict[str, int] = {}
    lane_inflight: dict[str, int] = {}
    for item in snap.items.values():
        state_val = item.state.value if isinstance(item.state, WorkItemState) else str(item.state)
        queue_depth[state_val] = queue_depth.get(state_val, 0) + 1
        if item.state in {WorkItemState.DISPATCHED, WorkItemState.RUNNING} and item.lane:
            lane_inflight[item.lane] = lane_inflight.get(item.lane, 0) + 1

    # Cooldown remaining per lane
    lane_cooldown_remaining: dict[str, int] = {}
    for lane_name, lane_record in snap.lanes.items():
        remaining = cooldown_remaining_seconds(lane_record, now=now)
        if remaining > 0:
            lane_cooldown_remaining[lane_name] = remaining

    # Heartbeat ages and log refs from attempt records
    attempt_heartbeats: dict[str, int] = {}
    attempt_log_refs: dict[str, str] = {}
    for attempt_id, attempt_record in snap.attempts.items():
        metadata = attempt_record.metadata or {}
        heartbeat_at = metadata.get("heartbeat_at")
        if heartbeat_at:
            try:
                hb_dt = _parse_datetime(str(heartbeat_at))
                age = int((now - hb_dt).total_seconds())
                attempt_heartbeats[attempt_id] = max(0, age)
            except (ValueError, TypeError):
                pass
        log_path = metadata.get("log_path")
        if log_path:
            attempt_log_refs[attempt_id] = str(log_path)

    # Outcome counters from events
    outcome_counts: dict[str, int] = {}
    for event in snap.events:
        if event.event_type == "result":
            rt = event.payload.get("result_type", "unknown")
            outcome_counts[str(rt)] = outcome_counts.get(str(rt), 0) + 1

    # Recent events tail
    recent_events = snap.events[-RECENT_EVENTS_LIMIT:]

    return StatusSnapshot(
        queue_depth=queue_depth,
        lane_inflight=lane_inflight,
        lane_cooldown_remaining=lane_cooldown_remaining,
        attempt_heartbeats=attempt_heartbeats,
        attempt_log_refs=attempt_log_refs,
        recent_events=recent_events,
        outcome_counts=outcome_counts,
    )
