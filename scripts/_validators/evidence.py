from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from .common import CheckResult, ValidationContext, load_schema, parse_yaml_like_file, validate_schema


def normalize_task_id(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if text.isdigit():
        return f"T{text}"
    upper = text.upper()
    if upper.startswith("T") and upper[1:].isdigit():
        return f"T{upper[1:]}"
    return text


def evidence_path_for_task(spec_dir: Path, task_id: str, explicit_file: Optional[str] = None) -> Path:
    if explicit_file:
        return spec_dir / explicit_file
    normalized = normalize_task_id(task_id)
    suffix = normalized[1:] if normalized.startswith("T") else normalized
    return spec_dir / "evidence" / f"task-{suffix}.yaml"


def validate_evidence_data(data: dict[str, Any], source_name: str) -> list[str]:
    errors: list[str] = []
    normalized_task_id = normalize_task_id(data.get("task_id"))
    if not normalized_task_id:
        errors.append(f"{source_name}: task_id must be present")
    entries = data.get("entries") if isinstance(data.get("entries"), list) else []
    seen_steps: set[str] = set()
    for index, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            continue
        step = str(entry.get("step", "")).strip()
        if step in seen_steps:
            errors.append(f"{source_name}: entries[{index}].step duplicates `{step}`")
        seen_steps.add(step)
    return errors


def load_task_evidence(spec_dir: Path, task_id: Any, explicit_file: Optional[str] = None) -> tuple[dict[str, Any], list[str]]:
    normalized_task_id = normalize_task_id(task_id)
    evidence_path = evidence_path_for_task(spec_dir, normalized_task_id, explicit_file)
    relative_path = evidence_path.relative_to(spec_dir) if evidence_path.is_absolute() else evidence_path
    if not evidence_path.exists():
        return {}, [f"{relative_path}: missing evidence file for {normalized_task_id}"]

    data, errors = parse_yaml_like_file(evidence_path)
    if errors:
        return {}, errors

    schema, schema_errors = load_schema("evidence.schema.yaml")
    errors.extend(schema_errors)
    if schema:
        errors.extend(validate_schema(data, schema, str(relative_path)))
    errors.extend(validate_evidence_data(data, str(relative_path)))
    if normalized_task_id and normalize_task_id(data.get("task_id")) != normalized_task_id:
        errors.append(f"{relative_path}: task_id does not match requested task `{normalized_task_id}`")
    return data, errors


def run(context: ValidationContext):
    evidence_dir = context.spec_dir / "evidence"
    if not evidence_dir.is_dir():
        return CheckResult(
            display_name="evidence",
            errors=[],
            skipped=True,
            skip_message=f"evidence directory not found at {evidence_dir}",
        )

    evidence_paths = sorted(evidence_dir.glob("task-*.yaml"))
    if not evidence_paths:
        return CheckResult(
            display_name="evidence",
            errors=[],
            skipped=True,
            skip_message=f"no evidence files found at {evidence_dir}",
        )

    errors: list[str] = []
    validated: list[str] = []
    for evidence_path in evidence_paths:
        task_hint = evidence_path.stem.replace("task-", "")
        _, evidence_errors = load_task_evidence(context.spec_dir, task_hint, str(evidence_path.relative_to(context.spec_dir)))
        errors.extend(evidence_errors)
        if not evidence_errors:
            validated.append(str(evidence_path.relative_to(context.spec_dir)))

    summary = None if errors else f"evidence files valid: {', '.join(validated)}"
    return CheckResult(
        display_name="evidence",
        errors=errors,
        total_checks=len(evidence_paths),
        summary=summary,
    )