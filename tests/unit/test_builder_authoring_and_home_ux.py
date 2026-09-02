from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import subprocess
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from _builder_project_model.attribution import AdmissionStore, Membership, resolve_admission_repo
from _builder_project_model.authoring import emit_declaration_patch_handoff, load_authoring_context
from _builder_project_model.editors import plan_backlog_edit
from _builder_project_model.home import load_builder_home
from _dispatch_runtime.paths import runtime_dir
from _dispatch_runtime.queue_store import QueueStore


def _load_isanna():
    spec = importlib.util.spec_from_file_location("isanna_phase5_test", SCRIPTS / "isanna.py")
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _repo(root: Path, name: str) -> Path:
    repo = root / name
    (repo / ".git").mkdir(parents=True)
    return repo


def _seed_home(tmp_path: Path):
    home = tmp_path / ".builder-home"
    alpha = _repo(tmp_path, "alpha-repo")
    beta = _repo(tmp_path, "beta-repo")
    _write(runtime_dir(alpha) / "specs" / "core" / "spec.yaml", "status: planned\n")
    _write(runtime_dir(beta) / "specs" / "shared" / "spec.yaml", "status: planned\n")
    _write(runtime_dir(alpha) / "intents" / "wave-1-work" / "intent.yaml", textwrap.dedent(
        """\
        artifact: intent-object
        intent: wave-1-work
        title: Wave 1 work
        status: accepted
        problem: p
        why: w
        success_criteria:
          - id: sc-1
            statement: s
        non_goals:
          - n
        ssot_delta:
          capabilities: []
          behaviors: []
          journeys: []
        specs:
          - alpha/core
          - beta/shared
        """
    ))
    QueueStore(runtime_dir(alpha) / "dispatch-queue")
    _write(home / "builder.yaml", textwrap.dedent(
        """\
        schema_version: 1
        home_id: phase5
        repositories: repositories.yaml
        policy: policy.yaml
        projects:
          - id: portfolio
            manifest: projects/portfolio/product.yaml
        """
    ))
    _write(home / "repositories.yaml", textwrap.dedent(
        """\
        schema_version: 1
        repos:
          - id: alpha-repo
            path: ../alpha-repo
          - id: beta-repo
            path: ../beta-repo
        """
    ))
    _write(home / "policy.yaml", textwrap.dedent(
        """\
        schema_version: 1
        governor:
          enabled: false
        providers:
          claude-code-cli:
            max_sessions: 2
            quota_cooldown:
              initial_seconds: 300
              max_seconds: 3600
          codex-cli:
            max_sessions: 2
            quota_cooldown:
              initial_seconds: 300
              max_seconds: 3600
        allocation:
          policy: equal-weight-fair-share
          project_weight: 1
        scheduler:
          poll_seconds: 2
          heartbeat_seconds: 5
          stale_daemon_seconds: 30
        """
    ))
    _write(home / "projects" / "portfolio" / "product.yaml", textwrap.dedent(
        """\
        schema_version: 1
        product: portfolio
        title: Portfolio
        description: Shared portfolio
        default_repo: alpha-repo
        repos:
          - alias: alpha
            repo_id: alpha-repo
          - alias: beta
            repo_id: beta-repo
        backlog:
          - alpha/future-work
        releases:
          - name: wave-1
            manifest: releases/wave-1.yaml
        """
    ))
    _write(home / "projects" / "portfolio" / "releases" / "wave-1.yaml", textwrap.dedent(
        """\
        schema_version: 1
        name: wave-1
        description: Synthetic release
        status: active
        intents:
          - wave-1-work
        """
    ))
    return load_builder_home(home), alpha, beta


def _run_isanna(argv: list[str]) -> tuple[int, str]:
    isanna = _load_isanna()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = isanna.main(argv)
    return code, buf.getvalue()


