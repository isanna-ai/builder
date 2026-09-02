from __future__ import annotations

from pathlib import Path

from _validators.common import ValidationContext
from _validators.sync_artifacts import run_ssot_delta


def test_ssot_delta_validation_is_file_native(tmp_path: Path):
    spec_dir = tmp_path / ".builder" / "specs" / "demo"
    intent_dir = tmp_path / ".builder" / "intents" / "demo-intent"
    intent_dir.mkdir(parents=True, exist_ok=True)
    intent_dir.joinpath("intent.yaml").write_text(
        "artifact: intent-object\nintent: demo-intent\ntitle: t\nstatus: accepted\nproblem: p\nwhy: w\n"
        "success_criteria:\n  - id: sc-1\n    statement: s\nnon_goals:\n  - n\n"
        "ssot_delta:\n  capabilities: []\n  behaviors: []\n  journeys: []\n"
        "specs:\n  - demo\n",
        encoding="utf-8",
    )
    spec_dir.mkdir(parents=True, exist_ok=True)
    spec_dir.joinpath("spec.yaml").write_text(
        "name: demo\ncreated: '2026-07-20'\nstatus: planned\ncurrent_phase: implement\nnext_action: x\n",
        encoding="utf-8",
    )
    spec_dir.joinpath("ssot-delta.yaml").write_text(
        "capabilities: []\nbehaviors: []\njourneys: []\n",
        encoding="utf-8",
    )
    result = run_ssot_delta(ValidationContext(spec_dir=spec_dir))
    assert result.errors == []
