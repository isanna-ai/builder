from __future__ import annotations

from _telemetry.memory_eval import (
    append_memory_eval,
    build_memory_eval,
    load_memory_evals,
)


def test_build_memory_eval_defaults():
    rec = build_memory_eval(
        run_id="w1",
        spec_id="demo",
        lane="claude",
        plan_tokens_in=10,
        plan_tokens_out=20,
        plan_wall_ms=1234,
        spec_outcome="unknown",
    )
    assert rec["artifact"] == "memory_eval"
    assert rec["phase"] == "4-plan"
    assert rec["memory_mode"] == "off"
    assert rec["recall_calls"] == 0
    assert rec["recall_hits"] == 0
    assert rec["recall_latency_ms"] == 0
    assert rec["decisions_reused"] == 0
    assert rec["decisions_written"] == 0
    assert rec["ts"].endswith("Z")


def test_append_and_load_round_trips_one_record(tmp_path):
    rec = build_memory_eval(
        run_id="w1",
        spec_id="demo",
        lane="claude",
        plan_tokens_in=10,
        plan_tokens_out=20,
        plan_wall_ms=1234,
        spec_outcome="unknown",
    )
    append_memory_eval(tmp_path, rec)
    rows = load_memory_evals(tmp_path)
    assert len(rows) == 1
    assert rows[0]["spec_id"] == "demo"
    assert rows[0]["memory_mode"] == "off"
    assert rows[0]["plan_tokens_out"] == 20
    assert rows[0]["recall_calls"] == 0


def test_append_never_overwrites(tmp_path):
    rec = build_memory_eval(
        run_id="w1",
        spec_id="demo",
        lane="claude",
        plan_tokens_in=1,
        plan_tokens_out=2,
        plan_wall_ms=3,
        spec_outcome="unknown",
    )
    p1 = append_memory_eval(tmp_path, rec)
    p2 = append_memory_eval(tmp_path, rec)
    assert p1 != p2
    assert len(load_memory_evals(tmp_path)) == 2


def test_missing_field_raises(tmp_path):
    rec = build_memory_eval(
        run_id="w1",
        spec_id="demo",
        lane="claude",
        plan_tokens_in=10,
        plan_tokens_out=20,
        plan_wall_ms=1234,
        spec_outcome="unknown",
    )
    del rec["plan_tokens_out"]
    try:
        append_memory_eval(tmp_path, rec)
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for missing plan_tokens_out")


def test_secret_in_field_is_rejected(tmp_path):
    rec = build_memory_eval(
        run_id="w1",
        spec_id="sk-ant-AAAAAAAAAAAAAAAAAAAA",  # publish-ok: deliberate secret-rejection fixture
        lane="claude",
        plan_tokens_in=10,
        plan_tokens_out=20,
        plan_wall_ms=1234,
        spec_outcome="unknown",
    )
    try:
        append_memory_eval(tmp_path, rec)
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for a secret in spec_id")
