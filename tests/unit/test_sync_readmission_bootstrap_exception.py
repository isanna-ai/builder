from __future__ import annotations

from pathlib import Path
from unittest import SkipTest

from _dispatch_runtime import gate_evidence
from _sync.readmit import BOOTSTRAP_AUTHORIZATION, ReadmitFailure, readmit_spec
from _validators.common import parse_yaml_like_file
from tests.unit.test_sync_readmit_cli import _seed_spec


def test_sync_readmission_records_bootstrap_exception_verbatim(tmp_path: Path):
    _seed_spec(tmp_path, "demo")
    code, _ = readmit_spec(tmp_path, "demo")
    assert code == 0
    scope, errors = parse_yaml_like_file(tmp_path / ".builder" / "specs" / "demo" / "sync-scope.yaml")
    assert not errors
    assert scope["provenance"] == "bootstrap-exception"
    assert scope["owner_authorization"] == BOOTSTRAP_AUTHORIZATION
    assert BOOTSTRAP_AUTHORIZATION.endswith(
        "Proceed to planning with sc-3/R6 satisfiable under this mechanism; R3 fail-closed rules stay intact for all non-bootstrap evidence."
    )
    report, report_errors = parse_yaml_like_file(tmp_path / ".builder" / "specs" / "demo" / "sync-readmission-report.yaml")
    assert not report_errors
    assert report["derived_baseline"] == scope["derived_baseline"] == scope["implementation_baseline"]
    assert report["source_evidence"]["dirty_tree_summary"]["files"] == ["src/demo.txt"]


def test_bootstrap_authorization_constant_matches_the_locked_requirement_byte_for_byte():
    # Repo-root-relative (not CWD-relative) and skip-if-absent: sync-readmission-tool's spec
    # provenance is local (uncommitted) state, so it is not present in a fresh clone or an
    # isolated dispatch worktree. On main the drift check runs; elsewhere it skips.
    req = Path(__file__).resolve().parents[2] / ".builder/specs/sync-readmission-tool/requirements.yaml"
    if not req.is_file():
        raise SkipTest("sync-readmission-tool requirements.yaml absent in this checkout (worktree / fresh clone)")
    requirements, errors = parse_yaml_like_file(req)
    assert not errors
    criterion = next(
        acceptance
        for requirement in requirements["requirements"]
        for acceptance in requirement["acceptance"]
        if acceptance["id"] == "AC-R2-6"
    )
    statement = criterion["statement"]
    locked = statement.split("authorization text byte-for-byte: `", 1)[1].rsplit("`", 1)[0]
    assert BOOTSTRAP_AUTHORIZATION == locked


def test_sync_readmission_consumes_bootstrap_exception_once(tmp_path: Path):
    _seed_spec(tmp_path, "demo")
    assert readmit_spec(tmp_path, "demo")[0] == 0
    try:
        readmit_spec(tmp_path, "demo")
    except ReadmitFailure as exc:
        assert exc.code == "bootstrap-exception-expired"
    else:
        assert False


def test_bootstrap_exception_rejects_bundle_after_cutoff(tmp_path: Path):
    spec_dir = _seed_spec(tmp_path, "demo")
    evidence = spec_dir / "gate-evidence"
    baseline, _ = parse_yaml_like_file(evidence / "0001-red_baseline-plan.yaml")
    verify, _ = parse_yaml_like_file(evidence / "0002-host_verify-verify.yaml")
    for path in evidence.glob("*.yaml"):
        path.unlink()
    for row in (baseline, verify):
        row.update(gate_id="", seq=0, prev_bundle_sha256="", bundle_sha256="")
    verify["finished_at"] = "2026-07-21T00:00:00Z"
    assert gate_evidence.write_bundle(evidence, baseline)
    assert gate_evidence.write_bundle(evidence, verify)
    try:
        readmit_spec(tmp_path, "demo")
    except ReadmitFailure as exc:
        assert exc.code == "missing-immutable-identity"
    else:
        assert False
