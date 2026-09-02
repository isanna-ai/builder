from __future__ import annotations

from pathlib import Path

import _sync.readmit as readmit_mod
from _sync.locking import spec_mutation_lock
from _sync.readmit import ReadmitFailure, readmit_spec
from _validators.common import parse_yaml_like_file
from tests.unit.test_sync_readmit_cli import _seed_spec


def test_sync_readmission_preserves_prior_evidence_on_publish_failure(tmp_path: Path):
    spec_dir = _seed_spec(tmp_path, "demo")
    prior = "keep: true\n"
    (spec_dir / "sync-readmission-report.yaml").write_text(prior, encoding="utf-8")

    def boom(_files):
        raise RuntimeError("boom")

    original = readmit_mod.atomic_publish
    readmit_mod.atomic_publish = boom
    try:
        try:
            readmit_spec(tmp_path, "demo")
        except RuntimeError:
            pass
        else:
            assert False
    finally:
        readmit_mod.atomic_publish = original
    assert (spec_dir / "sync-readmission-report.yaml").read_text(encoding="utf-8") == prior


def test_sync_readmission_refuses_concurrent_spec_mutation(tmp_path: Path):
    _seed_spec(tmp_path, "demo")
    with spec_mutation_lock(tmp_path, "demo", blocking=False, owner="test"):
        try:
            readmit_spec(tmp_path, "demo")
        except ReadmitFailure as exc:
            assert exc.code == "mutation-contention"
        else:
            assert False


def test_sync_readmission_publishes_the_report_commit_marker_last(tmp_path: Path):
    _seed_spec(tmp_path, "demo")
    observed = []

    def capture(files):
        observed.extend(path.name for path in files)

    original = readmit_mod.atomic_publish
    readmit_mod.atomic_publish = capture
    try:
        readmit_spec(tmp_path, "demo")
    finally:
        readmit_mod.atomic_publish = original
    assert observed[-1] == "sync-readmission-report.yaml"
    assert observed[:2] == ["implementation-baseline.yaml", "sync-scope.yaml"]
    assert "host_verify-verify.yaml" in observed[2]


def test_sync_readmission_validates_every_staged_artifact_schema_and_binding(tmp_path: Path):
    spec_dir = _seed_spec(tmp_path, "demo")
    readmit_spec(tmp_path, "demo")
    baseline, _ = parse_yaml_like_file(spec_dir / "implementation-baseline.yaml")
    scope, _ = parse_yaml_like_file(spec_dir / "sync-scope.yaml")
    report, _ = parse_yaml_like_file(spec_dir / "sync-readmission-report.yaml")
    bundle, _ = parse_yaml_like_file(spec_dir / scope["verify_gate_bundle"])
    baseline["unexpected"] = True
    try:
        readmit_mod._validate_staged(baseline, scope, report, bundle)
    except ReadmitFailure as exc:
        assert exc.code == "invalid-staged-artifact"
    else:
        assert False
