from __future__ import annotations

from pathlib import Path

from _validators.common import ValidationContext
from _validators.sync_artifacts import run_sync_result
from tests.unit.sync_evidence_support import write_host_scope, write_sync_result


def test_sync_result_requires_matching_delta_digest(tmp_path: Path):
    spec_dir = tmp_path / ".builder" / "specs" / "demo"
    spec_dir.mkdir(parents=True)
    spec_dir.joinpath("spec.yaml").write_text("name: demo\nstatus: verified\ncurrent_phase: sync\n", encoding="utf-8")
    spec_dir.joinpath("ssot-delta.yaml").write_text("capabilities: []\nbehaviors: []\njourneys: []\n", encoding="utf-8")
    scope = write_host_scope(tmp_path, "demo")
    write_sync_result(spec_dir, scope, "divergence", undeclared=[
        {"category": "capabilities", "target": "outside", "change": "enrich"}
    ])
    text = spec_dir.joinpath("sync-result.yaml").read_text(encoding="utf-8")
    spec_dir.joinpath("sync-result.yaml").write_text(text.replace(scope["declared_delta_digest"], "stale"), encoding="utf-8")
    result = run_sync_result(ValidationContext(spec_dir=spec_dir))
    assert any("declared_delta_digest" in err for err in result.errors)


def test_sync_result_requires_matching_host_sync_gate(tmp_path: Path):
    spec_dir = tmp_path / ".builder" / "specs" / "demo"
    spec_dir.mkdir(parents=True)
    spec_dir.joinpath("spec.yaml").write_text(
        "name: demo\nstatus: verified\ncurrent_phase: sync\n", encoding="utf-8"
    )
    spec_dir.joinpath("ssot-delta.yaml").write_text(
        "capabilities: []\nbehaviors: []\njourneys: []\n", encoding="utf-8"
    )
    scope = write_host_scope(tmp_path, "demo")
    write_sync_result(spec_dir, scope, "divergence", undeclared=[
        {"category": "capabilities", "target": "outside", "change": "enrich"}
    ])
    text = spec_dir.joinpath("sync-result.yaml").read_text(encoding="utf-8")
    spec_dir.joinpath("sync-result.yaml").write_text(
        text.replace("sync_gate_sha256:", "unexpected_field: forged\nsync_gate_sha256:"),
        encoding="utf-8",
    )
    result = run_sync_result(ValidationContext(spec_dir=spec_dir))
    assert result.errors
