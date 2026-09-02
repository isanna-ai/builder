"""Task 10 — emit memory_eval from the dispatcher plan/verify path.

RED-first: drive finalize_turn for a plan work item with an injected emitter and
assert one memory_eval is appended carrying run_id/spec_id/lane/recall-stats,
decisions_written==0 on plan; a verify turn with post_validation.passed writes
decisions and reports the count; a failed verify writes nothing.

Uses constructor/argument injection (no monkeypatch fixture in the local runner).
"""

from __future__ import annotations

import os
from pathlib import Path

from _dispatch_runtime import memory_hook, phase_runtime, run_ledger
from _dispatch_runtime.lane_common import SessionState, Work, finalize_turn
from _dispatch_runtime.phase_runtime import (
    capture_spec_snapshot,
    validate_phase_completion,
)


def _work(tmp_path: Path, phase: str) -> Work:
    specs_dir = tmp_path / ".builder" / "specs"
    (specs_dir / "demo").mkdir(parents=True, exist_ok=True)
    queue_root = tmp_path / ".builder" / "dispatch-queue"
    return Work(
        work_id="w-77",
        spec_id="demo",
        phase=phase,
        project_dir=tmp_path,
        specs_dir=specs_dir,
        runner_task_ref=None,
        capability_class=None,
        queue_root=queue_root,
        log_path=queue_root / "queue" / "attempts" / "a.log",
    )


def _exec(**over) -> dict:
    base = {
        "status": "interrupted",
        "stdout": "",
        "stderr": "",
        "returncode": 0,
        "session_id": "s",
        "input_tokens": 100,
        "output_tokens": 200,
        "cli_duration_ms": 1500,
    }
    base.update(over)
    return base


def _disable_ledger():
    """No-op the run-ledger so the memory_eval-focused tests below never hit the
    live hivemind wire (the in-container env gate is set). Returns the saved
    callable; restore it in a finally. The dedicated ledger-wiring tests further
    down patch their own capture and so do not need this."""
    saved = run_ledger.write_run_ledger
    run_ledger.write_run_ledger = lambda *a, **k: False
    return saved


def test_plan_turn_emits_one_memory_eval(tmp_path):
    work = _work(tmp_path, "plan")
    # Seed the plan recall stats the way build_phase_goal would.
    phase_runtime._LAST_PLAN_RECALL_STATS = {
        "recall_calls": 1,
        "recall_hits": 1,
        "recall_latency_ms": 12,
        "decisions_reused": 3,
    }
    captured: list[dict] = []
    pre_snap = capture_spec_snapshot(work.specs_dir, work.spec_id, work.phase)
    pre_val = validate_phase_completion(work.specs_dir, work.spec_id, work.phase)

    saved = _disable_ledger()
    try:
        finalize_turn(
            work, ["claude", "-p", "goal"], _exec(), pre_snap, pre_val, SessionState(),
            lane_name="claude-code-cli", emitter=captured.append,
        )
    finally:
        run_ledger.write_run_ledger = saved

    assert len(captured) == 1
    ev = captured[0]
    assert ev["artifact"] == "memory_eval"
    assert ev["phase"] == "4-plan"
    assert ev["run_id"] == "w-77"
    assert ev["spec_id"] == "demo"
    assert ev["lane"] == "claude"
    assert ev["recall_calls"] == 1
    assert ev["recall_hits"] == 1
    assert ev["decisions_reused"] == 3
    assert ev["decisions_written"] == 0  # never written during plan
    assert ev["memory_mode"] in {"off", "hivemind", "holographic"}
    assert ev["plan_tokens_in"] == 100
    assert ev["plan_tokens_out"] == 200
    # The extended memory-telemetry fields are present with their defaults (no budget/distill/
    # supersede configured in this test => 0 / 0 / 0; recall_mode per the contract).
    assert ev["prior_art_tokens"] == 0
    assert ev["decisions_distilled"] == 0
    assert ev["decisions_deduped"] == 0
    assert ev["recall_mode"] in {"push", "pull", "off"}
    assert "rubric_score" in ev and ev["rubric_score"] == 0  # default sentinel


