from __future__ import annotations

import re
from datetime import date, datetime, timezone
from pathlib import Path

from _dispatch_runtime.paths import runtime_dir
from typing import Any

from _validators.common import load_schema, parse_yaml_like_file, validate_schema


def read_yaml_mapping(path: Path) -> tuple[dict[str, Any], list[str]]:
    return parse_yaml_like_file(path)


def load_workflow_event_schema() -> tuple[dict[str, Any], list[str]]:
    return load_schema("workflow-event.schema.yaml")


def validate_workflow_event_data(data: dict[str, Any], source_name: str) -> list[str]:
    errors: list[str] = []

    intent_summary = str(data.get("intent_summary", "")).strip()
    if len(intent_summary) > 200:
        errors.append(f"{source_name}: intent_summary must be <= 200 characters")

    capture_source = str(data.get("capture_source", "")).strip()
    numeric_fields = ["input_tokens", "output_tokens", "total_tokens", "latency_ms", "tokens_per_second"]
    if capture_source == "unavailable":
        for field_name in numeric_fields:
            if field_name in data and str(data.get(field_name)).strip() != "":
                errors.append(f"{source_name}: `{field_name}` must be omitted when capture_source is `unavailable`")

    if capture_source == "runtime_measured":
        present = [field_name for field_name in numeric_fields if field_name in data and str(data.get(field_name)).strip() != ""]
        if present and "input_tokens" in data and "output_tokens" in data and "total_tokens" in data:
            try:
                input_tokens = int(data["input_tokens"])
                output_tokens = int(data["output_tokens"])
                total_tokens = int(data["total_tokens"])
            except (TypeError, ValueError):
                pass
            else:
                if total_tokens != input_tokens + output_tokens:
                    errors.append(f"{source_name}: total_tokens must equal input_tokens + output_tokens")

    redaction = data.get("redaction") if isinstance(data.get("redaction"), dict) else {}
    if redaction and redaction.get("sanitized") is not True:
        errors.append(f"{source_name}: redaction.sanitized must be true")

    return errors


def normalize_workflow_event_payload(data: dict[str, Any]) -> dict[str, Any]:
    normalized: dict[str, Any] = {}
    integer_fields = {"input_tokens", "output_tokens", "total_tokens", "latency_ms"}
    number_fields = {"tokens_per_second"}
    for key, value in data.items():
        if isinstance(value, datetime):
            normalized[key] = value.strftime("%Y-%m-%dT%H:%M:%SZ")
        elif isinstance(value, date):
            normalized[key] = value.strftime("%Y-%m-%d")
        elif key in integer_fields and isinstance(value, str) and re.fullmatch(r"-?[0-9]+", value.strip()):
            normalized[key] = int(value.strip())
        elif key in number_fields and isinstance(value, str) and re.fullmatch(r"-?[0-9]+(?:\.[0-9]+)?", value.strip()):
            normalized[key] = float(value.strip())
        else:
            normalized[key] = value
    return normalized


SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("openai_api_key", re.compile(r"sk-[A-Za-z0-9_-]{16,}")),
    ("anthropic_api_key", re.compile(r"sk-ant-[A-Za-z0-9_-]{16,}")),
    ("aws_access_key", re.compile(r"AKIA[0-9A-Z]{12,}")),
    ("github_token", re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}")),
    ("bearer_token", re.compile(r"(?i)bearer\s+[A-Za-z0-9._-]{16,}")),
    ("private_key_block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("password_assignment", re.compile(r"(?i)password\s*[:=]\s*\S+")),
)

RAW_CODE_PATTERN = re.compile(r"```")
RAW_PROMPT_KEYS = ("raw_prompt", "raw_user_prompt", "raw_chat", "transcript", "raw_code")


def sanitize_telemetry_payload(data: dict[str, Any]) -> dict[str, Any]:
    sanitized = dict(data)
    redacted_fields: list[str] = []

    intent_summary = str(sanitized.get("intent_summary", "")).strip()
    if len(intent_summary) > 200:
        intent_summary = intent_summary[:200]
        redacted_fields.append("intent_summary:truncated")
    if RAW_CODE_PATTERN.search(intent_summary):
        intent_summary = RAW_CODE_PATTERN.sub("[code-block-redacted]", intent_summary)
        redacted_fields.append("intent_summary:raw_code")
    for label, pattern in SECRET_PATTERNS:
        if pattern.search(intent_summary):
            intent_summary = pattern.sub("[redacted]", intent_summary)
            redacted_fields.append(f"intent_summary:{label}")
    sanitized["intent_summary"] = intent_summary

    outcome_detail = str(sanitized.get("outcome_detail", "")).strip() if "outcome_detail" in sanitized else ""
    if outcome_detail:
        if RAW_CODE_PATTERN.search(outcome_detail):
            outcome_detail = RAW_CODE_PATTERN.sub("[code-block-redacted]", outcome_detail)
            redacted_fields.append("outcome_detail:raw_code")
        for label, pattern in SECRET_PATTERNS:
            if pattern.search(outcome_detail):
                outcome_detail = pattern.sub("[redacted]", outcome_detail)
                redacted_fields.append(f"outcome_detail:{label}")
        sanitized["outcome_detail"] = outcome_detail

    for raw_key in RAW_PROMPT_KEYS:
        if raw_key in sanitized:
            sanitized.pop(raw_key, None)
            redacted_fields.append(f"{raw_key}:dropped")

    redaction = sanitized.get("redaction") if isinstance(sanitized.get("redaction"), dict) else {}
    existing_fields = list(redaction.get("fields", [])) if isinstance(redaction.get("fields"), list) else []
    sanitized["redaction"] = {
        "sanitized": True,
        "fields": existing_fields + redacted_fields,
    }
    return sanitized


