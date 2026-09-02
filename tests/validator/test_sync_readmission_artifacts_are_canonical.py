from __future__ import annotations

from pathlib import Path

from _validators.common import ValidationContext
from _validators.sync_artifacts import run_implementation_baseline, run_sync_readmission_report, run_sync_scope
from tests.unit.test_sync_readmit_cli import _seed_spec
from _sync.readmit import readmit_spec


def test_sync_readmission_artifacts_validate_against_canonical_schemas(tmp_path: Path):
    _seed_spec(tmp_path, "demo")
    readmit_spec(tmp_path, "demo")
    spec_dir = tmp_path / ".builder" / "specs" / "demo"
    ctx = ValidationContext(spec_dir=spec_dir)
    assert not run_implementation_baseline(ctx).errors
    assert not run_sync_readmission_report(ctx).errors
    assert not run_sync_scope(ctx).errors