def _snapshot_tree(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_admission_resolution_returns_exactly_one_repo_and_rejects_ambiguity(tmp_path):
    home, _alpha, _beta = _seed_home(tmp_path)
    receipts = AdmissionStore(home.root)
    receipts.write_receipt(
        admission_id="adm-1",
        repo_id="alpha-repo",
        spec_id="core",
        project_id="portfolio",
        release_name="wave-1",
        roadmap_index=0,
        work_id="w1",
        attempt_id=None,
        membership=Membership(project_id="portfolio", release_name="wave-1", roadmap_index=0),
    )
    assert resolve_admission_repo(home.root, admission_id="adm-1") == "alpha-repo"

    path = receipts.path_for("shadow")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"admission_id": "adm-ambiguous", "repo_id": "alpha-repo"}), encoding="utf-8")
    (path.parent / "shadow-2.json").write_text(json.dumps({"admission_id": "adm-ambiguous", "repo_id": "beta-repo"}), encoding="utf-8")
    try:
        resolve_admission_repo(home.root, admission_id="adm-ambiguous")
    except ValueError as exc:
        assert "ambiguous" in str(exc)
    else:
        raise AssertionError("expected ambiguous admission rejection")


def test_admission_resolution_rejects_zero_matching_repos(tmp_path):
    home, _alpha, _beta = _seed_home(tmp_path)

    try:
        resolve_admission_repo(home.root, admission_id="missing")
    except ValueError as exc:
        assert "did not resolve to any repo" in str(exc)
    else:
        raise AssertionError("expected missing admission rejection")


def test_repo_register_preview_writes_nothing_until_confirm(tmp_path):
    home, _alpha, _beta = _seed_home(tmp_path)
    code, preview = _run_isanna([
        "home", "repo-register",
        "--home", str(home.root),
        "--repo-id", "gamma-repo",
        "--path", "../gamma-repo",
    ])
    assert code == 0
    assert "Selected home:" in preview
    assert "dry-run only" in preview
    assert "gamma-repo" not in (home.root / "repositories.yaml").read_text(encoding="utf-8")

    _repo(tmp_path, "gamma-repo")
    code, applied = _run_isanna([
        "home", "repo-register",
        "--home", str(home.root),
        "--repo-id", "gamma-repo",
        "--path", "../gamma-repo",
        "--confirm",
    ])
    assert code == 0
    assert "registered repo gamma-repo" in applied
    assert "gamma-repo" in (home.root / "repositories.yaml").read_text(encoding="utf-8")


def test_backlog_edit_preview_and_confirm_follow_exact_plan(tmp_path):
    home, _alpha, _beta = _seed_home(tmp_path)
    product_path = home.root / "projects" / "portfolio" / "product.yaml"
    before = product_path.read_text(encoding="utf-8")
    code, preview = _run_isanna([
        "home", "backlog-edit",
        "--home", str(home.root),
        "--project", "portfolio",
        "--backlog", "alpha/core,beta/shared",
    ])
    assert code == 0
    assert "--- " in preview and "+++ " in preview
    assert product_path.read_text(encoding="utf-8") == before

    code, applied = _run_isanna([
        "home", "backlog-edit",
        "--home", str(home.root),
        "--project", "portfolio",
        "--backlog", "alpha/core,beta/shared",
        "--confirm",
    ])
    assert code == 0
    assert "updated backlog for portfolio" in applied
    assert "beta/shared" in product_path.read_text(encoding="utf-8")


def test_project_release_lifecycle_and_unregister_editors_preview_before_confirm(tmp_path):
    home, _alpha, _beta = _seed_home(tmp_path)
    product_path = home.root / "projects" / "portfolio" / "product.yaml"
    release_path = home.root / "projects" / "portfolio" / "releases" / "wave-1.yaml"
    repositories_path = home.root / "repositories.yaml"
    commands = [
        (
            [
                "home", "release-edit", "--home", str(home.root), "--project", "portfolio",
                "--release", "wave-1", "--specs", "alpha/core", "--release-status", "shipped",
            ],
            release_path,
            ("status: shipped", "beta/shared"),
        ),
        (
            [
                "home", "project-edit", "--home", str(home.root), "--project", "portfolio",
                "--title", "Updated Portfolio", "--repo", "alpha=alpha-repo",
            ],
            product_path,
            ("title: Updated Portfolio", "beta-repo"),
        ),
        (
            ["home", "repo-unregister", "--home", str(home.root), "--repo-id", "beta-repo"],
            repositories_path,
            (None, "beta-repo"),
        ),
    ]

    for command, target, (added, removed) in commands:
        before = target.read_text(encoding="utf-8")
        code, preview = _run_isanna(command)
        assert code == 0
        assert f"Selected home: {home.root}" in preview
        assert "--- " in preview and "+++ " in preview
        assert "dry-run only" in preview
        assert target.read_text(encoding="utf-8") == before

        code, _applied = _run_isanna([*command, "--confirm"])
        assert code == 0
        after = target.read_text(encoding="utf-8")
        if added is not None:
            assert added in after
        assert removed not in after


