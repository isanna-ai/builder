from __future__ import annotations

from pathlib import Path
import subprocess

from _dispatch_runtime import gate_evidence
from _sync.readmit import readmit_spec
from _sync.evidence import sha256_bytes, verified_tree_digest
from _validators.common import parse_yaml_like_file
from tests.unit.test_sync_readmit_cli import _seed_spec


def test_sync_readmission_derives_a_baseline_and_transaction(tmp_path: Path):
    _seed_spec(tmp_path, "demo")
    code, _ = readmit_spec(tmp_path, "demo")
    assert code == 0
    baseline, errors = parse_yaml_like_file(tmp_path / ".builder" / "specs" / "demo" / "implementation-baseline.yaml")
    scope, scope_errors = parse_yaml_like_file(tmp_path / ".builder" / "specs" / "demo" / "sync-scope.yaml")
    assert not errors and not scope_errors
    assert baseline["transaction_id"] == scope["transaction_id"]


def test_bootstrap_baseline_is_the_host_post_turn_head_not_the_red_baseline(tmp_path: Path):
    spec_dir = _seed_spec(tmp_path, "demo")
    red_head = subprocess.run(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"], check=True, capture_output=True, text=True,
    ).stdout.strip()
    (tmp_path / "dependency.txt").write_text("dependency\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "dependency.txt"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "post-turn head"], check=True)
    post_head = subprocess.run(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"], check=True, capture_output=True, text=True,
    ).stdout.strip()
    evidence = spec_dir / "gate-evidence"
    baseline_bundle, _ = parse_yaml_like_file(evidence / "0001-red_baseline-plan.yaml")
    verify_bundle, _ = parse_yaml_like_file(evidence / "0002-host_verify-verify.yaml")
    for path in evidence.glob("*.yaml"):
        path.unlink()
    for row in (baseline_bundle, verify_bundle):
        row.update(gate_id="", seq=0, prev_bundle_sha256="", bundle_sha256="")
    verify_bundle["git_head_sha"] = post_head
    assert gate_evidence.write_bundle(evidence, baseline_bundle)
    assert gate_evidence.write_bundle(evidence, verify_bundle)
    readmit_spec(tmp_path, "demo")
    derived, errors = parse_yaml_like_file(spec_dir / "implementation-baseline.yaml")
    assert not errors
    assert red_head != post_head
    scope, scope_errors = parse_yaml_like_file(spec_dir / "sync-scope.yaml")
    assert not scope_errors
    assert scope["derived_baseline"] == post_head
    assert derived["implementation_baseline"] == post_head  # current HEAD in this fixture


def test_immutable_lineage_requires_and_uses_the_exact_manifest(tmp_path: Path):
    spec_dir = _seed_spec(tmp_path, "demo")
    evidence = spec_dir / "gate-evidence"
    baseline_bundle, _ = parse_yaml_like_file(evidence / "0001-red_baseline-plan.yaml")
    verify_bundle, _ = parse_yaml_like_file(evidence / "0002-host_verify-verify.yaml")
    source = tmp_path / "src" / "demo.txt"
    head = verify_bundle["git_head_sha"]
    verify_bundle["verified_snapshot"] = {
        "implementation_baseline": baseline_bundle["git_head_sha"],
        "verified_tree": verified_tree_digest(tmp_path, ["src/demo.txt"], head),
        "path_manifest": [{"path": "src/demo.txt", "sha256": sha256_bytes(source.read_bytes())}],
    }
    for path in evidence.glob("*.yaml"):
        path.unlink()
    for row in (baseline_bundle, verify_bundle):
        row.update(gate_id="", seq=0, prev_bundle_sha256="", bundle_sha256="")
    assert gate_evidence.write_bundle(evidence, baseline_bundle)
    assert gate_evidence.write_bundle(evidence, verify_bundle)
    code, report = readmit_spec(tmp_path, "demo")
    assert code == 0 and report["provenance"] == "immutable"
    assert "owner_authorization" not in report
