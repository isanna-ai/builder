"""Plan-approval gate (hard stop) — build_phase_goal must HALT the plan phase for
human approval when plan_gate is armed, instead of fast-forwarding into implement.

Root cause this guards: the lane drives plan -> implement -> verify in ONE session
via fast-forward semantics, so the scheduler's plan-approval gate (which fires only
on a discrete `completed == "plan"`) was structurally bypassed. The fix makes the
plan phase a hard stop when the gate is armed; these tests pin the prompt contract.

Env is forced to recall "off" (no HIVEMIND endpoint) so the plan goal builds without
touching memory_hook — mirrors the os.environ pop/restore idiom in the sibling tests.
"""

from __future__ import annotations

import os
from pathlib import Path

from _dispatch_runtime.phase_runtime import build_phase_goal

_RECALL_ENV_KEYS = ("MEMORY_RECALL_MODE", "HIVEMIND_MCP_URL", "HIVEMIND_API_KEY")

# Sentinel phrases that distinguish the two prompt modes.
_FAST_FORWARD = "fast-forward semantics"
_FF_DONT_STOP = "Do not stop until"
_HOLD_GATE = "plan-approval gate"


def _force_recall_off() -> dict[str, str | None]:
    saved = {k: os.environ.get(k) for k in _RECALL_ENV_KEYS}
    for k in _RECALL_ENV_KEYS:
        os.environ.pop(k, None)
    return saved


def _restore(saved: dict[str, str | None]) -> None:
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def _make_spec(tmp_path: Path, phase: str, status: str) -> tuple[Path, Path, str]:
    project_dir = tmp_path
    specs_dir = tmp_path / ".builder" / "specs"
    spec_id = "demo"
    spec_dir = specs_dir / spec_id
    spec_dir.mkdir(parents=True, exist_ok=True)
    (spec_dir / "spec.yaml").write_text(
        f'name: "{spec_id}"\nstatus: "{status}"\ncurrent_phase: "{phase}"\nsummary: "intent"\n',
        encoding="utf-8",
    )
    return project_dir, specs_dir, spec_id


def _goal(tmp_path, phase, *, plan_gate, status="planned"):
    project_dir, specs_dir, spec_id = _make_spec(tmp_path, phase, status)
    saved = _force_recall_off()
    try:
        return build_phase_goal(project_dir, specs_dir, spec_id, phase, None, plan_gate=plan_gate)
    finally:
        _restore(saved)


def test_gated_plan_phase_hard_stops(tmp_path):
    goal = _goal(tmp_path, "plan", plan_gate=True)
    # Halts for approval ...
    assert _HOLD_GATE in goal
    assert "then STOP" in goal
    assert "not start implementing" in goal.lower()
    # ... and explicitly does NOT grant fast-forward.
    assert _FAST_FORWARD not in goal
    assert _FF_DONT_STOP not in goal


def test_ungated_plan_phase_fast_forwards(tmp_path):
    goal = _goal(tmp_path, "plan", plan_gate=False)
    assert _FAST_FORWARD in goal
    assert _FF_DONT_STOP in goal
    # The hold language must be absent on the autonomous path.
    assert _HOLD_GATE not in goal


def test_plan_gate_defaults_to_fast_forward(tmp_path):
    # Backward-compat: the bare (5-arg) call keeps the pre-gate behavior so existing
    # callers / tests are unaffected.
    project_dir, specs_dir, spec_id = _make_spec(tmp_path, "plan", "planned")
    saved = _force_recall_off()
    try:
        goal = build_phase_goal(project_dir, specs_dir, spec_id, "plan", None)
    finally:
        _restore(saved)
    assert _FAST_FORWARD in goal
    assert _HOLD_GATE not in goal


def test_gated_spec_phase_also_holds(tmp_path):
    # The spec phase MUST also complete-then-stop when the gate is armed, otherwise it
    # fast-forwards spec -> verify in one session and the discrete plan completion (which
    # the gate fires on) never happens. So a gated spec goal drops fast-forward too.
    goal = _goal(tmp_path, "spec", plan_gate=True, status="specifying")
    assert _FAST_FORWARD not in goal
    assert _FF_DONT_STOP not in goal
    assert _HOLD_GATE in goal  # gate-armed proceed clause
    # The spec directive itself is unchanged (still the merged specify+design+review pass).
    assert "specify + design + review" in goal


def test_post_plan_phases_keep_fast_forward(tmp_path):
    # implement / verify are AFTER the plan->implement boundary: even with the gate armed
    # they fast-forward (post-approval the implement batch rolls implement -> verify).
    for phase in ("implement", "verify"):
        goal = _goal(tmp_path, phase, plan_gate=True, status="implementing")
        assert _FAST_FORWARD in goal, f"{phase} should keep fast-forward"
        assert _HOLD_GATE not in goal, f"{phase} must not show the gate-hold clause"