def test_plan_event_carries_prior_art_tokens_from_recall_stats(tmp_path):
    # When the plan recall stash carries prior_art_tokens (the memory hook populates it),
    # the emitted memory_eval surfaces it verbatim.
    work = _work(tmp_path, "plan")
    phase_runtime._LAST_PLAN_RECALL_STATS = {
        "recall_calls": 2,
        "recall_hits": 1,
        "recall_latency_ms": 30,
        "decisions_reused": 1,
        "prior_art_tokens": 137,
    }
    captured: list[dict] = []
    pre_snap = capture_spec_snapshot(work.specs_dir, work.spec_id, work.phase)
    pre_val = validate_phase_completion(work.specs_dir, work.spec_id, work.phase)
    saved = _disable_ledger()
    try:
        finalize_turn(
            work, ["claude", "-p", "goal"], _exec(), pre_snap, pre_val, SessionState(),
            lane_name="claude-code-cli", emitter=captured.append,
        )
    finally:
        run_ledger.write_run_ledger = saved
        # Reset the global so a stale stat never leaks into later tests.
        phase_runtime._reset_plan_recall_stats()

    assert len(captured) == 1
    assert captured[0]["prior_art_tokens"] == 137


def test_verify_event_carries_the_four_new_fields_with_defaults(tmp_path):
    # A verify turn (fake writer, last_write_stats may be zero) still emits all four
    # The extended memory fields with safe defaults — never missing from the event.
    work = _work(tmp_path, "verify")
    captured: list[dict] = []

    def fake_writer(spec_id, module, decisions, learned):
        return len(decisions) + len(learned)

    pre_snap = capture_spec_snapshot(work.specs_dir, work.spec_id, work.phase)
    pre_val = validate_phase_completion(work.specs_dir, work.spec_id, work.phase)
    saved = _disable_ledger()
    try:
        finalize_turn(
            work, ["claude", "-p", "goal"], _exec(), pre_snap, pre_val, SessionState(),
            lane_name="claude-code-cli", emitter=captured.append,
            decision_writer=fake_writer,
            force_verify_passed=True,
            verify_decisions=["d1"],
            verify_learned=[],
        )
    finally:
        run_ledger.write_run_ledger = saved

    assert len(captured) == 1
    ev = captured[0]
    for key in ("prior_art_tokens", "decisions_distilled", "decisions_deduped"):
        assert key in ev and isinstance(ev[key], int)
    assert ev["recall_mode"] in {"push", "pull", "off"}


def test_verify_turn_passed_writes_decisions(tmp_path):
    work = _work(tmp_path, "verify")
    # Make the verify validation "pass" by stubbing validate_phase_completion via
    # an injected post_validation that reports passed. finalize_turn recomputes
    # post_validation internally, so we drive the write through the injected
    # write hook + a forced "passed" decision via the spec artifacts is complex;
    # instead inject the writer and a passed flag.
    captured: list[dict] = []

    writes: list[tuple] = []

    def fake_writer(spec_id, module, decisions, learned):
        writes.append((spec_id, module, decisions, learned))
        return len(decisions) + len(learned)

    pre_snap = capture_spec_snapshot(work.specs_dir, work.spec_id, work.phase)
    pre_val = validate_phase_completion(work.specs_dir, work.spec_id, work.phase)

    saved = _disable_ledger()
    try:
        finalize_turn(
            work, ["claude", "-p", "goal"], _exec(), pre_snap, pre_val, SessionState(),
            lane_name="claude-code-cli", emitter=captured.append,
            decision_writer=fake_writer,
            force_verify_passed=True,
            verify_decisions=["d1", "d2"],
            verify_learned=["l1"],
        )
    finally:
        run_ledger.write_run_ledger = saved

    assert len(captured) == 1
    ev = captured[0]
    assert len(writes) == 1
    assert ev["decisions_written"] == 3  # 2 decisions + 1 learned


