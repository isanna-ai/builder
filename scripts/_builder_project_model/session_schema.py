from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .common import CANONICAL_PROVIDERS, ValidationError, ValidationIssue, load_yaml_mapping, reject_unknown_keys, require_schema_version

SESSION_STATES = ("starting", "active", "reaping", "closed")
SESSION_TRANSITIONS = {
    "starting": {"active"},
    "active": {"reaping"},
    "reaping": {"closed"},
    "closed": set(),
}


@dataclass(frozen=True)
class SessionRecord:
    data: dict[str, Any]


def parse_session_record(path: Path) -> SessionRecord:
    data = load_yaml_mapping(path)
    issues = require_schema_version(data, location=str(path))
    issues.extend(reject_unknown_keys(
        data,
        {
            "schema_version",
            "slot_id",
            "provider",
            "state",
            "daemon_instance_id",
            "owner_pid",
            "owner_pid_start_ticks",
            "repo_id",
            "queue_root",
            "work_id",
            "attempt_id",
            "lane",
            "project_attribution",
            "release_name",
            "reserved_at",
            "updated_at",
            "pgid",
            "pgid_leader_start_ticks",
            "executable",
            "command_digest",
            "previous_state",
        },
        location=str(path),
    ))
    if data.get("provider") not in CANONICAL_PROVIDERS:
        issues.append(ValidationIssue(str(path), f"provider must be one of {', '.join(CANONICAL_PROVIDERS)}"))
    state = data.get("state")
    if state not in SESSION_STATES:
        issues.append(ValidationIssue(str(path), f"state must be one of {', '.join(SESSION_STATES)}"))
    previous = data.get("previous_state")
    if previous is not None:
        if previous not in SESSION_STATES:
            issues.append(ValidationIssue(str(path), f"previous_state must be one of {', '.join(SESSION_STATES)}"))
        elif state in SESSION_STATES and state not in SESSION_TRANSITIONS.get(previous, set()):
            issues.append(ValidationIssue(str(path), f"invalid transition {previous!r} -> {state!r}"))
    if state == "starting":
        if data.get("pgid") is not None:
            issues.append(ValidationIssue(str(path), "starting session must not have pgid"))
    else:
        if not isinstance(data.get("pgid"), int) or data.get("pgid", 0) <= 1:
            issues.append(ValidationIssue(str(path), "non-starting session must have pgid > 1"))
    if issues:
        raise ValidationError(issues)
    return SessionRecord(data)
