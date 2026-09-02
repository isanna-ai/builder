from __future__ import annotations

from _validators.common import load_schema, validate_schema


def _valid_record() -> dict:
    return {
        "artifact": "memory_eval",
        "ts": "2026-06-07T12:00:00Z",
        "run_id": "w1",
        "spec_id": "demo",
        "phase": "4-plan",
        "lane": "claude",
        "memory_mode": "off",
        "plan_tokens_in": 10,
        "plan_tokens_out": 20,
        "plan_wall_ms": 1234,
        "recall_calls": 0,
        "recall_hits": 0,
        "recall_latency_ms": 0,
        "decisions_reused": 0,
        "decisions_written": 0,
        "spec_outcome": "unknown",
    }


def test_schema_loads_and_validates():
    schema, load_errors = load_schema("memory-eval.schema.yaml")
    assert not load_errors, load_errors
    assert schema

    valid = _valid_record()
    assert validate_schema(valid, schema, "memory_eval") == []

    missing = _valid_record()
    del missing["plan_tokens_out"]
    assert validate_schema(missing, schema, "memory_eval") != []

    extra = _valid_record()
    extra["foo"] = "bar"
    assert validate_schema(extra, schema, "memory_eval") != []