def test_editor_strict_parse_rejects_unknown_keys_before_write(tmp_path):
    home, _alpha, _beta = _seed_home(tmp_path)
    product_path = home.root / "projects" / "portfolio" / "product.yaml"
    product_path.write_text(product_path.read_text(encoding="utf-8") + "owner: bad\n", encoding="utf-8")
    try:
        _run_isanna([
            "home", "backlog-edit",
            "--home", str(home.root),
            "--project", "portfolio",
            "--backlog", "alpha/core",
        ])
    except Exception as exc:
        assert "unknown key 'owner'" in str(exc)
    else:
        raise AssertionError("expected strict parse failure")


def test_home_status_and_dispatch_plan_are_read_only(tmp_path):
    home, alpha, _beta = _seed_home(tmp_path)
    queue_root = runtime_dir(alpha) / "dispatch-queue"
    before_ids = list(QueueStore(queue_root).reconstruct().items)
    before_tree = _snapshot_tree(tmp_path)

    code, status = _run_isanna(["home", "status", "--home", str(home.root)])
    assert code == 0
    assert "Selected home:" in status
    assert "lint: clean" in status

    code, plan = _run_isanna(["home", "dispatch-plan", "--home", str(home.root), "--project", "portfolio", "--release", "wave-1"])
    assert code == 0
    assert "Dispatch plan: portfolio/wave-1" in plan
    assert "repo=alpha-repo" in plan and "repo=beta-repo" in plan
    after_ids = list(QueueStore(queue_root).reconstruct().items)
    assert after_ids == before_ids
    assert _snapshot_tree(tmp_path) == before_tree


def test_authoring_context_is_read_only_and_patch_is_text_only(tmp_path):
    home, alpha, _beta = _seed_home(tmp_path)
    context = load_authoring_context(repo_root=alpha, home=home)
    assert context is not None
    assert context.project_id == "portfolio"
    assert context.backlog == ("alpha/future-work",)
    standalone = tmp_path / "standalone"
    standalone.mkdir()
    assert load_authoring_context(repo_root=standalone) is None

    product_path = home.root / "projects" / "portfolio" / "product.yaml"
    before = product_path.read_text(encoding="utf-8")
    preview = plan_backlog_edit(home=home, project_id="portfolio", backlog=["alpha/core", "beta/shared"])
    handoff = emit_declaration_patch_handoff(preview=preview)
    assert "Declaration patch handoff" in handoff
    assert "--- " in handoff and "+++ " in handoff
    assert product_path.read_text(encoding="utf-8") == before


def test_installer_offers_home_init_but_does_not_create_home(tmp_path):
    repo = _repo(tmp_path, "install-target")
    proc = subprocess.run(
        ["sh", str(ROOT / "install.sh"), "--target", str(repo), "--yes"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=True,
    )
    out = proc.stdout
    assert "isanna home init --projects-root" in out
    assert not (repo / ".builder-home").exists()
    # The offer has to be ACTIONABLE. `isanna` is not installed by this installer -- it ships
    # with the repository -- and "Builder Home" means nothing to a first-time reader, so a bare
    # command here sends them to a shell that answers "command not found". Printing the offer is
    # the SSOT contract; printing it without these two facts is a dead end.
    assert "builder repository" in out, "the offer must say where the isanna CLI comes from"
    assert "--confirm" in out, "the offer must show that nothing is created without --confirm"
    # The line exists to be COPIED. An unquoted path breaks the moment a target contains a space,
    # which is ordinary on macOS ("~/My Projects") -- found by installing into one.
    assert f"--projects-root '{repo}'" in out, "the printed path must be quoted for copy-paste"
