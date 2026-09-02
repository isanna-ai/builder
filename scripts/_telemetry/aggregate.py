from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from _validators.common import load_schema, parse_yaml_like_file, validate_schema

from .common import dump_yaml, telemetry_root
from .record import validate_workflow_event


def load_workflow_events(workspace_root: Path) -> list[dict[str, Any]]:
    events_dir = telemetry_root(workspace_root) / "events"
    if not events_dir.is_dir():
        return []

    events: list[dict[str, Any]] = []
    for event_path in sorted(events_dir.rglob("*.yaml")):
        data, parse_errors = parse_yaml_like_file(event_path)
        if parse_errors:
            raise ValueError("; ".join(parse_errors))
        errors = validate_workflow_event(data)
        if errors:
            raise ValueError("; ".join(errors))
        events.append(data)
    return events


def aggregate_workflow_events(workspace_root: Path) -> dict[str, Any]:
    events = load_workflow_events(workspace_root)

    command_usage = Counter(str(event.get("command", "")).strip() for event in events)
    outcome_counts = Counter(str(event.get("outcome_category", "")).strip() for event in events)
    model_outcome_matrix = Counter(
        (str(event.get("used_model", "")).strip(), str(event.get("outcome_category", "")).strip())
        for event in events
    )
    thinking_effort_matrix = Counter(
        (str(event.get("thinking_effort", "")).strip(), str(event.get("outcome_category", "")).strip())
        for event in events
    )
    utility_adoption = Counter(
        str(event.get("command", "")).strip()
        for event in events
        if str(event.get("mode", "")).strip() == "utility"
    )

    validator_failures = sum(1 for event in events if str(event.get("outcome_category", "")).strip() == "validator_failed")
    another_pass_loops = sum(1 for event in events if str(event.get("execution_path", "")).strip() == "another_pass")
    verified_events = sum(
        1
        for event in events
        if str(event.get("command", "")).strip() in {"/sp-6-verify", "/isanna-6-verify"}
        and str(event.get("outcome_category", "")).strip() in {"completed", "completed_with_tasks"}
    )
    archived_events = sum(
        1
        for event in events
        if str(event.get("command", "")).strip() in {"/sp-archive", "/isanna-archive"}
        and str(event.get("outcome_category", "")).strip() == "completed"
    )

    report = {
        "artifact": "telemetry-report",
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "scope": "workspace",
        "event_count": len(events),
        "summaries": {
            "command_usage": _counter_rows(command_usage, "command"),
            "outcome_counts": _counter_rows(outcome_counts, "outcome_category"),
            "model_outcome_matrix": _matrix_rows(model_outcome_matrix, ("used_model", "outcome_category")),
            "thinking_effort_matrix": _matrix_rows(thinking_effort_matrix, ("thinking_effort", "outcome_category")),
            "validator_failures": validator_failures,
            "another_pass_loops": another_pass_loops,
            "archive_funnel": {
                "verified_events": verified_events,
                "archived_events": archived_events,
            },
            "utility_adoption": _counter_rows(utility_adoption, "command"),
        },
        "recommendations": build_recommendations(
            validator_failures=validator_failures,
            another_pass_loops=another_pass_loops,
            archived_events=archived_events,
            verified_events=verified_events,
        ),
    }
    validate_telemetry_report(report)
    return report


def build_recommendations(*, validator_failures: int, another_pass_loops: int, archived_events: int, verified_events: int) -> list[str]:
    recommendations: list[str] = []
    if validator_failures:
        recommendations.append("Tighten Phase 4 and /isanna-validate guidance so malformed task artifacts fail earlier.")
    if another_pass_loops:
        recommendations.append("Review specify/design/review prompts for recurring ambiguity that is causing Another pass loops.")
    if verified_events > archived_events:
        recommendations.append("Inspect the post-verify archive funnel; verified specs are not always reaching /isanna-archive.")
    if not recommendations:
        recommendations.append("Telemetry looks healthy; continue collecting forward-only events and review again after more samples.")
    return recommendations


def write_telemetry_report(workspace_root: Path) -> Path:
    report = aggregate_workflow_events(workspace_root)
    report_path = telemetry_root(workspace_root) / "reports" / "telemetry-report.yaml"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(dump_yaml(report), encoding="utf-8")
    return report_path


def load_telemetry_report(path: Path) -> dict[str, Any]:
    data, parse_errors = parse_yaml_like_file(path)
    if parse_errors:
        raise ValueError("; ".join(parse_errors))
    validate_telemetry_report(data)
    return data


def validate_telemetry_report(data: dict[str, Any]) -> None:
    schema, schema_errors = load_schema("telemetry-report.schema.yaml")
    if schema_errors:
        raise ValueError("; ".join(schema_errors))
    errors = validate_schema(data, schema, "telemetry-report.yaml")
    if errors:
        raise ValueError("; ".join(errors))


def _counter_rows(counter: Counter[str], field_name: str) -> list[dict[str, Any]]:
    return [
        {field_name: key, "count": count}
        for key, count in sorted(counter.items(), key=lambda item: (-item[1], item[0]))
        if key
    ]


def _matrix_rows(counter: Counter[tuple[str, str]], field_names: tuple[str, str]) -> list[dict[str, Any]]:
    first_name, second_name = field_names
    return [
        {first_name: key[0], second_name: key[1], "count": count}
        for key, count in sorted(counter.items(), key=lambda item: (-item[1], item[0][0], item[0][1]))
        if key[0] and key[1]
    ]