from __future__ import annotations

from pathlib import Path
from typing import Any

from .common import (
    dump_yaml,
    event_store_dir,
    load_workflow_event_schema,
    normalize_workflow_event_payload,
    read_yaml_mapping,
    sanitize_telemetry_payload,
    validate_workflow_event_data,
)
from _validators.common import validate_schema


def validate_workflow_event(source: Path | dict[str, Any]) -> list[str]:
    if isinstance(source, Path):
        data, parse_errors = read_yaml_mapping(source)
        source_name = source.name
    else:
        data = source
        parse_errors = []
        source_name = "workflow-event"

    errors = list(parse_errors)
    if parse_errors:
        return errors

    data = normalize_workflow_event_payload(data)

    schema, schema_errors = load_workflow_event_schema()
    errors.extend(schema_errors)
    if schema:
        errors.extend(validate_schema(data, schema, source_name))
    errors.extend(validate_workflow_event_data(data, source_name))
    return errors


def record_workflow_event(source: Path | dict[str, Any], workspace_root: Path) -> Path:
    if isinstance(source, Path):
        data, parse_errors = read_yaml_mapping(source)
        if parse_errors:
            raise ValueError("; ".join(parse_errors))
    else:
        data = source

    data = normalize_workflow_event_payload(data)

    errors = validate_workflow_event(data)
    if errors:
        raise ValueError("; ".join(errors))

    sanitized = sanitize_telemetry_payload(data)
    recorded_at = str(sanitized.get("recorded_at", "")).strip()
    event_id = str(sanitized.get("event_id", "")).strip()
    target_dir = event_store_dir(workspace_root, recorded_at)
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / f"{event_id}.yaml"
    target_path.write_text(dump_yaml(sanitized), encoding="utf-8")
    return target_path


def write_phase_completion_event(workspace_root: Path, **event: Any) -> Path:
    payload = _build_event_payload(mode="lifecycle", **event)
    return record_workflow_event(payload, workspace_root)


def write_decision_event(workspace_root: Path, **event: Any) -> Path:
    payload = _build_event_payload(mode="lifecycle", **event)
    return record_workflow_event(payload, workspace_root)


def write_utility_event(workspace_root: Path, **event: Any) -> Path:
    payload = _build_event_payload(mode="utility", **event)
    return record_workflow_event(payload, workspace_root)


def _build_event_payload(*, mode: str, **event: Any) -> dict[str, Any]:
    payload = {
        "artifact": "workflow-event",
        "event_id": str(event.get("event_id", "")).strip(),
        "recorded_at": str(event.get("recorded_at", "")).strip(),
        "command": str(event.get("command", "")).strip(),
        "mode": mode,
        "used_model": str(event.get("used_model", "")).strip(),
        "thinking_effort": str(event.get("thinking_effort", "unknown")).strip() or "unknown",
        "capture_source": str(event.get("capture_source", "unavailable")).strip() or "unavailable",
        "reason_category": str(event.get("reason_category", "phase_progress")).strip() or "phase_progress",
        "intent_summary": str(event.get("intent_summary", "")).strip(),
        "execution_path": str(event.get("execution_path", "normal_phase")).strip() or "normal_phase",
        "artifacts_read": list(event.get("artifacts_read", []) or []),
        "artifacts_written": list(event.get("artifacts_written", []) or []),
        "validation_refs": list(event.get("validation_refs", []) or []),
        "outcome_category": str(event.get("outcome_category", "completed")).strip() or "completed",
        "next_command": str(event.get("next_command", "none")).strip() or "none",
        "redaction": event.get("redaction") if isinstance(event.get("redaction"), dict) else {"sanitized": True, "fields": []},
    }

    for optional_name in (
        "phase",
        "spec",
        "used_model_class",
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "latency_ms",
        "tokens_per_second",
        "outcome_detail",
        "parent_event_id",
    ):
        value = event.get(optional_name)
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        payload[optional_name] = value
    return payload