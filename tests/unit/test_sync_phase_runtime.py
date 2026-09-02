from __future__ import annotations

from pathlib import Path

from _dispatch_runtime.phase_runtime import sync_visibility, validate_phase_completion
from tests.unit.sync_evidence_support import write_host_scope, write_sync_result


def test_verify_completion_requires_sync_next_phase(tmp_path: Path):
    spec_dir = tmp_path / "specs" / "demo"
    spec_dir.mkdir(parents=True, exist_ok=True)
    spec_dir.joinpath("spec.yaml").write_text("name: demo\nstatus: syncing\ncurrent_phase: sync\n", encoding="utf-8")
    spec_dir.joinpath("phase-log.yaml").write_text(
        'phases:\n  - phase: verify\n    completed: "2026-07-20T12:30:00Z"\n    outcome: SUCCEEDED\n',
        encoding="utf-8",
    )
    spec_dir.joinpath("handoff.yaml").write_text(
        "next_phase: sync\nspec: demo\nready: true\ncompleted_phase: verify\n",
        encoding="utf-8",
    )
    result = validate_phase_completion(tmp_path / "specs", "demo", "verify")
    assert result.passed, result.reason


def test_divergence_projects_verified_awaiting_sync(tmp_path: Path):
    spec_dir = tmp_path / ".builder" / "specs" / "demo"
    spec_dir.mkdir(parents=True)
    spec_dir.joinpath("spec.yaml").write_text("name: demo\nstatus: verified\ncurrent_phase: sync\n", encoding="utf-8")
    delta = "capabilities: []\nbehaviors: []\njourneys: []\n"
    spec_dir.joinpath("ssot-delta.yaml").write_text(delta, encoding="utf-8")
    scope = write_host_scope(tmp_path, "demo")
    write_sync_result(spec_dir, scope, "divergence", undeclared=[
        {"category": "capabilities", "target": "outside", "change": "enrich"}
    ])
    assert sync_visibility(spec_dir) == "verified-awaiting-sync"