def test_verify_turn_failed_writes_nothing(tmp_path):
    work = _work(tmp_path, "verify")
    captured: list[dict] = []
    writes: list[tuple] = []

    def fake_writer(spec_id, module, decisions, learned):
        writes.append((spec_id, module, decisions, learned))
        return len(decisions) + len(learned)

    pre_snap = capture_spec_snapshot(work.specs_dir, work.spec_id, work.phase)
    pre_val = validate_phase_completion(work.specs_dir, work.spec_id, work.phase)

    saved = _disable_ledger()
    try:
        finalize_turn(
            work, ["claude", "-p", "goal"], _exec(), pre_snap, pre_val, SessionState(),
            lane_name="claude-code-cli", emitter=captured.append,
            decision_writer=fake_writer,
            force_verify_passed=False,
            verify_decisions=["d1"],
            verify_learned=[],
        )
    finally:
        run_ledger.write_run_ledger = saved

    assert writes == []  # writer not called
    assert len(captured) == 1
    assert captured[0]["decisions_written"] == 0


def test_memory_eval_written_to_control_root_not_isolated_worktree(tmp_path):
    """M-D: under `pipeline.worktree_isolation`, `work.project_dir` is the
    per-spec isolated worktree — only `.builder/specs/<id>` is symlinked back
    to MAIN there, NOT `.builder/telemetry/`. The default memory_eval sink
    (no emitter injected, so `_emit_finalize_memory_eval` falls through to
    `append_memory_eval`) must write under `control_root` (MAIN) instead of
    `work.project_dir` — otherwise the event is lost when `_cleanup_worktree`
    removes the worktree, and the scheduler-side dedup then drops the
    corresponding main-side emit too, undercounting the Tier-1 A/B clock."""
    worktree_dir = tmp_path / "worktree"
    control_dir = tmp_path / "main"
    (worktree_dir / ".builder" / "specs" / "demo").mkdir(parents=True, exist_ok=True)
    control_dir.mkdir(parents=True, exist_ok=True)
    work = Work(
        work_id="w-md", spec_id="demo", phase="plan",
        project_dir=worktree_dir, specs_dir=worktree_dir / ".builder" / "specs",
        runner_task_ref=None, capability_class=None,
        queue_root=tmp_path / ".builder" / "dispatch-queue",
        log_path=tmp_path / ".builder" / "dispatch-queue" / "queue" / "attempts" / "a.log",
    )
    pre_snap = capture_spec_snapshot(work.specs_dir, work.spec_id, work.phase)
    pre_val = validate_phase_completion(work.specs_dir, work.spec_id, work.phase)
    saved = _disable_ledger()
    try:
        finalize_turn(
            work, ["claude", "-p", "goal"], _exec(), pre_snap, pre_val, SessionState(),
            lane_name="claude-code-cli", control_root=control_dir,
            # No emitter injected -> exercises the DEFAULT append_memory_eval sink.
        )
    finally:
        run_ledger.write_run_ledger = saved

    def _events(root: Path) -> list[Path]:
        base = root / ".builder" / "telemetry" / "events" / "memory-eval"
        return list(base.rglob("*.yaml")) if base.exists() else []

    assert _events(control_dir), "memory_eval must land under control_root"
    assert not _events(worktree_dir), "memory_eval must NOT land under the isolated worktree"


