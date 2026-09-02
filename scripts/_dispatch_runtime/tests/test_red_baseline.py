"""R11: plan-time RED baseline gate. A `tdd_mode: required` task's FOCUSED verify command
must be RED (exit != 0) on the pre-implementation working tree; if it PASSES (exit 0) before
the code exists it is NON-PROBATIVE and plan approval is BLOCKED. Staged via a NEW flag,
BUILDER_RED_BASELINE (off default / warn / enforce), mirroring BUILDER_HOST_VERIFY.

Shim-safe: no pytest.raises / monkeypatch; env via set/restore; the subprocess is an injected
runner (verify_runner) so no real process is ever spawned."""

from __future__ import annotations

import os
from pathlib import Path

from _dispatch_runtime.lane_common import (
    SessionState,
    Work,
    _plan_red_baseline_tasks,
    _red_baseline_gate,
    _task_focused_verify,
    _task_tdd_mode,
    finalize_turn,
)
from _dispatch_runtime.phase_runtime import (
    capture_spec_snapshot,
    validate_phase_completion,
)

_ENV = "BUILDER_RED_BASELINE"


def _work(tmp_path, *, spec_id="demo", phase="plan"):
    return Work(
        work_id="w1", spec_id=spec_id, phase=phase,
        project_dir=tmp_path, specs_dir=tmp_path / ".builder" / "specs",
        runner_task_ref=None, capability_class=None,
        queue_root=tmp_path / ".builder" / "dispatch-queue",
        log_path=tmp_path / "log",
    )


def _with_env(value, fn):
    saved = os.environ.get(_ENV)
    if value is None:
        os.environ.pop(_ENV, None)
    else:
        os.environ[_ENV] = value
    try:
        return fn()
    finally:
        if saved is None:
            os.environ.pop(_ENV, None)
        else:
            os.environ[_ENV] = saved


def _runner(rc_map):
    """verify_runner(cmd, cwd) -> returncode from rc_map (default 0 == the command PASSES)."""
    return lambda cmd, cwd: rc_map.get(cmd, 0)


def _canonical_task(tid, mode, *commands):
    """Canonical tasks.yaml task: nested `tdd.mode` + `verify: [{command}]` (first == focused)."""
    return {"id": tid, "tdd": {"mode": mode}, "verify": [{"command": c} for c in commands]}


# --- task-shape normalization -----------------------------------------------

def test_tdd_mode_from_canonical_and_flat():
    assert _task_tdd_mode({"tdd": {"mode": "required"}}) == "required"
    assert _task_tdd_mode({"tdd_mode": "required"}) == "required"      # flat runner-packet form
    assert _task_tdd_mode({"tdd": {"mode": "exempt"}}) == "exempt"
    assert _task_tdd_mode({}) == ""
    assert _task_tdd_mode("not-a-dict") == ""


def test_focused_verify_is_first_command():
    # Canonical `verify: [{command}]` — the FIRST (narrowest) command is the focused test.
    assert _task_focused_verify(_canonical_task("T1", "required", "pytest -q a", "pytest all")) == "pytest -q a"
    # Flat runner-packet `verify_commands: [...]`.
    assert _task_focused_verify({"verify_commands": ["first", "second"]}) == "first"
    assert _task_focused_verify({}) is None


# --- the gate ---------------------------------------------------------------

def test_red_baseline_passes_when_focused_verify_is_red(tmp_path):
    # tdd-required task whose focused verify FAILS (rc 1) on the pre-impl tree -> RED confirmed.
    tasks = [_canonical_task("T1", "required", "pytest -q")]
    passed, reason = _with_env("enforce", lambda: _red_baseline_gate(
        _work(tmp_path), tasks, verify_runner=_runner({"pytest -q": 1})))
    assert passed is True and reason == ""


def test_red_baseline_blocks_when_focused_verify_passes(tmp_path):
    # tdd-required task whose focused verify PASSES (rc 0) before any code -> NON-PROBATIVE -> block,
    # with the task id AND command in the reason.
    tasks = [_canonical_task("T7", "required", "pytest -q")]
    passed, reason = _with_env("enforce", lambda: _red_baseline_gate(
        _work(tmp_path), tasks, verify_runner=_runner({"pytest -q": 0})))
    assert passed is False
    assert "T7" in reason and "pytest -q" in reason and "non-probative" in reason


