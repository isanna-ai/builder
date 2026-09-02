from __future__ import annotations

import importlib.util
import io
import sys
import textwrap
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
from tests.unit.public_export_support import require_repo_asset


def _load_dispatch_cli():
    spec = importlib.util.spec_from_file_location("phase7_dispatch_cli", SCRIPTS / "_dispatch_runtime" / "cli.py")
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_group_runner():
    require_repo_asset(
        ROOT, "scripts/builder-group-runner.py", "the legacy group-runner ownership guard"
    )
    spec = importlib.util.spec_from_file_location("phase7_group_runner_crosscheck", SCRIPTS / "builder-group-runner.py")
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


def _dispatch_yaml() -> str:
    return textwrap.dedent(
        """\
        queue_store:
          path: .builder/dispatch-queue
        lanes:
          - name: claude
            provider: claude-code-cli
            max_concurrency: 1
        routing_policy:
          default: ordered
        cooldown_policy:
          default_seconds: 30
        retry_policy:
          max_attempts: 3
          initial_seconds: 5
          max_seconds: 30
          jitter_seconds: 0
        """
    )


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


def _seed_repo_with_dispatch(root: Path, name: str = "alpha-repo") -> tuple[Path, Path]:
    repo = _repo(root, name)
    config = _write(repo / ".builder" / "dispatch.yaml", _dispatch_yaml())
    return repo, config


def _seed_owned_home(tmp_path: Path, repo: Path) -> Path:
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
        f"""\
        schema_version: 1
        repos:
          - id: alpha-repo
            path: ../{repo.name}
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
    return home


def _seed_malformed_home(tmp_path: Path, repo: Path) -> Path:
    home = tmp_path / ".builder-home"
    _write(home / "builder.yaml", textwrap.dedent(
        """\
        schema_version: 1
        home_id: broken
        repositories: repositories.yaml
        policy: policy.yaml
        projects:
          - id: portfolio
            manifest: projects/portfolio/product.yaml
        """
    ))
    _write(home / "repositories.yaml", textwrap.dedent(
        f"""\
        schema_version: 1
        repos:
          - id: alpha-repo
            path: ../{repo.name}
        """
    ))
    _write(home / "policy.yaml", "schema_version: 1\n")
    return home


class _FakeScheduler:
    def __init__(self, store, config, executor, owner_id, project_dir, lease_seconds):
        self.store = store
        self.config = config
        self.executor = executor
        self.owner_id = owner_id
        self.project_dir = project_dir
        self.lease_seconds = lease_seconds
        self.dispatch_calls = 0
        self.wait_calls = []

    def dispatch_once(self):
        self.dispatch_calls += 1
        return []

    def wait_for_attempts(self, timeout=None):
        self.wait_calls.append(timeout)
        return True


def test_dispatch_run_refuses_owned_repo_before_scheduler_start(tmp_path):
    module = _load_dispatch_cli()
    repo, config = _seed_repo_with_dispatch(tmp_path)
    _seed_owned_home(tmp_path, repo)
    module.DispatchScheduler = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("scheduler must not start"))

    stdout = io.StringIO()
    with redirect_stdout(stdout):
        rc = module.run(["--config", str(config), "run", "--once"])

    out = stdout.getvalue()
    assert rc == 2
    assert "refuse builder-dispatch run" in out
    assert "alpha-repo" in out
    assert str(tmp_path / ".builder-home") in out
    assert "central daemon" in out


def test_dispatch_run_keeps_standalone_once_path_when_no_home_exists(tmp_path):
    module = _load_dispatch_cli()
    repo, config = _seed_repo_with_dispatch(tmp_path)
    created = {}

    def _scheduler(*args, **kwargs):
        created["scheduler"] = _FakeScheduler(*args, **kwargs)
        return created["scheduler"]

    module.DispatchScheduler = _scheduler
    module._RoutingExecutor = lambda config: object()
    module.sweep_orphan_pgids = lambda path: []

    stdout = io.StringIO()
    with redirect_stdout(stdout):
        rc = module.run(["--config", str(config), "run", "--once"])

    out = stdout.getvalue()
    assert rc == 0
    assert created["scheduler"].project_dir == repo.resolve()
    assert created["scheduler"].dispatch_calls == 1
    assert "builder-dispatch run: project=" in out
    assert "run --once: no eligible work" in out


def test_dispatch_run_malformed_home_warns_and_degrades_to_standalone(tmp_path):
    module = _load_dispatch_cli()
    repo, config = _seed_repo_with_dispatch(tmp_path)
    _seed_malformed_home(tmp_path, repo)
    created = {}

    def _scheduler(*args, **kwargs):
        created["scheduler"] = _FakeScheduler(*args, **kwargs)
        return created["scheduler"]

    module.DispatchScheduler = _scheduler
    module._RoutingExecutor = lambda config: object()
    module.sweep_orphan_pgids = lambda path: []

    stdout = io.StringIO()
    with redirect_stdout(stdout):
        rc = module.run(["--config", str(config), "run", "--once"])

    out = stdout.getvalue()
    assert rc == 0
    assert created["scheduler"].dispatch_calls == 1
    assert "ownership-guard:" in out
    assert "degraded to standalone" in out
    assert "run --once: no eligible work" in out


def test_group_runner_malformed_home_warns_and_degrades_to_standalone(tmp_path):
    module = _load_group_runner()
    repo, config = _seed_repo_with_dispatch(tmp_path)
    _seed_malformed_home(tmp_path, repo)
    member = module.Member(name="alpha", project_dir=repo, dispatch_config=config)
    recorded = {}

    module.load_members = lambda runner_name, projects_root, config=None: ["alpha"]
    module.resolve_members = lambda member_names, projects_root: ([member], [])
    module._write_pidfile = lambda runner_name: None

    def _run_loop(runner_name, members, skipped, **kwargs):
        recorded["members"] = members
        recorded["once"] = kwargs["once"]
        return 0

    module.run_loop = _run_loop

    stdout = io.StringIO()
    with redirect_stdout(stdout):
        rc = module.main(["--runner", "core", "--projects-root", str(tmp_path), "--once"])

    out = stdout.getvalue()
    assert rc == 0
    assert recorded == {"members": [member], "once": True}
    assert "ownership-guard:" in out
    assert "degraded to standalone" in out


def test_both_legacy_entrypoints_refuse_the_same_owned_repo(tmp_path):
    dispatch_cli = _load_dispatch_cli()
    group_runner = _load_group_runner()
    repo, config = _seed_repo_with_dispatch(tmp_path)
    home = _seed_owned_home(tmp_path, repo)
    member = group_runner.Member(name="alpha", project_dir=repo, dispatch_config=config)

    dispatch_cli.DispatchScheduler = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("scheduler must not start"))
    group_runner.load_members = lambda runner_name, projects_root, config=None: ["alpha"]
    group_runner.resolve_members = lambda member_names, projects_root: ([member], [])
    group_runner._write_pidfile = lambda runner_name: (_ for _ in ()).throw(AssertionError("pidfile write must not happen"))
    group_runner.run_loop = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("loop must not start"))

    dispatch_stdout = io.StringIO()
    with redirect_stdout(dispatch_stdout):
        dispatch_rc = dispatch_cli.run(["--config", str(config), "run", "--once"])

    group_stdout = io.StringIO()
    with redirect_stdout(group_stdout):
        group_rc = group_runner.main(["--runner", "core", "--projects-root", str(tmp_path), "--once"])

    assert dispatch_rc == 2
    assert group_rc == 2
    assert str(home) in dispatch_stdout.getvalue()
    assert str(home) in group_stdout.getvalue()