def test_memory_eval_falls_back_to_project_dir_when_control_root_omitted(tmp_path):
    """Backward-compat / non-isolated behavior: an older-shape call that omits
    `control_root` entirely must still write under `work.project_dir`, exactly
    as it did before M-D — this is the non-isolated case where the two are
    equal anyway, or a caller that hasn't been updated yet."""
    work = _work(tmp_path, "plan")
    pre_snap = capture_spec_snapshot(work.specs_dir, work.spec_id, work.phase)
    pre_val = validate_phase_completion(work.specs_dir, work.spec_id, work.phase)
    saved = _disable_ledger()
    try:
        finalize_turn(
            work, ["claude", "-p", "goal"], _exec(), pre_snap, pre_val, SessionState(),
            lane_name="claude-code-cli",  # no control_root kwarg at all
        )
    finally:
        run_ledger.write_run_ledger = saved

    base = tmp_path / ".builder" / "telemetry" / "events" / "memory-eval"
    assert base.exists() and list(base.rglob("*.yaml"))


def test_emitter_failure_does_not_break_finalize(tmp_path):
    work = _work(tmp_path, "plan")

    def boom(_ev):
        raise RuntimeError("sink down")

    pre_snap = capture_spec_snapshot(work.specs_dir, work.spec_id, work.phase)
    pre_val = validate_phase_completion(work.specs_dir, work.spec_id, work.phase)
    saved = _disable_ledger()
    try:
        # Must not raise.
        result = finalize_turn(
            work, ["claude", "-p", "goal"], _exec(), pre_snap, pre_val, SessionState(),
            lane_name="claude-code-cli", emitter=boom,
        )
    finally:
        run_ledger.write_run_ledger = saved
    assert result is not None
    assert result.metadata["spec_id"] == "demo"


# --- S7 Task 3: run-ledger wiring (additive, next to memory_eval) -----------
# finalize_turn imports run_ledger.write_run_ledger lazily; patch the module
# attribute (the local runner has no monkeypatch fixture, so save/restore by hand).


def _patch_ledger(fn):
    saved = run_ledger.write_run_ledger
    run_ledger.write_run_ledger = fn
    return saved


def _finalize_with_ledger_capture(tmp_path, phase, ledger_fn, **finalize_over):
    work = _work(tmp_path, phase)
    pre_snap = capture_spec_snapshot(work.specs_dir, work.spec_id, work.phase)
    pre_val = validate_phase_completion(work.specs_dir, work.spec_id, work.phase)
    saved = _patch_ledger(ledger_fn)
    try:
        return work, finalize_turn(
            work, ["claude", "-p", "goal"], _exec(), pre_snap, pre_val, SessionState(),
            lane_name="claude-code-cli", emitter=lambda _ev: None,
            **finalize_over,
        )
    finally:
        run_ledger.write_run_ledger = saved


def test_ledger_invoked_once_on_plan_finalize(tmp_path):
    calls: list[tuple] = []

    def fake_ledger(work, exec_result, lane_name, decision, *, decisions_learned=None,
                    memory_mode=None, recall_hits=0):
        calls.append((work.spec_id, lane_name, getattr(decision, "outcome", None), decisions_learned))
        return True

    work, result = _finalize_with_ledger_capture(tmp_path, "plan", fake_ledger)
    assert len(calls) == 1
    spec_id, lane_name, outcome, learned = calls[0]
    assert spec_id == "demo"
    assert lane_name == "claude-code-cli"
    assert outcome is not None  # the PostTurnDecision was threaded through
    assert learned is None  # no learned notes on a plan turn
    assert result.metadata["spec_id"] == "demo"


def test_ledger_invoked_once_on_implement_finalize(tmp_path):
    calls: list[tuple] = []

    def fake_ledger(work, exec_result, lane_name, decision, *, decisions_learned=None,
                    memory_mode=None, recall_hits=0):
        calls.append((work.phase, decisions_learned))
        return True

    _finalize_with_ledger_capture(tmp_path, "implement", fake_ledger)
    assert len(calls) == 1
    assert calls[0][0] == "implement"
    assert calls[0][1] is None  # implement is not a verify turn


def test_ledger_invoked_on_verify_finalize(tmp_path):
    calls: list[tuple] = []

    def fake_ledger(work, exec_result, lane_name, decision, *, decisions_learned=None,
                    memory_mode=None, recall_hits=0):
        calls.append((work.phase, decisions_learned))
        return True

    _finalize_with_ledger_capture(tmp_path, "verify", fake_ledger)
    assert len(calls) == 1
    assert calls[0][0] == "verify"
    # verify_learned_for_ledger is threaded through (None when no failures parsed).


