import json
import os
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from _builder_project_model.central_daemon import snapshot_digest
from _builder_project_model.central_supervisor import CentralSupervisor
from _builder_project_model.governor import current_pid_start_ticks
from _builder_project_model.home import load_builder_home


def _write(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(value), encoding="utf-8")


def _home(tmp_path: Path) -> Path:
    repo = tmp_path / "alpha"
    (repo / ".git").mkdir(parents=True)
    home = tmp_path / ".builder-home"
    _write(home / "builder.yaml", """
        schema_version: 1
        home_id: supervisor
        repositories: repositories.yaml
        policy: policy.yaml
        projects: []
    """)
    _write(home / "repositories.yaml", """
        schema_version: 1
        repos:
          - {id: alpha, path: ../alpha}
    """)
    _write(home / "policy.yaml", """
        schema_version: 1
        governor: {enabled: true, drain_repos: [alpha]}
        providers:
          claude-code-cli:
            max_sessions: 1
            quota_cooldown: {initial_seconds: 1, max_seconds: 2}
          codex-cli:
            max_sessions: 1
            quota_cooldown: {initial_seconds: 1, max_seconds: 2}
        allocation: {policy: equal-weight-fair-share, project_weight: 1}
        scheduler: {poll_seconds: 1, heartbeat_seconds: 1, stale_daemon_seconds: 3}
    """)
    return home


def _record(home: Path, *, pid: int, ticks: int) -> None:
    loaded = load_builder_home(home)
    _write(home / "state" / "daemon.json", json.dumps({
        "schema_version": 1,
        "pid": pid,
        "pid_start_ticks": ticks,
        "config_digest": snapshot_digest(loaded),
    }))


def test_watchdog_refuses_live_matching_daemon_and_identity_mismatch(tmp_path):
    home = _home(tmp_path)
    launches = []
    _record(home, pid=os.getpid(), ticks=current_pid_start_ticks())
    supervisor = CentralSupervisor(home, launcher=lambda argv: launches.append(argv))
    assert supervisor.ensure_once().action == "refused-live"
    assert launches == []

    _record(home, pid=os.getpid(), ticks=current_pid_start_ticks() + 1)
    decision = supervisor.ensure_once()
    assert decision.action == "refused"
    assert "identity-mismatch" in decision.detail
    assert launches == []


def test_watchdog_restarts_only_recorded_identity_proven_gone(tmp_path):
    home = _home(tmp_path)
    launches = []
    _record(home, pid=99_999_999, ticks=1)
    decision = CentralSupervisor(home, launcher=lambda argv: launches.append(argv)).ensure_once()
    assert decision.action == "restarted"
    assert len(launches) == 1
    assert "Mission Control" not in " ".join(launches[0])


def test_launch_detaches_daemon_into_new_session(tmp_path):
    # Regression: a watchdog-restarted daemon must run in its own session (start_new_session)
    # so it survives the watchdog dying and is re-adopted via daemon.json identity — same
    # detach fix as LiveCutoverOperator._spawn.
    import subprocess

    supervisor = CentralSupervisor(tmp_path / ".builder-home")
    captured = {}

    class _FakePopen:
        def __init__(self, argv, **kwargs):
            captured["kwargs"] = kwargs
            self.pid = 4242

    real_popen = subprocess.Popen
    subprocess.Popen = _FakePopen
    try:
        supervisor._launch(["true"])
    finally:
        subprocess.Popen = real_popen
    assert captured["kwargs"].get("start_new_session") is True
