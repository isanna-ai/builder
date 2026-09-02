from __future__ import annotations

from pathlib import Path

import isanna
from _sync.evidence import atomic_write_yaml, validate_scope_evidence
from _validators.common import parse_yaml_like_file
from tests.unit.test_sync_readmit_cli import _seed_spec


def test_sync_readmit_then_sync_is_the_only_completion_path(tmp_path: Path):
    spec_dir = _seed_spec(tmp_path, "demo")
    (tmp_path / ".builder" / "sync-adapter.yaml").write_text("artifact: sync-adapter\nmappings: []\n", encoding="utf-8")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "system-behaviors.yaml").write_text("schema: system-behaviors/v1\nbehaviors: []\n", encoding="utf-8")
    assert isanna.main(["sync-readmit", "--root", str(tmp_path), "--spec", "demo"]) == 0
    scope, _ = parse_yaml_like_file(spec_dir / "sync-scope.yaml")
    assert isanna.main(["sync", "--root", str(tmp_path), "--spec", "demo", "--scope-evidence", str(spec_dir / "sync-scope.yaml")]) in {0, 1, 2}
    result, _ = parse_yaml_like_file(spec_dir / "sync-result.yaml")
    assert result["transaction_id"] == scope["transaction_id"]


def test_normal_scope_cannot_introduce_bootstrap_provenance_without_a_committed_report(tmp_path: Path):
    from tests.unit.sync_evidence_support import write_host_scope

    spec_dir = _seed_spec(tmp_path, "demo")
    scope = write_host_scope(tmp_path, "demo", ["src/demo.txt"])
    scope.update(
        provenance="bootstrap-exception",
        owner_authorization="invented",
        derived_baseline=scope["implementation_baseline"],
    )
    atomic_write_yaml(spec_dir / "sync-scope.yaml", scope)
    parsed, errors = validate_scope_evidence(tmp_path, spec_dir)
    assert parsed is None
    assert any("committed readmission set" in error for error in errors)