def test_raising_ledger_does_not_change_dispatch_result(tmp_path):
    # Run once with a no-op ledger, once with a raising ledger; the returned
    # DispatchResult (result_type + metadata) must be byte-identical.
    def noop_ledger(work, exec_result, lane_name, decision, *, decisions_learned=None):
        return True

    def raising_ledger(work, exec_result, lane_name, decision, *, decisions_learned=None):
        raise RuntimeError("ledger down")

    _, base = _finalize_with_ledger_capture(tmp_path, "plan", noop_ledger)
    _, raised = _finalize_with_ledger_capture(tmp_path, "plan", raising_ledger)

    assert raised.result_type == base.result_type
    # metadata must match except for any path-derived noise (none here: same work).
    assert raised.metadata == base.metadata
    assert raised.metadata["memory_eval_emitted"] is True  # memory_eval unaffected


# --- QW5b: lane kwarg threading through the production lambda -----------------


def test_production_lambda_forwards_lane_to_write_decision_memory(tmp_path):
    """The production code path (no injected decision_writer) builds a lambda that
    wraps write_decision_memory.  Assert that the lambda captures lane_name and
    passes it as ``lane=`` so the agent_id logic in memory_hook receives the
    dispatcher's lane identifier rather than always defaulting to None.

    Strategy: temporarily replace memory_hook.write_decision_memory with a
    capturing shim, set the required env vars so the production branch is taken,
    drive finalize_turn with force_verify_passed=True (no injected writer), then
    restore everything in a finally block.
    """
    work = _work(tmp_path, "verify")

    captured_kwargs: list[dict] = []

    # Shim: record kwargs, return 0 (int, as the caller does int(writer(...))).
    def _shim(spec_id, module, decisions, learned, **kwargs):
        captured_kwargs.append(kwargs)
        return 0

    # The production branch only builds the lambda when HIVEMIND_MCP_URL and
    # HIVEMIND_API_KEY are both set (the env gate at lane_common.py ~541).
    saved_url = os.environ.get("HIVEMIND_MCP_URL")
    saved_key = os.environ.get("HIVEMIND_API_KEY")
    saved_fn = memory_hook.write_decision_memory
    saved_ledger = run_ledger.write_run_ledger
    os.environ["HIVEMIND_MCP_URL"] = "http://fake-hive:8000"
    os.environ["HIVEMIND_API_KEY"] = "fake-key"
    memory_hook.write_decision_memory = _shim
    run_ledger.write_run_ledger = lambda *a, **k: False
    try:
        pre_snap = capture_spec_snapshot(work.specs_dir, work.spec_id, work.phase)
        pre_val = validate_phase_completion(work.specs_dir, work.spec_id, work.phase)
        finalize_turn(
            work, ["claude", "-p", "goal"], _exec(), pre_snap, pre_val, SessionState(),
            lane_name="claude",
            emitter=lambda _ev: None,
            # No decision_writer injected — the production lambda is built.
            force_verify_passed=True,
            verify_decisions=["always commit after green tests"],
            verify_learned=[],
        )
    finally:
        memory_hook.write_decision_memory = saved_fn
        run_ledger.write_run_ledger = saved_ledger
        if saved_url is None:
            os.environ.pop("HIVEMIND_MCP_URL", None)
        else:
            os.environ["HIVEMIND_MCP_URL"] = saved_url
        if saved_key is None:
            os.environ.pop("HIVEMIND_API_KEY", None)
        else:
            os.environ["HIVEMIND_API_KEY"] = saved_key

    assert len(captured_kwargs) == 1, "shim must be called exactly once"
    assert captured_kwargs[0].get("lane") == "claude", (
        f"expected lane='claude', got {captured_kwargs[0]!r}"
    )