# --- scheduler-side robustness: pin the gated phase on re-queue -------------------
#
# Closes the residual race: an interrupted gated plan turn that already advanced
# spec.yaml current_phase to 'implement' (its completion bookkeeping) must NOT
# re-dispatch as an un-gated implement. _maybe_pin_gated_phase pins the dispatch-time
# phase into task_ref so resolve_work re-runs the SAME gated phase. Tested by calling
# the method with a stand-in self (it only reads self.pipeline) — no real queue.
import types  # noqa: E402

from _dispatch_runtime.scheduler import DispatchScheduler  # noqa: E402


class _Item:
    def __init__(self, task_ref):
        self.task_ref = task_ref


class _FakeSched:
    """Minimal stand-in: stubs per-spec gate resolution, binds the real methods."""
    def __init__(self, gate):
        self._gate = gate

    def _effective_plan_gate(self, spec_id):
        return self._gate

    _spec_id_for = DispatchScheduler._spec_id_for
    _maybe_pin_gated_phase = DispatchScheduler._maybe_pin_gated_phase


def _pin(plan_gate, dispatch_phase, existing=None):
    task_ref = {"spec_id": "demo", "kind": "builder-phase-batch"}
    if existing is not None:
        task_ref["phase"] = existing
    item = _Item(task_ref)
    result = types.SimpleNamespace(metadata={"phase": dispatch_phase})
    pinned = _FakeSched(plan_gate)._maybe_pin_gated_phase(item, result)
    return pinned, item.task_ref.get("phase")


def test_pin_holds_gated_plan_on_requeue():
    # A re-queued gated PLAN turn is pinned to 'plan' so a resume cannot morph to the
    # 'implement' current_phase it advanced to as bookkeeping.
    pinned, phase = _pin(True, "plan")
    assert pinned is True
    assert phase == "plan"


def test_pin_holds_gated_spec_on_requeue():
    # The spec phase is pinned too (a spec overrun would otherwise re-detect verify).
    pinned, phase = _pin(True, "spec")
    assert pinned is True
    assert phase == "spec"


def test_pin_noop_for_post_plan_phases():
    # implement/verify are past the gate (post-approval) — never pinned.
    for ph in ("implement", "verify"):
        pinned, phase = _pin(True, ph)
        assert pinned is False
        assert phase is None


def test_pin_noop_when_gate_off():
    # plan_gate off => no pin, so the non-gated fast-forward-via-resume path is intact.
    pinned, phase = _pin(False, "plan")
    assert pinned is False
    assert phase is None


def test_pin_is_idempotent():
    # Already pinned to the same phase => no rewrite (returns False), value preserved.
    pinned, phase = _pin(True, "plan", existing="plan")
    assert pinned is False
    assert phase == "plan"


# --- admission guard: resolve_work folds un-approved post-gate phases to plan -------
#
# The airtight choke point: every lane dispatch resolves its phase here. Under an armed
# gate, a phase past the plan->implement boundary runs ONLY with a <spec>.approved token
# (written by `approve`). Any other arrival (crash/reclaim re-detect, un-pinned resume)
# is folded back to 'plan'. This closes the paths the result-handler pin cannot see.
from _dispatch_runtime.lane_common import resolve_work  # noqa: E402


def _resolve(tmp_path, current_phase, *, plan_gate, approved):
    spec_id = "demo"
    spec_dir = tmp_path / ".builder" / "specs" / spec_id
    spec_dir.mkdir(parents=True, exist_ok=True)
    (spec_dir / "spec.yaml").write_text(
        f'name: "{spec_id}"\nstatus: "implementing"\ncurrent_phase: "{current_phase}"\n',
        encoding="utf-8",
    )
    queue_root = tmp_path / ".builder" / "dispatch-queue"
    gates = queue_root / "queue" / "gates"
    gates.mkdir(parents=True, exist_ok=True)
    if approved:
        (gates / f"{spec_id}.approved").write_text("approved phase: implement\n", encoding="utf-8")
    task_ref = {"kind": "builder-phase-batch", "spec_id": spec_id}  # no runner packet => detect_phase
    attempt_context = {
        "work_id": "w1", "attempt_id": "a1",
        "workspace_root": str(tmp_path),
        "queue_root": str(queue_root),
        "plan_gate": plan_gate,
    }
    return resolve_work(task_ref, attempt_context).phase


def test_admission_folds_unapproved_implement_to_plan(tmp_path):
    # The crash/reclaim case: current_phase advanced to implement, no approve => fold.
    assert _resolve(tmp_path, "implement", plan_gate=True, approved=False) == "plan"


