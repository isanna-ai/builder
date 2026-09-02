from __future__ import annotations

from pathlib import Path

from _validators.common import parse_yaml_like_file
from _sync.readmit import readmit_spec
from _dispatch_runtime import gate_evidence
from _sync.evidence import atomic_write_yaml, validate_scope_evidence
from tests.unit.test_sync_readmit_cli import _seed_spec


def test_sync_readmission_binds_scope_to_same_transaction(tmp_path: Path):
    _seed_spec(tmp_path, "demo")
    readmit_spec(tmp_path, "demo")
    spec_dir = tmp_path / ".builder" / "specs" / "demo"
    baseline, _ = parse_yaml_like_file(spec_dir / "implementation-baseline.yaml")
    scope, _ = parse_yaml_like_file(spec_dir / "sync-scope.yaml")
    report, _ = parse_yaml_like_file(spec_dir / "sync-readmission-report.yaml")
    assert baseline["transaction_id"] == scope["transaction_id"] == report["transaction_id"]


def test_sync_reader_rejects_a_mixed_or_missing_commit_marker(tmp_path: Path):
    _seed_spec(tmp_path, "demo")
    readmit_spec(tmp_path, "demo")
    spec_dir = tmp_path / ".builder" / "specs" / "demo"
    report_path = spec_dir / "sync-readmission-report.yaml"
    report, _ = parse_yaml_like_file(report_path)
    report["transaction_id"] = "f" * 64
    atomic_write_yaml(report_path, report)
    scope, errors = validate_scope_evidence(tmp_path, spec_dir)
    assert scope is None
    assert any("matching committed report" in error for error in errors)


def test_sync_reader_rejects_unknown_fields_in_the_enriched_verify_bundle(tmp_path: Path):
    spec_dir = _seed_spec(tmp_path, "demo")
    readmit_spec(tmp_path, "demo")
    scope_path = spec_dir / "sync-scope.yaml"
    report_path = spec_dir / "sync-readmission-report.yaml"
    scope, _ = parse_yaml_like_file(scope_path)
    report, _ = parse_yaml_like_file(report_path)
    bundle_path = spec_dir / scope["verify_gate_bundle"]
    bundle, _ = parse_yaml_like_file(bundle_path)
    bundle["unexpected"] = True
    bundle["bundle_sha256"] = gate_evidence.bundle_sha(bundle)
    scope["verify_gate_sha256"] = bundle["bundle_sha256"]
    report["verify_gate_sha256"] = bundle["bundle_sha256"]
    atomic_write_yaml(bundle_path, bundle)
    atomic_write_yaml(scope_path, scope)
    atomic_write_yaml(report_path, report)
    parsed, errors = validate_scope_evidence(tmp_path, spec_dir)
    assert parsed is None
    assert any("unknown fields" in error for error in errors)
