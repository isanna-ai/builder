from __future__ import annotations

import isanna as isanna_cli
from tests.unit.sync_evidence_support import write_host_scope


def test_isanna_sync_requires_curated_ssot(tmp_path):
    (tmp_path / ".builder" / "specs" / "demo").mkdir(parents=True)
    (tmp_path / ".builder" / "specs" / "demo" / "spec.yaml").write_text("status: verified\ncurrent_phase: sync\n", encoding="utf-8")
    (tmp_path / ".builder" / "specs" / "demo" / "ssot-delta.yaml").write_text("capabilities: []\nbehaviors: []\njourneys: []\n", encoding="utf-8")
    write_host_scope(tmp_path, "demo")
    assert isanna_cli.main(["sync", "--root", str(tmp_path), "--spec", "demo", "--scope-evidence", str(tmp_path / ".builder" / "specs" / "demo" / "sync-scope.yaml")]) == 2
    result = (tmp_path / ".builder" / "specs" / "demo" / "sync-result.yaml").read_text(encoding="utf-8")
    assert "result: bootstrap_required" in result
