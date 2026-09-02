"""Task 1 (S7) — RED-first tests for the run-ledger write side.

Mirrors the fake-client pattern in test_memory_hook.py: a FakeClient records
(tool, args) calls and returns canned results. Covers:

  * bootstrap issues exactly two hive_create_table calls (spec_runs, verify_runs)
    with the documented columns and NO id/org_id column,
  * a plan-phase write inserts ONE spec_runs row with the documented keys,
  * a verify-phase write inserts the spec_runs row PLUS one verify_runs row per
    learned note,
  * env-gate-off (client=None + no HIVEMIND_* env) is a no-op returning False,
  * a client that raises on hive_insert is swallowed (returns False, no propagate).

Uses constructor-injected fake clients + a SimpleNamespace work/decision (the
repo's local pytest runner supports zero-arg / tmp_path tests; no monkeypatch
fixture, so env is saved/restored manually as in test_memory_hook).
"""

from __future__ import annotations

import os
from types import SimpleNamespace

from _dispatch_runtime import run_ledger


class FakeClient:
    """Records (tool, args) calls; returns canned hive results."""

    def __init__(self, raise_on=None):
        self.raise_on = set(raise_on or ())
        self.calls: list[tuple[str, dict]] = []

    def call(self, tool, args):
        self.calls.append((tool, args))
        if tool in self.raise_on:
            raise RuntimeError(f"{tool} boom")
        if tool == "bia_create_table":
            return {"success": True, "table": args.get("table"), "idempotent": True}
        if tool == "bia_insert":
            return {"success": True, "row": dict(args.get("data") or {})}
        return {}


def _work(phase="plan", spec_id="spec-x"):
    return SimpleNamespace(spec_id=spec_id, phase=phase)


def _decision(outcome="phase-complete"):
    return SimpleNamespace(outcome=outcome)


def _exec(**over):
    base = {"input_tokens": 10, "output_tokens": 20, "cli_duration_ms": 1234}
    base.update(over)
    return base


def test_ensure_ledger_tables_issues_two_creates_no_reserved_columns():
    fake = FakeClient()
    run_ledger.ensure_ledger_tables(fake)

    creates = [c for c in fake.calls if c[0] == "bia_create_table"]
    assert len(creates) == 2
    # spec_runs first, then verify_runs
    assert creates[0][1]["table"] == "spec_runs"
    assert creates[1][1]["table"] == "verify_runs"

    spec_cols = {c["name"] for c in creates[0][1]["columns"]}
    verify_cols = {c["name"] for c in creates[1][1]["columns"]}
    assert spec_cols == {"spec_id", "phase", "lane", "plan_tokens", "wall_ms",
                         "outcome", "ts", "memory_mode", "recall_hits"}
    assert verify_cols == {"spec_id", "module", "test", "root_cause", "ts"}
    # hivemind injects id/org_id; passing them would collide with reserved cols.
    for cols in (spec_cols, verify_cols):
        assert "id" not in cols
        assert "org_id" not in cols
    # plan_tokens / wall_ms are `integer` (NOT `bigint`): the live hivemind server
    # cannot JSON-serialize a `bigint` column value back through /mcp/message
    # (TypeError: Do not know how to serialize a BigInt -> HTTP 500). int32 is
    # serialization-safe and amply wide for per-phase token/ms values.
    spec_types = {c["name"]: c["type"] for c in creates[0][1]["columns"]}
    assert spec_types["plan_tokens"] == "integer"
    assert spec_types["wall_ms"] == "integer"


def test_write_run_ledger_plan_inserts_one_spec_runs_row():
    fake = FakeClient()
    ok = run_ledger.write_run_ledger(
        _work("plan"), _exec(), "claude-code-cli", _decision("phase-complete"),
        client=fake,
    )
    assert ok is True

    inserts = [c for c in fake.calls if c[0] == "bia_insert"]
    assert len(inserts) == 1
    args = inserts[0][1]
    assert args["table"] == "spec_runs"
    data = args["data"]
    assert set(data) == {"spec_id", "phase", "lane", "plan_tokens", "wall_ms",
                         "outcome", "ts", "memory_mode", "recall_hits"}
    assert data["spec_id"] == "spec-x"
    assert data["phase"] == "plan"
    assert data["lane"] == "claude"
    assert data["plan_tokens"] == 30  # input + output
    assert data["wall_ms"] == 1234
    assert data["outcome"] == "complete"  # phase-complete -> complete
    assert data["ts"].endswith("Z")
    # Tier-1 clock: defaults when caller passes nothing (memory_mode unknown, 0 hits)
    assert data["memory_mode"] == "unknown"
    assert data["recall_hits"] == 0


