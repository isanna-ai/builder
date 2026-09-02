"""The plan/implement completion gate must accept EITHER tasks.* or plan.*.

The plan-phase artifact is named tasks.yaml by some runners and plan.yaml by the
claude phase-batch lane. Requiring only tasks.* silently stalled every spec whose
lane wrote plan.yaml: validate_phase_completion never matched the artifact, so the
turn resumed forever until max_attempts and the spec FAILED (even with all gates
green). These tests pin that both names satisfy the gate.
"""

from __future__ import annotations

from pathlib import Path

from _dispatch_runtime.phase_runtime import (
    required_phase_artifact_groups,
    validate_phase_completion,
)


def _planned_spec(tmp_path: Path, plan_file: str) -> Path:
    sd = tmp_path / "specs" / "demo"
    sd.mkdir(parents=True, exist_ok=True)
    (sd / "spec.yaml").write_text("name: demo\nstatus: planned\ncurrent_phase: implement\n")
    (sd / plan_file).write_text("tasks: []\n")
    (sd / "handoff.yaml").write_text(
        "next_phase: implement\nspec: demo\nready: true\ncompleted_phase: plan\n"
    )
    (sd / "phase-log.yaml").write_text(
        'phases:\n  - phase: plan\n    completed: "2026-06-10T00:00:00Z"\n    outcome: SUCCEEDED\n'
    )
    return tmp_path / "specs"


def test_plan_complete_accepts_plan_yaml(tmp_path):
    # claude phase-batch lane names the artifact plan.yaml (the F3 failure case)
    r = validate_phase_completion(_planned_spec(tmp_path, "plan.yaml"), "demo", "plan")
    assert r.passed, r.reason


def test_plan_complete_accepts_tasks_yaml(tmp_path):
    # legacy runner names it tasks.yaml — still accepted (no regression)
    r = validate_phase_completion(_planned_spec(tmp_path, "tasks.yaml"), "demo", "plan")
    assert r.passed, r.reason


def test_plan_complete_rejects_when_neither_present(tmp_path):
    # no plan artifact at all => the gate still rejects (it is a real reject)
    sd = tmp_path / "specs" / "demo"
    sd.mkdir(parents=True, exist_ok=True)
    (sd / "spec.yaml").write_text("name: demo\nstatus: planned\ncurrent_phase: implement\n")
    (sd / "handoff.yaml").write_text(
        "next_phase: implement\nspec: demo\nready: true\ncompleted_phase: plan\n"
    )
    (sd / "phase-log.yaml").write_text(
        'phases:\n  - phase: plan\n    completed: "2026-06-10T00:00:00Z"\n    outcome: SUCCEEDED\n'
    )
    r = validate_phase_completion(tmp_path / "specs", "demo", "plan")
    assert not r.passed


def test_artifact_group_lists_both_names():
    for phase in ("plan", "implement", "4-plan", "5-implement"):
        grp = required_phase_artifact_groups(phase)[0]
        assert "plan.yaml" in grp and "tasks.yaml" in grp, f"{phase}: {grp}"