def test_red_baseline_enforces_by_default(tmp_path):
    # Flag unset -> the gate RUNS. A TDD task whose focused verify already PASSES before a line
    # of code is written is a non-probative test, and that now blocks by default.
    tasks = [_canonical_task("T1", "required", "pytest -q")]
    passed, reason = _with_env(None, lambda: _red_baseline_gate(
        _work(tmp_path, phase="plan"), tasks, phase="plan", verify_runner=_runner({"pytest -q": 0})))
    assert passed is False and "pytest -q" in reason


def test_red_baseline_off_is_explicit_opt_out(tmp_path):
    tasks = [_canonical_task("T1", "required", "pytest -q")]
    passed, reason = _with_env("off", lambda: _red_baseline_gate(
        _work(tmp_path), tasks, verify_runner=_runner({"pytest -q": 0})))
    assert passed is None and reason == ""


def test_red_baseline_ignores_non_tdd_required_tasks(tmp_path):
    # A non-tdd-required task whose focused verify PASSES must be IGNORED (no block) even under enforce.
    tasks = [_canonical_task("T1", "exempt", "pytest -q")]
    passed, reason = _with_env("enforce", lambda: _red_baseline_gate(
        _work(tmp_path), tasks, verify_runner=_runner({"pytest -q": 0})))
    assert passed is None and reason == ""


def test_red_baseline_no_focused_command_cannot_gate(tmp_path):
    # A tdd-required task with NO verify command -> nothing to prove -> no-op (None).
    tasks = [{"id": "T1", "tdd": {"mode": "required"}}]
    passed, reason = _with_env("enforce", lambda: _red_baseline_gate(
        _work(tmp_path), tasks, verify_runner=_runner({})))
    assert passed is None and reason == ""


def test_red_baseline_empty_tasks_is_noop(tmp_path):
    passed, reason = _with_env("enforce", lambda: _red_baseline_gate(
        _work(tmp_path), None, verify_runner=_runner({})))
    assert passed is None and reason == ""


def test_red_baseline_only_focused_command_is_run(tmp_path):
    # Only the FIRST verify command is the RED baseline: a task whose focused (first) command is RED
    # confirms the baseline even if a later (broader) command would pass.
    tasks = [_canonical_task("T1", "required", "focused-red", "broad-green")]
    passed, reason = _with_env("enforce", lambda: _red_baseline_gate(
        _work(tmp_path), tasks, verify_runner=_runner({"focused-red": 1, "broad-green": 0})))
    assert passed is True and reason == ""


def test_red_baseline_warn_never_blocks(tmp_path):
    # warn mode runs but never returns False; a non-probative task is reported, not blocked.
    tasks = [_canonical_task("T3", "required", "pytest -q")]
    passed, reason = _with_env("warn", lambda: _red_baseline_gate(
        _work(tmp_path), tasks, verify_runner=_runner({"pytest -q": 0})))
    assert passed is None
    assert reason.startswith("[warn]") and "T3" in reason


def test_red_baseline_flat_runner_packet_form(tmp_path):
    # Flat form (tdd_mode + verify_commands) is honored too; focused = verify_commands[0].
    tasks = [{"task_id": "T9", "tdd_mode": "required", "verify_commands": ["flat-cmd", "other"]}]
    passed, reason = _with_env("enforce", lambda: _red_baseline_gate(
        _work(tmp_path), tasks, verify_runner=_runner({"flat-cmd": 0})))
    assert passed is False and "T9" in reason and "flat-cmd" in reason


# --- plan-task loading ------------------------------------------------------

def test_plan_red_baseline_tasks_reads_tasks_yaml(tmp_path):
    spec_dir = tmp_path / ".builder" / "specs" / "demo"
    spec_dir.mkdir(parents=True, exist_ok=True)
    (spec_dir / "tasks.yaml").write_text(
        "artifact: tasks\ntasks:\n  - id: T1\n    tdd:\n      mode: required\n"
        "    verify:\n      - command: pytest -q\n",
        encoding="utf-8",
    )
    tasks = _plan_red_baseline_tasks(_work(tmp_path))
    assert isinstance(tasks, list) and len(tasks) == 1
    assert _task_tdd_mode(tasks[0]) == "required"
    assert _task_focused_verify(tasks[0]) == "pytest -q"