def test_admission_folds_unapproved_verify_to_plan(tmp_path):
    assert _resolve(tmp_path, "verify", plan_gate=True, approved=False) == "plan"


def test_admission_allows_approved_implement(tmp_path):
    # Post-approval: the token is present => implement runs normally.
    assert _resolve(tmp_path, "implement", plan_gate=True, approved=True) == "implement"


def test_admission_noop_when_gate_off(tmp_path):
    # Gate off => no fold; implement resolves as detected (non-gated behavior intact).
    assert _resolve(tmp_path, "implement", plan_gate=False, approved=False) == "implement"


def test_admission_ignores_pre_gate_phases(tmp_path):
    # A pre-boundary phase (spec) is never folded; it is gated by the prompt side, not here.
    assert _resolve(tmp_path, "spec", plan_gate=True, approved=False) == "spec"


# --- per-spec opt-in: full automation by default; spec.yaml plan_gate:true opts in --
#
# The gate is OFF by default (full automation to verified/ready-to-archive). A spec opts
# in via `plan_gate: true` in its spec.yaml (written by `draft --plan-gate`). The
# scheduler resolves the effective value per spec, falling back to the pipeline default.

def _effective(tmp_path, spec_value, *, pipeline_default):
    spec_id = "demo"
    spec_dir = tmp_path / ".builder" / "specs" / spec_id
    spec_dir.mkdir(parents=True, exist_ok=True)
    gate_line = "" if spec_value is None else f"plan_gate: {spec_value}\n"
    (spec_dir / "spec.yaml").write_text(
        f'name: "{spec_id}"\nstatus: "specifying"\n{gate_line}', encoding="utf-8"
    )

    class _S:
        pipeline = {"plan_gate": pipeline_default}
        project_dir = tmp_path
        _effective_plan_gate = DispatchScheduler._effective_plan_gate

    return _S()._effective_plan_gate(spec_id)


def test_default_is_full_automation(tmp_path):
    # No spec flag + pipeline default off => gate OFF: the spec runs straight through.
    assert _effective(tmp_path, None, pipeline_default=False) is False


def test_spec_opts_into_gate(tmp_path):
    # spec.yaml plan_gate: true opts IN even when the project default is off.
    assert _effective(tmp_path, "true", pipeline_default=False) is True


def test_spec_opts_out_of_project_gate(tmp_path):
    # An explicit plan_gate: false overrides a project default of on.
    assert _effective(tmp_path, "false", pipeline_default=True) is False


def test_no_spec_flag_inherits_project_default(tmp_path):
    assert _effective(tmp_path, None, pipeline_default=True) is True


from _dispatch_runtime.cli import _draft_spec  # noqa: E402


def test_draft_without_flag_is_ungated(tmp_path):
    specs = tmp_path / "specs"
    specs.mkdir()
    sid = _draft_spec(specs, "do a thing", "demo")
    assert "plan_gate" not in (specs / sid / "spec.yaml").read_text()


def test_draft_with_plan_gate_flag_opts_in(tmp_path):
    specs = tmp_path / "specs"
    specs.mkdir()
    sid = _draft_spec(specs, "do a risky thing", "demo", plan_gate=True)
    assert "plan_gate: true" in (specs / sid / "spec.yaml").read_text()


# --- R13: drafted specs default to ai_native (no model-authored Markdown dual-write) ---

def _draft_with_env(tmp_path, value):
    specs = tmp_path / "specs"
    specs.mkdir()
    saved = os.environ.get("BUILDER_DRAFT_ARTIFACT_MODE")
    if value is None:
        os.environ.pop("BUILDER_DRAFT_ARTIFACT_MODE", None)
    else:
        os.environ["BUILDER_DRAFT_ARTIFACT_MODE"] = value
    try:
        sid = _draft_spec(specs, "do a thing", "demo")
        return (specs / sid / "spec.yaml").read_text()
    finally:
        if saved is None:
            os.environ.pop("BUILDER_DRAFT_ARTIFACT_MODE", None)
        else:
            os.environ["BUILDER_DRAFT_ARTIFACT_MODE"] = saved


def test_draft_defaults_to_ai_native(tmp_path):
    assert "artifact_mode: ai_native" in _draft_with_env(tmp_path, None)


def test_draft_artifact_mode_env_override_to_dual(tmp_path):
    # The escape hatch restores the old dual-write default.
    assert "artifact_mode: dual" in _draft_with_env(tmp_path, "dual")


def test_draft_artifact_mode_invalid_env_falls_back_ai_native(tmp_path):
    # A garbage value must not produce an invalid artifact_mode.
    assert "artifact_mode: ai_native" in _draft_with_env(tmp_path, "nonsense")