def apply_retention_policy(workspace_root: Path, *, max_age_days: int = 90, now: datetime | None = None) -> list[Path]:
    """Delete event files older than max_age_days. Returns list of removed paths.

    Retention is applied to the per-day event directories under
    .builder/telemetry/events/<YYYY-MM-DD>/. Aggregated reports are kept.
    """
    events_dir = telemetry_root(workspace_root) / "events"
    if not events_dir.is_dir():
        return []
    cutoff = (now or datetime.now(timezone.utc)).date()
    removed: list[Path] = []
    for day_dir in sorted(events_dir.iterdir()):
        if not day_dir.is_dir():
            continue
        try:
            day = datetime.strptime(day_dir.name, "%Y-%m-%d").date()
        except ValueError:
            continue
        age = (cutoff - day).days
        if age > max_age_days:
            for event_file in sorted(day_dir.glob("*.yaml")):
                event_file.unlink()
                removed.append(event_file)
            try:
                day_dir.rmdir()
            except OSError:
                pass
    return removed


def telemetry_root(workspace_root: Path) -> Path:
    return runtime_dir(workspace_root) / "telemetry"


def event_store_dir(workspace_root: Path, recorded_at: str) -> Path:
    day = datetime.strptime(recorded_at, "%Y-%m-%dT%H:%M:%SZ").strftime("%Y-%m-%d")
    return telemetry_root(workspace_root) / "events" / day


def dump_yaml(data: Any) -> str:
    try:
        from _yaml import yaml  # type: ignore

        return yaml.safe_dump(data, sort_keys=False)
    except ImportError:
        return _dump_yaml_fallback(data)


def _dump_yaml_fallback(data: Any, indent: int = 0) -> str:
    prefix = " " * indent
    if isinstance(data, dict):
        lines: list[str] = []
        for key, value in data.items():
            if isinstance(value, (dict, list)):
                lines.append(f"{prefix}{key}:")
                lines.append(_dump_yaml_fallback(value, indent + 2).rstrip())
            else:
                lines.append(f"{prefix}{key}: {_format_scalar(value)}")
        return "\n".join(lines) + "\n"
    if isinstance(data, list):
        lines = []
        for item in data:
            if isinstance(item, dict):
                item_lines = _dump_mapping_list_item(item, indent)
                lines.extend(item_lines)
            elif isinstance(item, list):
                lines.append(f"{prefix}-")
                lines.append(_dump_yaml_fallback(item, indent + 2).rstrip())
            else:
                lines.append(f"{prefix}- {_format_scalar(item)}")
        return "\n".join(lines) + "\n"
    return f"{prefix}{_format_scalar(data)}\n"


def _dump_mapping_list_item(data: dict[str, Any], indent: int) -> list[str]:
    prefix = " " * indent
    if not data:
        return [f"{prefix}- {{}}"]

    entries = list(data.items())
    first_key, first_value = entries[0]
    lines: list[str] = []
    if isinstance(first_value, (dict, list)):
        lines.append(f"{prefix}- {first_key}:")
        lines.append(_dump_yaml_fallback(first_value, indent + 4).rstrip())
    else:
        lines.append(f"{prefix}- {first_key}: {_format_scalar(first_value)}")

    for key, value in entries[1:]:
        if isinstance(value, (dict, list)):
            lines.append(f"{' ' * (indent + 2)}{key}:")
            lines.append(_dump_yaml_fallback(value, indent + 4).rstrip())
        else:
            lines.append(f"{' ' * (indent + 2)}{key}: {_format_scalar(value)}")
    return lines


# YAML 1.1 implicit-typing words/patterns. A *string* whose value is one of
# these would be reparsed by real PyYAML (6.x is YAML 1.1) as a bool/null/number/
# timestamp, breaking the write->read string fidelity. The fallback writer must
# quote such strings so the value round-trips as a str through EITHER engine.
_YAML11_BOOL_WORDS = frozenset(
    w.lower()
    for w in ("y", "yes", "n", "no", "true", "false", "on", "off")
)
_YAML11_NULL_WORDS = frozenset(("", "null", "~", "none"))
_YAML11_NUMBER_RE = re.compile(r"[-+]?(\.[0-9]+|[0-9][0-9_]*(\.[0-9]*)?)([eE][-+]?[0-9]+)?")
# Matches the ISO `...T...Z` / `... HH:MM:SS` shapes PyYAML reads back as datetime.
_YAML11_TIMESTAMP_RE = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}([Tt ][0-9]{2}:[0-9]{2}:[0-9]{2}.*)?"
)


def _needs_quoting(text: str) -> bool:
    if text == "":
        return True
    if text.lower() in _YAML11_BOOL_WORDS:
        return True
    if text.lower() in _YAML11_NULL_WORDS:
        return True
    if _YAML11_NUMBER_RE.fullmatch(text):
        return True
    if _YAML11_TIMESTAMP_RE.fullmatch(text):
        return True
    if any(ch in text for ch in [":", "#", "[", "]", "{", "}", ","]):
        return True
    if text.strip() != text:
        return True
    return False


def _format_scalar(value: Any) -> str:
    if value is True:
        return "true"
    if value is False:
        return "false"
    if value is None:
        return '""'
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value)
    if _needs_quoting(text):
        return f'"{text}"'
    return text
