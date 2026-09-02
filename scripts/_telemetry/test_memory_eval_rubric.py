"""Contract tests for the agent-lift A/B `rubric_score` field on `memory_eval`.

Asserts the versioned, additive field-set bump: a record WITH `rubric_score`
validates, an OLD record WITHOUT it STILL validates (optional + defaulted), an
out-of-range value is rejected (maximum: 100), and the sink round-trips the new
field. Shim-compatible: bare `test_*`, plain asserts, `tmp_path` only, no
monkeypatch / pytest.raises / classes / marks.
"""

from __future__ import annotations

from pathlib import Path

from _telemetry.memory_eval import (
    MEMORY_EVAL_FIELDS,
    append_memory_eval,
    build_memory_eval,
    load_memory_evals,
)
from _validators.common import load_schema, validate_schema


def _base_kwargs() -> dict:
    return {
        "run_id": "run-1",
        "spec_id": "spec-1",
        "lane": "claude",
        "plan_tokens_in": 100,
        "plan_tokens_out": 200,
        "plan_wall_ms": 1500,
        "spec_outcome": "verified",
    }


def test_rubric_score_in_frozen_field_set():
    # The field-set bump is additive: rubric_score is present, every prior field
    # is preserved, and names never change.
    assert "rubric_score" in MEMORY_EVAL_FIELDS
    for prior in (
        "artifact", "ts", "run_id", "spec_id", "phase", "lane", "memory_mode",
        "plan_tokens_in", "plan_tokens_out", "plan_wall_ms", "recall_calls",
        "recall_hits", "recall_latency_ms", "decisions_reused", "decisions_written",
        "prior_art_tokens", "decisions_distilled", "decisions_deduped", "recall_mode",
        "spec_outcome",
    ):
        assert prior in MEMORY_EVAL_FIELDS


def test_build_defaults_rubric_score_to_zero():
    # An existing caller that omits rubric_score gets the safe default 0.
    record = build_memory_eval(**_base_kwargs())
    assert record["rubric_score"] == 0


def test_build_emits_rubric_score_x10_int():
    # Stored x10 of the 0-10 rubric (7.5 -> 75); coerced to int.
    record = build_memory_eval(rubric_score=75, **_base_kwargs())
    assert record["rubric_score"] == 75
    assert isinstance(record["rubric_score"], int)


def test_record_with_rubric_score_validates():
    schema, schema_errors = load_schema("memory-eval.schema.yaml")
    assert not schema_errors
    record = build_memory_eval(rubric_score=75, **_base_kwargs())
    errors = validate_schema(record, schema, "with-rubric")
    assert errors == []


def test_old_record_without_rubric_score_still_validates():
    # Backward compat: an on-disk record from before the bump omits rubric_score
    # entirely and MUST still validate (optional property, not in `required`).
    schema, schema_errors = load_schema("memory-eval.schema.yaml")
    assert not schema_errors
    record = build_memory_eval(**_base_kwargs())
    del record["rubric_score"]
    errors = validate_schema(record, schema, "legacy-no-rubric")
    assert errors == []


def test_rubric_score_out_of_range_rejected():
    # maximum: 100 — a 999 must be rejected (the run stores x10 of a [0,10] rubric).
    schema, schema_errors = load_schema("memory-eval.schema.yaml")
    assert not schema_errors
    record = build_memory_eval(**_base_kwargs())
    record["rubric_score"] = 999
    errors = validate_schema(record, schema, "over-range")
    assert errors != []


def test_rubric_score_negative_rejected():
    # minimum: 0 — a negative is rejected too.
    schema, schema_errors = load_schema("memory-eval.schema.yaml")
    assert not schema_errors
    record = build_memory_eval(**_base_kwargs())
    record["rubric_score"] = -5
    errors = validate_schema(record, schema, "under-range")
    assert errors != []


def test_sink_round_trips_rubric_score(tmp_path: Path):
    # append -> load preserves rubric_score (it is an int field, not a string field,
    # so _coerce_string_fields leaves it alone).
    record = build_memory_eval(rubric_score=80, **_base_kwargs())
    append_memory_eval(tmp_path, record)
    loaded = load_memory_evals(tmp_path)
    assert len(loaded) == 1
    assert int(loaded[0]["rubric_score"]) == 80


def test_sink_round_trips_record_without_rubric_score(tmp_path: Path):
    # A record missing rubric_score still appends + loads + re-validates cleanly.
    record = build_memory_eval(**_base_kwargs())
    del record["rubric_score"]
    append_memory_eval(tmp_path, record)
    loaded = load_memory_evals(tmp_path)
    assert len(loaded) == 1
    assert "rubric_score" not in loaded[0]