def test_plan_red_baseline_tasks_missing_is_empty(tmp_path):
    assert _plan_red_baseline_tasks(_work(tmp_path)) == []


# --- finalize_turn integration (block plan approval) ------------------------

def _plan_spec(tmp_path, *, focused_cmd="pytest -q", tdd="required"):
    """A MINIMAL but valid `plan` completion: phase-log + spec.yaml (planned/implement) +
    tasks.yaml + handoff.yaml, so validate_phase_completion passes and the host gate runs."""
    specs_dir = tmp_path / ".builder" / "specs"
    spec_dir = specs_dir / "demo"
    spec_dir.mkdir(parents=True, exist_ok=True)
    (spec_dir / "spec.yaml").write_text(
        'name: "demo"\nstatus: "planned"\ncurrent_phase: "implement"\n', encoding="utf-8")
    (spec_dir / "tasks.yaml").write_text(
        "artifact: tasks\ntasks:\n"
        f"  - id: T1\n    tdd:\n      mode: {tdd}\n"
        f"    verify:\n      - command: {focused_cmd}\n",
        encoding="utf-8",
    )
    (spec_dir / "handoff.yaml").write_text(
        "next_phase: implement\nspec: demo\nready: true\ncompleted_phase: plan\n", encoding="utf-8")
    (spec_dir / "phase-log.yaml").write_text(
        'phases:\n  - phase: plan\n    completed: "2026-07-10T00:00:00Z"\n    outcome: SUCCEEDED\n',
        encoding="utf-8",
    )
    return specs_dir, spec_dir


def _finalize(tmp_path, specs_dir, *, verify_runner):
    queue_root = tmp_path / ".builder" / "dispatch-queue"
    work = Work(
        work_id="w-red", spec_id="demo", phase="plan",
        project_dir=tmp_path, specs_dir=specs_dir,
        runner_task_ref=None, capability_class=None,
        queue_root=queue_root, log_path=queue_root / "queue" / "attempts" / "a.log",
    )
    pre_snap = capture_spec_snapshot(work.specs_dir, work.spec_id, work.phase)
    pre_val = validate_phase_completion(work.specs_dir, work.spec_id, work.phase)
    assert pre_val.passed  # sanity: the fixture IS a valid plan completion
    exec_result = {"status": "interrupted", "stdout": "", "stderr": "", "returncode": 0, "session_id": "s"}

    from _dispatch_runtime import run_ledger

    saved_ledger = run_ledger.write_run_ledger
    run_ledger.write_run_ledger = lambda *a, **k: False  # never hit the live hivemind wire
    try:
        return finalize_turn(
            work, ["claude", "-p", "goal"], exec_result, pre_snap, pre_val, SessionState(),
            lane_name="claude-code-cli", verify_runner=verify_runner,
        )
    finally:
        run_ledger.write_run_ledger = saved_ledger


def test_finalize_blocks_plan_when_focused_verify_passes(tmp_path):
    # enforce + a tdd-required task whose focused verify PASSES pre-impl -> plan is NOT approved.
    specs_dir, _spec_dir = _plan_spec(tmp_path, focused_cmd="pytest -q", tdd="required")
    result = _with_env("enforce", lambda: _finalize(
        tmp_path, specs_dir, verify_runner=_runner({"pytest -q": 0})))
    assert result.metadata["decision"] != "phase-complete"
    assert "non-probative" in (result.metadata.get("host_verify") or "")


def test_finalize_approves_plan_when_baseline_is_red(tmp_path):
    # enforce + a RED focused verify -> plan completes normally (baseline confirmed).
    specs_dir, _spec_dir = _plan_spec(tmp_path, focused_cmd="pytest -q", tdd="required")
    result = _with_env("enforce", lambda: _finalize(
        tmp_path, specs_dir, verify_runner=_runner({"pytest -q": 1})))
    assert result.metadata["decision"] == "phase-complete"


def test_finalize_off_is_explicit_opt_out_ignores_passing_baseline(tmp_path):
    # Explicit opt-out -> the passing focused verify does NOT block: plan completes.
    specs_dir, _spec_dir = _plan_spec(tmp_path, focused_cmd="pytest -q", tdd="required")
    result = _with_env("off", lambda: _finalize(
        tmp_path, specs_dir, verify_runner=_runner({"pytest -q": 0})))
    assert result.metadata["decision"] == "phase-complete"
