from __future__ import annotations

import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from _builder_project_model.ownership_guard import evaluate_repo_ownership


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _repo(root: Path, name: str) -> Path:
    repo = root / name
    (repo / ".git").mkdir(parents=True)
    return repo


def _policy_yaml(*, enabled: bool, drain_repos: list[str]) -> str:
    flag = "true" if enabled else "false"
    lines = [
        "schema_version: 1",
        "governor:",
        f"  enabled: {flag}",
        "  drain_repos:",
    ]
    if drain_repos:
        lines.extend(f"    - {repo_id}" for repo_id in drain_repos)
    else:
        lines.append("    []")
    lines.extend(
        [
            "providers:",
            "  claude-code-cli:",
            "    max_sessions: 1",
            "    quota_cooldown:",
            "      initial_seconds: 300",
            "      max_seconds: 3600",
            "  codex-cli:",
            "    max_sessions: 1",
            "    quota_cooldown:",
            "      initial_seconds: 300",
            "      max_seconds: 3600",
            "allocation:",
            "  policy: equal-weight-fair-share",
            "  project_weight: 1",
            "scheduler:",
            "  poll_seconds: 2",
            "  heartbeat_seconds: 5",
            "  stale_daemon_seconds: 30",
        ]
    )
    return "\n".join(lines) + "\n"


def _seed_home(tmp_path: Path, *, enabled: bool, drain_repos: list[str]) -> tuple[Path, Path, Path]:
    home = tmp_path / ".builder-home"
    alpha = _repo(tmp_path, "alpha-repo")
    beta = _repo(tmp_path, "beta-repo")
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
          - id: beta-repo
            path: ../beta-repo
        """
    ))
    _write(home / "policy.yaml", _policy_yaml(enabled=enabled, drain_repos=drain_repos))
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
          - alias: beta
            repo_id: beta-repo
        backlog: []
        releases: []
        """
    ))
    return home, alpha, beta


def test_ownership_guard_returns_unowned_when_no_home_exists(tmp_path: Path):
    repo = _repo(tmp_path, "standalone")

    result = evaluate_repo_ownership(repo)

    assert result.owned is False
    assert result.home_root is None
    assert result.repo_id is None
    assert result.findings == ()


def test_ownership_guard_only_owns_when_home_enabled_and_repo_is_drained(tmp_path: Path):
    _home, alpha, beta = _seed_home(tmp_path, enabled=True, drain_repos=["alpha-repo"])

    alpha_result = evaluate_repo_ownership(alpha)
    beta_result = evaluate_repo_ownership(beta)

    assert alpha_result.owned is True
    assert alpha_result.repo_id == "alpha-repo"
    assert alpha_result.home_root == tmp_path / ".builder-home"
    assert beta_result.owned is False
    assert beta_result.repo_id == "beta-repo"


def test_ownership_guard_returns_unowned_when_governor_is_disabled(tmp_path: Path):
    _home, alpha, _beta = _seed_home(tmp_path, enabled=False, drain_repos=["alpha-repo"])

    result = evaluate_repo_ownership(alpha)

    assert result.owned is False
    assert result.repo_id == "alpha-repo"
    assert result.findings == ()


def test_ownership_guard_degrades_malformed_home_to_unowned_with_finding(tmp_path: Path):
    home = tmp_path / ".builder-home"
    alpha = _repo(tmp_path, "alpha-repo")
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
        """\
        schema_version: 1
        repos:
          - id: alpha-repo
            path: ../alpha-repo
        """
    ))
    _write(home / "policy.yaml", "schema_version: 1\n")

    result = evaluate_repo_ownership(alpha)

    assert result.owned is False
    assert result.home_root is None
    assert result.findings
    assert "degraded to standalone" in result.findings[0]
