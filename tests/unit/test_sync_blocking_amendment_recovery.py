from __future__ import annotations

from pathlib import Path

from _dispatch_runtime.phase_runtime import sync_visibility
from tests.unit.sync_evidence_support import write_host_scope, write_sync_result


def test_clean_sync_result_projects_synced(tmp_path: Path):
    spec_dir = tmp_path / ".builder" / "specs" / "demo"
    spec_dir.mkdir(parents=True)
    spec_dir.joinpath("spec.yaml").write_text("name: demo\nstatus: synced\ncurrent_phase: sync\n", encoding="utf-8")
    delta = "capabilities: []\nbehaviors: []\njourneys: []\n"
    spec_dir.joinpath("ssot-delta.yaml").write_text(delta, encoding="utf-8")
    scope = write_host_scope(tmp_path, "demo")
    write_sync_result(spec_dir, scope, "synced")
    assert sync_visibility(spec_dir) == "synced"
