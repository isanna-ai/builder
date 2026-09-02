"""The `memory_eval` telemetry artifact: writer + reader + redaction.

A SEPARATE artifact from `workflow-event` (different required fields), so it gets
its own validated, append-only writer rather than being forced through
`record_workflow_event` (whose schema validator rejects unknown artifacts/fields).

Reuses, does not reinvent, the Builder telemetry substrate:
  - sink root: `_telemetry.common.telemetry_root(workspace_root)`
  - day-partitioned, append-only (one file per record, never mutated)
  - YAML dump via `_telemetry.common.dump_yaml`
  - secret / raw-code redaction guard from `_telemetry.common`
  - JSON-Schema validation via `_validators.common` against `memory-eval.schema.yaml`

HARD INVARIANT: imports NO hivemind module; `memory_mode` defaults to `"off"`;
recall/decision counters default to `0`. S3 overwrites those fields later via the
SHARED TELEMETRY CONTRACT — the schema and field names do not change.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from _validators.common import load_schema, parse_yaml_like_file, validate_schema

from .common import (
    RAW_CODE_PATTERN,
    SECRET_PATTERNS,
    dump_yaml,
    telemetry_root,
)

# The SHARED TELEMETRY CONTRACT. Versioned, additive bumps only: the original
# 16-key set was later extended (prior_art_tokens, decisions_distilled,
# decisions_deduped, recall_mode) and by the agent-lift A/B rubric (rubric_score).
# Every added field is OPTIONAL in the schema and DEFAULTED here, so old on-disk
# rows (which omit the newer fields) still validate and round-trip — the field set
# only grows, names never change.
MEMORY_EVAL_FIELDS = (
    "artifact",
    "ts",
    "run_id",
    "spec_id",
    "phase",
    "lane",
    "memory_mode",
    "plan_tokens_in",
    "plan_tokens_out",
    "plan_wall_ms",
    "recall_calls",
    "recall_hits",
    "recall_latency_ms",
    "decisions_reused",
    "decisions_written",
    "prior_art_tokens",
    "decisions_distilled",
    "decisions_deduped",
    "rubric_score",
    "recall_mode",
    "spec_outcome",
)

_SCHEMA_FILE = "memory-eval.schema.yaml"

# Fields the schema types as `string`. Any YAML reader (real PyYAML is YAML 1.1)
# may reparse an unquoted on-disk value into a non-string (e.g. `off` -> bool
# False, an ISO `...Z` timestamp -> datetime). The writer now quotes these, but
# we still coerce on read so legacy/unquoted files round-trip faithfully.
_STRING_FIELDS = (
    "artifact",
    "run_id",
    "spec_id",
    "phase",
    "lane",
    "memory_mode",
    "recall_mode",
    "spec_outcome",
)


def _utc_z() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_memory_eval(
    *,
    run_id: str,
    spec_id: str,
    lane: str,
    plan_tokens_in: int,
    plan_tokens_out: int,
    plan_wall_ms: int,
    spec_outcome: str,
    memory_mode: str = "off",
    recall_calls: int = 0,
    recall_hits: int = 0,
    recall_latency_ms: int = 0,
    decisions_reused: int = 0,
    decisions_written: int = 0,
    prior_art_tokens: int = 0,
    decisions_distilled: int = 0,
    decisions_deduped: int = 0,
    recall_mode: str = "push",
    rubric_score: int = 0,
    ts: str | None = None,
) -> dict[str, Any]:
    """Build a `memory_eval` record. Defaults `memory_mode='off'` and all five
    recall/decision counters to `0` (the baseline arm); S3 overwrites those.

    The extended memory fields (`prior_art_tokens`, `decisions_distilled`,
    `decisions_deduped`, `recall_mode`) are all DEFAULTED so existing callers and
    today's behavior are unchanged: an unset budget/distill/supersede world emits
    `0/0/0` and `recall_mode="push"`.

    `rubric_score` (agent-lift A/B) is the arm-blind LLM-judge plan-adherence score
    stored as the 0.0-10.0 rubric mean times 10 — an integer in [0,100] (e.g. a 7.5
    mean -> 75). DEFAULTED to `0`: every non-A/B caller omits it and emits `0`, with
    no behavior change. Only the A/B harness stamps a real value post-run."""
    return {
        "artifact": "memory_eval",
        "ts": ts or _utc_z(),
        "run_id": str(run_id),
        "spec_id": str(spec_id),
        "phase": "4-plan",
        "lane": str(lane),
        "memory_mode": str(memory_mode),
        "plan_tokens_in": int(plan_tokens_in),
        "plan_tokens_out": int(plan_tokens_out),
        "plan_wall_ms": int(plan_wall_ms),
        "recall_calls": int(recall_calls),
        "recall_hits": int(recall_hits),
        "recall_latency_ms": int(recall_latency_ms),
        "decisions_reused": int(decisions_reused),
        "decisions_written": int(decisions_written),
        "prior_art_tokens": int(prior_art_tokens),
        "decisions_distilled": int(decisions_distilled),
        "decisions_deduped": int(decisions_deduped),
        "rubric_score": int(rubric_score),
        "recall_mode": str(recall_mode),
        "spec_outcome": str(spec_outcome),
    }


def _validate_record(record: dict[str, Any], source_name: str) -> None:
    schema, schema_errors = load_schema(_SCHEMA_FILE)
    if schema_errors:
        raise ValueError("; ".join(schema_errors))
    errors = validate_schema(record, schema, source_name)
    if errors:
        raise ValueError("; ".join(errors))


def _reject_secrets(record: dict[str, Any], source_name: str) -> None:
    """The sink stores only the bounded fields above — no free-form payloads.
    Reject any record whose string fields carry a secret or a fenced code block."""
    for key, value in record.items():
        if not isinstance(value, str):
            continue
        if RAW_CODE_PATTERN.search(value):
            raise ValueError(f"{source_name}: field `{key}` contains a fenced code block")
        for label, pattern in SECRET_PATTERNS:
            if pattern.search(value):
                raise ValueError(f"{source_name}: field `{key}` contains a secret ({label})")


def _partition_dir(workspace_root: Path, ts: str) -> Path:
    day = str(ts)[:10]
    return telemetry_root(workspace_root) / "events" / "memory-eval" / day


def _sanitize_id(value: Any) -> str:
    text = str(value)
    return "".join(ch if (ch.isalnum() or ch in "._-") else "_" for ch in text) or "x"


def append_memory_eval(workspace_root: Path, record: dict[str, Any]) -> Path:
    """Validate, redact-guard, then append a `memory_eval` record. Append-only:
    never overwrites an existing file (suffixes a counter on collision)."""
    source_name = f"MEVAL-{record.get('run_id', '?')}-{record.get('spec_id', '?')}"
    _validate_record(record, source_name)
    _reject_secrets(record, source_name)

    target_dir = _partition_dir(workspace_root, str(record.get("ts", "")))
    target_dir.mkdir(parents=True, exist_ok=True)
    base = f"MEVAL-{_sanitize_id(record.get('run_id'))}-{_sanitize_id(record.get('spec_id'))}"
    target_path = target_dir / f"{base}.yaml"
    counter = 1
    while target_path.exists():
        target_path = target_dir / f"{base}-{counter}.yaml"
        counter += 1
    target_path.write_text(dump_yaml(record), encoding="utf-8")
    return target_path


def _coerce_ts(value: Any) -> Any:
    """Map a datetime that YAML 1.1 produced from an unquoted `...Z` back to the
    canonical `%Y-%m-%dT%H:%M:%SZ` ISO string the schema requires."""
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%dT%H:%M:%SZ")
    if isinstance(value, date):
        return value.strftime("%Y-%m-%d")
    return value


def _coerce_string_fields(data: dict[str, Any]) -> dict[str, Any]:
    """Coerce schema-string fields back to `str` after YAML reparse. The notable
    cases: `memory_mode: off` -> bool False -> "off", and a datetime `ts` -> ISO
    string. Booleans map back to their YAML 1.1 source word so enum membership
    (e.g. memory_mode "off"/"on") survives a quote-less round trip."""
    coerced = dict(data)
    if "ts" in coerced:
        coerced["ts"] = _coerce_ts(coerced["ts"])
    for field in _STRING_FIELDS:
        if field not in coerced:
            continue
        value = coerced[field]
        if value is True:
            coerced[field] = "on"
        elif value is False:
            coerced[field] = "off"
        elif value is None:
            coerced[field] = ""
        elif not isinstance(value, str):
            coerced[field] = str(value)
    return coerced


def load_memory_evals(workspace_root: Path) -> list[dict[str, Any]]:
    """Read + re-validate every `memory_eval` record in the sink partition."""
    partition = telemetry_root(workspace_root) / "events" / "memory-eval"
    if not partition.is_dir():
        return []
    rows: list[dict[str, Any]] = []
    for path in sorted(partition.rglob("*.yaml")):
        data, parse_errors = parse_yaml_like_file(path)
        if parse_errors:
            raise ValueError("; ".join(parse_errors))
        data = _coerce_string_fields(data)
        _validate_record(data, path.name)
        rows.append(data)
    return rows
