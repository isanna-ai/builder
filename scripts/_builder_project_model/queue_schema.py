from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .common import ValidationError, ValidationIssue, load_yaml_mapping, reject_unknown_keys

WORK_STATES = {
    "queued",
    "dispatched",
    "running",
    "succeeded",
    "failed",
    "blocked_human",
    "blocked_dep",
    "cancelled",
}
EVENT_TYPES = {
    "enqueue",
    "state_transition",
    "attempt_recorded",
    "lease_acquired",
    "process_start",
    "heartbeat",
    "result",
    "cooldown_open",
    "human_block",
    "custom_event",
}


@dataclass(frozen=True)
class QueueRecord:
    kind: str
    data: dict[str, Any]


def parse_work_item_record(path: Path) -> QueueRecord:
    data = load_yaml_mapping(path)
    issues = reject_unknown_keys(
        data,
        {"id", "state", "priority", "attempt", "max_attempts", "scheduled_after", "lane", "lease", "task_ref", "created_at", "updated_at"},
        location=str(path),
    )
    if data.get("state") not in WORK_STATES:
        issues.append(ValidationIssue(str(path), f"unknown work-item state {data.get('state')!r}"))
    if not isinstance(data.get("lease"), dict):
        issues.append(ValidationIssue(str(path), "lease must be a mapping"))
    if not isinstance(data.get("task_ref"), dict):
        issues.append(ValidationIssue(str(path), "task_ref must be a mapping"))
    if issues:
        raise ValidationError(issues)
    return QueueRecord("item", data)


def parse_attempt_record(path: Path) -> QueueRecord:
    data = load_yaml_mapping(path)
    issues = reject_unknown_keys(data, {"attempt_id", "work_id", "lane", "metadata", "created_at"}, location=str(path))
    if not isinstance(data.get("metadata"), dict):
        issues.append(ValidationIssue(str(path), "metadata must be a mapping"))
    if issues:
        raise ValidationError(issues)
    return QueueRecord("attempt", data)


def parse_lane_record(path: Path) -> QueueRecord:
    data = load_yaml_mapping(path)
    issues = reject_unknown_keys(data, {"lane", "cooldown_until", "reason", "updated_at"}, location=str(path))
    if issues:
        raise ValidationError(issues)
    return QueueRecord("lane", data)


def parse_event_record(path: Path) -> QueueRecord:
    data = load_yaml_mapping(path)
    issues = reject_unknown_keys(data, {"event_id", "work_id", "event_type", "payload", "created_at"}, location=str(path))
    if data.get("event_type") not in EVENT_TYPES:
        issues.append(ValidationIssue(str(path), f"unknown event_type {data.get('event_type')!r}"))
    if not isinstance(data.get("payload"), dict):
        issues.append(ValidationIssue(str(path), "payload must be a mapping"))
    if issues:
        raise ValidationError(issues)
    return QueueRecord("event", data)
