from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
from unittest import SkipTest

# The live full-repo proof re-clones the real repository and replays the historical
# whole-suite verify command. Per the locked design (R6/R7) the hermetic pytest suite must
# not run it; the real live proof is a separate human-only `sync-readmit` then `sync`
# invocation, opted into explicitly with BUILDER_LIVE_READMISSION_PROOF=1.
_LIVE_PROOF_OPT_IN = "BUILDER_LIVE_READMISSION_PROOF"

import isanna
from _sync.readmit import readmit_spec
from _sync.evidence import result_is_corroborated
from _validators.common import parse_yaml_like_file
from tests.unit.test_sync_readmit_cli import _seed_spec


def test_sync_readmission_end_to_end_hermetic_copy(tmp_path: Path):
    spec_dir = _seed_spec(tmp_path, "intent-release-membership-cutover")
    spec_dir.joinpath("ssot-delta.yaml").write_text(
        "capabilities:\n  - target: sync-readmission-proof\n    change: create\nbehaviors: []\njourneys: []\n",
        encoding="utf-8",
    )
    (tmp_path / ".builder" / "sync-adapter.yaml").write_text(
        "artifact: sync-adapter\nmappings:\n  - paths: [src/*.txt]\n    tuples:\n"
        "      - category: capabilities\n        target: sync-readmission-proof\n        change: create\n",
        encoding="utf-8",
    )
    (tmp_path / "docs").mkdir()
    (tmp_path / "tests" / "unit").mkdir(parents=True)
    (tmp_path / "tests" / "unit" / "test_guard.py").write_text("def test_readmission_guard():\n    pass\n", encoding="utf-8")
    (tmp_path / "Makefile").write_text("gate:\n\tpytest tests/unit/test_guard.py -q\n", encoding="utf-8")
    (tmp_path / "docs" / "system-behaviors.yaml").write_text(
        "schema: system-behaviors/v1\nbehaviors:\n  - id: readmission-proof\n    area: sync\n"
        "    behavior: readmission proof\n    invariant: guarded\n    breaks_when: guard fails\n"
        "    guarding_tests:\n      - tests/unit/test_guard.py::test_readmission_guard\n",
        encoding="utf-8",
    )
    code, _ = readmit_spec(tmp_path, "intent-release-membership-cutover")
    assert code == 0
    report, errors = parse_yaml_like_file(
        tmp_path / ".builder" / "specs" / "intent-release-membership-cutover" / "sync-readmission-report.yaml"
    )
    assert not errors and report["provenance"] == "bootstrap-exception"
    assert isanna.main([
        "sync", "--root", str(tmp_path), "--spec", "intent-release-membership-cutover",
        "--scope-evidence", str(spec_dir / "sync-scope.yaml"),
    ]) == 0
    result, result_errors = parse_yaml_like_file(spec_dir / "sync-result.yaml")
    spec, spec_errors = parse_yaml_like_file(spec_dir / "spec.yaml")
    assert not result_errors and not spec_errors
    assert result["result"] == "synced" and result["provenance"] == "bootstrap-exception"
    assert result["owner_authorization"] == report["owner_authorization"]
    assert result["derived_baseline"] == report["derived_baseline"]
    assert result_is_corroborated(spec_dir, result)
    assert spec["status"] == "synced"


def test_canonical_proof_spec_chain_is_readmitted_only_in_a_hermetic_clone(tmp_path: Path):
    if os.environ.get(_LIVE_PROOF_OPT_IN) != "1":
        raise SkipTest(
            "live full-repo readmission proof is human-only; "
            f"set {_LIVE_PROOF_OPT_IN}=1 to run the real sync-readmit -> sync clone proof"
        )
    repo = Path(__file__).resolve().parents[2]
    common = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--path-format=absolute", "--git-common-dir"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    control_root = Path(common).parent
    canonical = control_root / ".builder" / "specs" / "intent-release-membership-cutover"
    assert canonical.is_dir()
    clone = tmp_path / "proof-copy"
    subprocess.run(["git", "clone", "-q", "--no-hardlinks", str(control_root), str(clone)], check=True)
    copied_spec = clone / ".builder" / "specs" / canonical.name
    if copied_spec.exists():
        shutil.rmtree(copied_spec)
    shutil.copytree(canonical, copied_spec)
    # Runtime-only historical products are intentionally removed; readmission must derive them anew.
    for name in ("implementation-baseline.yaml", "sync-scope.yaml", "sync-result.yaml", "sync-readmission-report.yaml"):
        copied_spec.joinpath(name).unlink(missing_ok=True)
    before = {
        str(path.relative_to(canonical)): path.read_bytes()
        for path in canonical.rglob("*") if path.is_file() and not path.is_symlink()
    }
    # Recreate the target-owned retained source worktree inside the clone. Only its source
    # paths are used to select the per-spec readmission scope; all canonical state remains copied.
    source_worktree = control_root / ".builder" / "worktrees" / canonical.name
    verify_bundle, errors = parse_yaml_like_file(canonical / "gate-evidence" / "0003-host_verify-verify.yaml")
    assert not errors
    target_worktree = clone / ".builder" / "worktrees" / canonical.name
    target_worktree.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([
        "git", "-C", str(clone), "worktree", "add", "--detach", str(target_worktree),
        verify_bundle["git_head_sha"],
    ], check=True, capture_output=True)
    changed = subprocess.run(
        ["git", "-C", str(source_worktree), "diff", "--name-only", "-z", verify_bundle["git_head_sha"], "--"],
        check=True, capture_output=True,
    ).stdout
    for raw in changed.split(b"\0"):
        if not raw:
            continue
        rel = raw.decode("utf-8")
        if rel.startswith(".builder/"):
            continue
        source = source_worktree / rel
        destination = target_worktree / rel
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)

    code, report = readmit_spec(clone, canonical.name)
    assert code == 0 and report["provenance"] == "bootstrap-exception"
    assert isanna.main(["sync", "--root", str(clone), "--spec", canonical.name]) == 0
    result, result_errors = parse_yaml_like_file(copied_spec / "sync-result.yaml")
    assert not result_errors and result["result"] == "synced"
    after = {
        str(path.relative_to(canonical)): path.read_bytes()
        for path in canonical.rglob("*") if path.is_file() and not path.is_symlink()
    }
    assert after == before
