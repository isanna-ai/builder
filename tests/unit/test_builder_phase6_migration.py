from __future__ import annotations

import importlib.util
import sys
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import planning
from _builder_project_model.home import load_builder_home
from _builder_project_model.importer import apply_import_preview, preview_bia_import
from _builder_project_model.init import apply_plan, scaffold_home
from _builder_project_model.migration import fingerprint_runtime_tree, verify_bia_import


def _load_isanna():
    spec = importlib.util.spec_from_file_location("isanna_phase6_migration", SCRIPTS / "isanna.py")
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _repo(root: Path, name: str) -> Path:
    repo = root / name
    (repo / ".git").mkdir(parents=True)
    return repo


def _seed_legacy_bia(root: Path) -> Path:
    repo = _repo(root, "hivemind-cloud")
    runtime = repo / ".builder"
    _write(
        runtime / "product.yaml",
        "product: bia\n"
        "title: Bia\n"
        "repos:\n"
        "  - alias: hivemind-cloud\n",
    )
    _write(
        runtime / "releases" / "bia-audit-remediation.yaml",
        "release: bia-audit-remediation\n"
        "product: bia\n"
        "title: Bia audit remediation\n"
        "goal: Remediate the confirmed audit findings\n"
        "status: draft\n"
        "specs:\n"
        "  - spec: audit-inventory\n"
        "  - spec: audit-log-retention\n",
    )
    _write(runtime / "specs" / "audit-inventory" / "spec.yaml", "status: specified\n")
    _write(runtime / "specs" / "audit-log-retention" / "spec.yaml", "status: planned\n")
    _write(runtime / "specs" / "audit-inventory" / "evidence" / "host.txt", "host-evidence\n")
    _write(runtime / "dispatch-queue" / "queue" / "attempts" / "attempt-1.yaml", "attempt_id: attempt-1\n")
    _write(runtime / "dispatch-queue" / "queue" / "items" / "work-1.yaml", "work_id: work-1\n")
    return repo


def _seed_scan_cache(repo: Path) -> None:
    planning._scan_cache[str(repo.resolve())] = {
        "specs": [
            {"spec": "audit-inventory", "verification": "host-verified"},
            {"spec": "audit-log-retention", "verification": "self-reported"},
        ]
    }


def test_confirmed_import_verification_matches_legacy_and_leaves_runtime_bytes_unchanged(tmp_path):
    source_root = _seed_legacy_bia(tmp_path)
    _seed_scan_cache(source_root)
    before = fingerprint_runtime_tree(source_root)

    apply_plan(scaffold_home(projects_root=tmp_path, home_id="sol"))
    home = load_builder_home(tmp_path / ".builder-home")
    apply_import_preview(preview_bia_import(home=home, source_root=source_root))

    report = verify_bia_import(home_dir=tmp_path / ".builder-home", source_root=source_root)

    assert report.success, report
    assert report.legacy_releases == report.home_releases
    assert before == report.protected_before == report.protected_after


def test_import_verification_fails_loudly_when_canonical_membership_drifts(tmp_path):
    source_root = _seed_legacy_bia(tmp_path)
    _seed_scan_cache(source_root)

    apply_plan(scaffold_home(projects_root=tmp_path, home_id="sol"))
    home = load_builder_home(tmp_path / ".builder-home")
    apply_import_preview(preview_bia_import(home=home, source_root=source_root))
    release_path = tmp_path / ".builder-home" / "projects" / "bia" / "releases" / "bia-audit-remediation.yaml"
    release_path.write_text(
        "schema_version: 1\n"
        "name: bia-audit-remediation\n"
        "description: Remediate the confirmed audit findings\n"
        "status: draft\n"
        "specs:\n"
        "  - spec: audit-inventory\n"
        "  - spec: audit-log-retention\n"
        "  - spec: audit-extra\n",
        encoding="utf-8",
    )

    report = verify_bia_import(home_dir=tmp_path / ".builder-home", source_root=source_root)

    assert not report.success
    assert "legacy-vs-home release equivalence mismatch" in report.findings


def test_isanna_import_bia_verify_record_runs_fixture_only_post_write_checks(tmp_path):
    isanna = _load_isanna()
    source_root = _seed_legacy_bia(tmp_path)
    _seed_scan_cache(source_root)

    stdout = StringIO()
    stderr = StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        assert isanna.main(["home", "init", "--projects-root", str(tmp_path), "--home-id", "sol", "--confirm"]) == 0
        rc = isanna.main(
            [
                "home",
                "import-bia",
                "--home",
                str(tmp_path / ".builder-home"),
                "--source-root",
                str(source_root),
                "--confirm",
                "--verify-record",
            ]
        )

    assert rc == 0
    assert stderr.getvalue() == ""
    assert "record-equivalence: ok" in stdout.getvalue()
    assert "runtime-bytes: unchanged" in stdout.getvalue()
