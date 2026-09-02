from __future__ import annotations

from pathlib import Path

from _validators.common import ValidationContext
from _validators.sync_artifacts import run_ssot_delta


def _spec(tmp_path: Path, *, status: str, with_delta: bool) -> Path:
    spec_dir = tmp_path / ".builder" / "specs" / "demo"
    intent_dir = tmp_path / ".builder" / "intents" / "demo-intent"
    intent_dir.mkdir(parents=True, exist_ok=True)
    intent_dir.joinpath("intent.yaml").write_text(
        "artifact: intent-object\nintent: demo-intent\ntitle: t\nstatus: accepted\nproblem: p\nwhy: w\n"
        "success_criteria:\n  - id: sc-1\n    statement: s\nnon_goals:\n  - n\n"
        "ssot_delta:\n  capabilities:\n    - target: sync-phase\n      change: create\n  behaviors: []\n  journeys: []\n"
        "specs:\n  - demo\n",
        encoding="utf-8",
    )
    spec_dir.mkdir(parents=True, exist_ok=True)
    spec_dir.joinpath("spec.yaml").write_text(
        f"name: demo\ncreated: '2026-07-20'\nstatus: {status}\ncurrent_phase: implement\nnext_action: x\n",
        encoding="utf-8",
    )
    if with_delta:
        spec_dir.joinpath("ssot-delta.yaml").write_text(
            "capabilities:\n  - target: sync-phase\n    change: create\nbehaviors: []\njourneys: []\n",
            encoding="utf-8",
        )
    return spec_dir


def test_pre_plan_specs_do_not_require_ssot_delta(tmp_path: Path):
    result = run_ssot_delta(ValidationContext(spec_dir=_spec(tmp_path, status="specified", with_delta=False)))
    assert result.skipped and result.errors == []


def test_planned_through_synced_specs_require_ssot_delta(tmp_path: Path):
    result = run_ssot_delta(ValidationContext(spec_dir=_spec(tmp_path, status="planned", with_delta=False)))
    assert any("required for spec status" in err for err in result.errors)


def test_archived_specs_remain_exempt(tmp_path: Path):
    result = run_ssot_delta(ValidationContext(spec_dir=_spec(tmp_path, status="archived", with_delta=False)))
    assert result.skipped and result.errors == []
