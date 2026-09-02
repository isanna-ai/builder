from __future__ import annotations

from pathlib import Path

from _validators.common import ValidationContext, load_schema, validate_schema
from _validators.sync_artifacts import run_sync_readmission_report
from tests.unit.test_sync_readmit_cli import _seed_spec
from _sync.readmit import readmit_spec
from _validators.common import parse_yaml_like_file


def test_sync_readmission_report_is_canonical(tmp_path: Path):
    _seed_spec(tmp_path, "demo")
    code, _ = readmit_spec(tmp_path, "demo")
    assert code == 0
    data_path = tmp_path / ".builder" / "specs" / "demo" / "sync-readmission-report.yaml"
    data, errors = parse_yaml_like_file(data_path)
    schema, schema_errors = load_schema("sync-readmission-report.schema.yaml")
    assert not errors and not schema_errors
    assert not validate_schema(data, schema, "sync-readmission-report.yaml")
    assert not run_sync_readmission_report(ValidationContext(spec_dir=data_path.parent)).errors
