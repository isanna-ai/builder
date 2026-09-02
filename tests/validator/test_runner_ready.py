from __future__ import annotations

from pathlib import Path

from scripts._validators.common import ValidationContext
from scripts._validators.runner_ready import run


def make(tmp_path: Path, profile: str | None = "tiny_local") -> Path:
    spec = tmp_path / ".builder" / "specs" / "demo"
    spec.mkdir(parents=True)
    (tmp_path / "schemas").mkdir()
    (tmp_path / "schemas" / "runner.schema.yaml").write_text("properties:\n  shell_allow_list:\n    examples: [git, python3 scripts/validate-spec.py]\n", encoding="utf-8")
    (spec / "spec.yaml").write_text("name: demo\n" + (f"target_model_profile: {profile}\n" if profile else ""), encoding="utf-8")
    (spec / "tasks.yaml").write_text("tasks: []\n", encoding="utf-8")
    return spec


def test_human_gate_with_profile_errors(tmp_path: Path) -> None:
    spec = make(tmp_path)
    (spec / "tasks.yaml").write_text("tasks:\n  - id: T1\n    human_gate: Decide\n", encoding="utf-8")
    assert "T1" in "\n".join(run(ValidationContext(spec)).errors)


def test_unresolved_decision_errors(tmp_path: Path) -> None:
    spec = make(tmp_path)
    (spec / "decisions.yaml").write_text("decisions:\n  - id: D1\n    status: unresolved\n", encoding="utf-8")
    assert "D1" in "\n".join(run(ValidationContext(spec)).errors)


def test_unresolved_decision_hint_points_at_owning_phase(tmp_path: Path) -> None:
    spec = make(tmp_path)
    (spec / "decisions.yaml").write_text(
        "decisions:\n  - id: D1\n    phase: 2-design\n    status: unresolved\n",
        encoding="utf-8",
    )
    joined = "\n".join(run(ValidationContext(spec)).errors)
    assert "D1" in joined
    assert "phase 2-design" in joined
    # the hint must not hardcode a single command that may not own the decision
    assert "/isanna-2-design" not in joined


def test_legacy_human_gate_passes(tmp_path: Path) -> None:
    spec = make(tmp_path, None)
    (spec / "tasks.yaml").write_text("tasks:\n  - id: T1\n    human_gate: Decide\n", encoding="utf-8")
    assert run(ValidationContext(spec)).errors == []


def test_environment_verify_not_allow_listed(tmp_path: Path) -> None:
    spec = make(tmp_path)
    (spec / "spec.yaml").write_text("target_model_profile: tiny_local\nenvironment_readiness:\n  - id: ENV1\n    verify: rm -rf /tmp\n", encoding="utf-8")
    assert "ENV1" in "\n".join(run(ValidationContext(spec)).errors)


def test_invalid_post_runner_review_scope(tmp_path: Path) -> None:
    spec = make(tmp_path)
    (spec / "spec.yaml").write_text("target_model_profile: tiny_local\npost_runner_review:\n  required: true\n  scope: invalid_value\n", encoding="utf-8")
    assert "invalid_value" in "\n".join(run(ValidationContext(spec)).errors)
