from __future__ import annotations

import importlib.util
import io
import sys
import textwrap
from pathlib import Path
from contextlib import redirect_stdout

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
from tests.unit.public_export_support import require_repo_asset


def _load_group_runner():
    require_repo_asset(
        ROOT, "scripts/builder-group-runner.py", "the legacy group-runner ownership guard"
    )
    spec = importlib.util.spec_from_file_location("phase7_group_runner", SCRIPTS / "builder-group-runner.py")
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


def _policy_yaml() -> str:
    return textwrap.dedent(
        """\
        schema_version: 1
        governor:
          enabled: true
          drain_repos:
            - alpha-repo
        providers:
          claude-code-cli:
            max_sessions: 1
            quota_cooldown:
              initial_seconds: 300
              max_seconds: 3600
          codex-cli:
            max_sessions: 1
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
    )


def _seed_owned_home(tmp_path: Path) -> Path:
    repo = _repo(tmp_path, "alpha-repo")
    home = tmp_path / ".builder-home"
    _write(home / "builder.yaml", textwrap.dedent(
        """\
        schema_version: 1
        home_id: phase7
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
        """
    ))
    _write(home / "policy.yaml", _policy_yaml())
    _write(home / "projects" / "portfolio" / "product.yaml", textwrap.dedent(
        """\
        schema_version: 1
        product: portfolio
        title: Portfolio
        description: Synthetic portfolio
        default_repo: alpha-repo
        repos:
          - alias: alpha
            repo_id: alpha-repo
        backlog: []
        releases: []
        """
    ))
    return repo


def test_group_runner_refuses_owned_repo_before_loop_start(tmp_path):
    module = _load_group_runner()
    repo = _seed_owned_home(tmp_path)
    member = module.Member(name="alpha", project_dir=repo, dispatch_config=repo / ".builder" / "dispatch.yaml")

    module.load_members = lambda runner_name, projects_root, config=None: ["alpha"]
    module.resolve_members = lambda member_names, projects_root: ([member], [])
    module._write_pidfile = lambda runner_name: (_ for _ in ()).throw(AssertionError("pidfile write must not happen"))
    module.run_loop = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("run_loop must not start"))

    stdout = io.StringIO()
    with redirect_stdout(stdout):
        rc = module.main(["--runner", "core", "--projects-root", str(tmp_path), "--once"])

    out = stdout.getvalue()
    assert rc == 2
    assert "refuse builder-group-runner" in out
    assert "alpha-repo" in out
    assert str(tmp_path / ".builder-home") in out
    assert "central daemon" in out


def test_group_runner_keeps_standalone_path_when_no_home_exists(tmp_path):
    module = _load_group_runner()
    repo = _repo(tmp_path, "alpha-repo")
    member = module.Member(name="alpha", project_dir=repo, dispatch_config=repo / ".builder" / "dispatch.yaml")
    recorded = {}

    module.load_members = lambda runner_name, projects_root, config=None: ["alpha"]
    module.resolve_members = lambda member_names, projects_root: ([member], [])
    module._write_pidfile = lambda runner_name: None

    def _run_loop(runner_name, members, skipped, **kwargs):
        recorded["runner_name"] = runner_name
        recorded["members"] = members
        recorded["skipped"] = skipped
        recorded["once"] = kwargs["once"]
        return 0

    module.run_loop = _run_loop

    rc = module.main(["--runner", "core", "--projects-root", str(tmp_path), "--once"])

    assert rc == 0
    assert recorded["runner_name"] == "core"
    assert recorded["members"] == [member]
    assert recorded["skipped"] == []
    assert recorded["once"] is True
