from __future__ import annotations

from pathlib import Path

from _sync.readmit import readmit_spec
from _dispatch_runtime import gate_evidence
from _validators.common import parse_yaml_like_file
from tests.unit.test_sync_readmit_cli import _seed_spec


def test_sync_readmission_publishes_baseline_scope_and_report(tmp_path: Path):
    _seed_spec(tmp_path, "demo")
    readmit_spec(tmp_path, "demo")
    spec_dir = tmp_path / ".builder" / "specs" / "demo"
    for name in ("implementation-baseline.yaml", "sync-scope.yaml", "sync-readmission-report.yaml"):
        assert (spec_dir / name).is_file()
    scope, errors = parse_yaml_like_file(spec_dir / "sync-scope.yaml")
    assert not errors
    fresh, fresh_errors = parse_yaml_like_file(spec_dir / scope["verify_gate_bundle"])
    assert not fresh_errors
    assert fresh["transaction_id"] == scope["transaction_id"]
    assert fresh["verified_snapshot"]["changed_paths_digest"] == scope["changed_paths_digest"]
    assert fresh["bundle_sha256"] == gate_evidence.bundle_sha(fresh)
    assert not (tmp_path / ".builder" / "readmission-worktrees").exists()
