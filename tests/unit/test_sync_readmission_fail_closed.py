from __future__ import annotations

from pathlib import Path
import shutil

from _dispatch_runtime import gate_evidence
from _sync.readmit import ReadmitFailure, readmit_spec
from _validators.common import ValidationContext, parse_yaml_like_file
from _validators.sync_artifacts import run_sync_readmission_report
from tests.unit.test_sync_readmit_cli import _seed_spec


def test_sync_readmission_rejects_ambiguous_lineage(tmp_path: Path):
    spec_dir = _seed_spec(tmp_path, "demo")
    original, errors = parse_yaml_like_file(spec_dir / "gate-evidence" / "0002-host_verify-verify.yaml")
    assert not errors
    original["gate_id"] = ""
    original["seq"] = 0
    original["prev_bundle_sha256"] = ""
    original["bundle_sha256"] = ""
    assert gate_evidence.write_bundle(spec_dir / "gate-evidence", original) is not None
    try:
        readmit_spec(tmp_path, "demo")
    except ReadmitFailure as exc:
        assert exc.code == "ambiguous-lineage"
    else:
        assert False


def test_sync_readmission_rejects_a_missing_host_chain_even_with_agent_scope_hints(tmp_path: Path):
    spec_dir = _seed_spec(tmp_path, "demo")
    for path in (spec_dir / "gate-evidence").glob("*.yaml"):
        path.unlink()
    (spec_dir / "phase-log.yaml").write_text("phases:\n  - files_written: [src/demo.txt]\n", encoding="utf-8")
    try:
        readmit_spec(tmp_path, "demo")
    except ReadmitFailure as exc:
        assert exc.code == "broken-evidence-chain"
        assert "empty" in exc.detail
    else:
        assert False
    report, errors = parse_yaml_like_file(spec_dir / "sync-readmission-report.yaml")
    assert not errors
    assert report == {
        "schema": "sync-readmission-report/v1",
        "spec": "demo",
        "status": "blocked",
        "result_code": "broken-evidence-chain",
        "detail": "gate-evidence directory empty",
    }
    assert not run_sync_readmission_report(ValidationContext(spec_dir=spec_dir)).errors


def test_sync_readmission_rejects_a_symlinked_spec_directory_without_touching_its_target(tmp_path: Path):
    spec_dir = _seed_spec(tmp_path, "demo")
    outside = tmp_path / "outside-spec"
    shutil.move(str(spec_dir), outside)
    spec_dir.symlink_to(outside, target_is_directory=True)
    try:
        readmit_spec(tmp_path, "demo")
    except ReadmitFailure as exc:
        assert exc.code == "unsafe-spec-path"
    else:
        assert False
    assert not (outside / "sync-readmission-report.yaml").exists()


def test_sync_readmission_rejects_a_symlinked_evidence_directory(tmp_path: Path):
    spec_dir = _seed_spec(tmp_path, "demo")
    evidence = spec_dir / "gate-evidence"
    outside = tmp_path / "outside-evidence"
    shutil.move(str(evidence), outside)
    evidence.symlink_to(outside, target_is_directory=True)
    before = sorted(path.name for path in outside.glob("*.yaml"))
    try:
        readmit_spec(tmp_path, "demo")
    except ReadmitFailure as exc:
        assert exc.code == "unsafe-evidence-directory"
    else:
        assert False
    assert sorted(path.name for path in outside.glob("*.yaml")) == before


def test_sync_readmission_rejects_cross_spec_bundle_ownership(tmp_path: Path):
    spec_dir = _seed_spec(tmp_path, "demo")
    evidence = spec_dir / "gate-evidence"
    baseline, _ = parse_yaml_like_file(evidence / "0001-red_baseline-plan.yaml")
    verify, _ = parse_yaml_like_file(evidence / "0002-host_verify-verify.yaml")
    for path in evidence.glob("*.yaml"):
        path.unlink()
    for row in (baseline, verify):
        row.update(gate_id="", seq=0, prev_bundle_sha256="", bundle_sha256="")
    verify["spec_id"] = "other"
    assert gate_evidence.write_bundle(evidence, baseline)
    assert gate_evidence.write_bundle(evidence, verify)
    try:
        readmit_spec(tmp_path, "demo")
    except ReadmitFailure as exc:
        assert exc.code == "cross-spec-evidence"
    else:
        assert False