def test_write_run_ledger_records_tier1_memory_mode_and_recall_hits():
    """Tier-1 forward-transfer clock: memory_mode is normalized (hivemind->on) and
    recall_hits is recorded on the spec_runs row — the OFF arm logs too (the write
    is NOT gated on memory_mode)."""
    fake = FakeClient()
    run_ledger.write_run_ledger(
        _work("verify"), _exec(), "claude-code-cli", _decision("phase-complete"),
        client=fake, memory_mode="hivemind", recall_hits=3,
    )
    data = [c for c in fake.calls if c[0] == "bia_insert"][0][1]["data"]
    assert data["memory_mode"] == "on"  # "hivemind" active-arm label -> "on"
    assert data["recall_hits"] == 3

    # OFF arm still produces a row (never gated on memory_mode).
    fake_off = FakeClient()
    ok = run_ledger.write_run_ledger(
        _work("plan"), _exec(), "claude", _decision("phase-complete"),
        client=fake_off, memory_mode="off", recall_hits=0,
    )
    assert ok is True
    data_off = [c for c in fake_off.calls if c[0] == "bia_insert"][0][1]["data"]
    assert data_off["memory_mode"] == "off"
    assert data_off["recall_hits"] == 0


def test_norm_memory_mode_maps_to_three_valued_factor():
    assert run_ledger._norm_memory_mode("hivemind") == "on"
    assert run_ledger._norm_memory_mode("on") == "on"
    assert run_ledger._norm_memory_mode("off") == "off"
    assert run_ledger._norm_memory_mode("baseline") == "off"
    assert run_ledger._norm_memory_mode(None) == "unknown"
    assert run_ledger._norm_memory_mode("garbage") == "unknown"


def test_write_run_ledger_codex_lane_label():
    fake = FakeClient()
    run_ledger.write_run_ledger(
        _work("implement"), _exec(), "codex-cli", _decision("phase-complete"),
        client=fake,
    )
    data = [c for c in fake.calls if c[0] == "bia_insert"][0][1]["data"]
    assert data["lane"] == "codex"
    assert data["phase"] == "implement"


def test_write_run_ledger_verify_inserts_verify_runs_per_learned_note():
    fake = FakeClient()
    notes = ["finance test_x failed: boom", "billing test_y failed: oops"]
    ok = run_ledger.write_run_ledger(
        _work("verify"), _exec(), "claude", _decision("phase-complete"),
        client=fake, decisions_learned=notes,
    )
    assert ok is True

    inserts = [c for c in fake.calls if c[0] == "bia_insert"]
    spec_inserts = [c for c in inserts if c[1]["table"] == "spec_runs"]
    verify_inserts = [c for c in inserts if c[1]["table"] == "verify_runs"]
    assert len(spec_inserts) == 1
    assert len(verify_inserts) == 2  # one per learned note
    first = verify_inserts[0][1]["data"]
    assert set(first) == {"spec_id", "module", "test", "root_cause", "ts"}
    assert first["spec_id"] == "spec-x"
    assert first["module"] == "spec-x"
    # token = leading "<test>: <reason>" segment
    assert first["test"] == "finance test_x failed"
    assert first["root_cause"] == "finance test_x failed: boom"


def test_write_run_ledger_non_verify_ignores_learned_notes():
    fake = FakeClient()
    run_ledger.write_run_ledger(
        _work("plan"), _exec(), "claude", _decision("phase-complete"),
        client=fake, decisions_learned=["should be ignored on plan"],
    )
    verify_inserts = [c for c in fake.calls if c[0] == "bia_insert" and c[1]["table"] == "verify_runs"]
    assert verify_inserts == []


def test_outcome_mapping():
    assert run_ledger._outcome("phase-complete") == "complete"
    assert run_ledger._outcome("blocked-human") == "blocked"
    assert run_ledger._outcome("stale-escalate") == "stale"
    assert run_ledger._outcome("rate-limit-cooldown") == "rate-limited"
    assert run_ledger._outcome("resume-same-session") == "resumed"
    assert run_ledger._outcome("retry-fresh-session") == "retried"
    assert run_ledger._outcome("something-else") == "something-else"  # passthrough
    assert run_ledger._outcome("") == "unknown"


def test_env_gate_off_is_noop_returns_false():
    saved = {k: os.environ.pop(k, None) for k in ("HIVEMIND_MCP_URL", "HIVEMIND_API_KEY")}
    try:
        ok = run_ledger.write_run_ledger(
            _work("plan"), _exec(), "claude", _decision("phase-complete"),
        )
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v
    assert ok is False


def test_write_run_ledger_swallows_insert_error():
    fake = FakeClient(raise_on={"bia_insert"})
    ok = run_ledger.write_run_ledger(
        _work("plan"), _exec(), "claude", _decision("phase-complete"), client=fake,
    )
    assert ok is False  # swallowed, not raised


def test_write_run_ledger_swallows_create_error():
    fake = FakeClient(raise_on={"bia_create_table"})
    ok = run_ledger.write_run_ledger(
        _work("plan"), _exec(), "claude", _decision("phase-complete"), client=fake,
    )
    assert ok is False
