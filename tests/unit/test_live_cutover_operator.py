import json
import os
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from _builder_project_model.cutover import CutoverError, CutoverOrchestrator
from _builder_project_model.governor import current_pid_start_ticks
from _builder_project_model.live_cutover import LiveCutoverOperator
from _dispatch_runtime.queue_store import QueueStore


def _write(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(value), encoding="utf-8")


def _home(tmp_path: Path) -> Path:
    repo = tmp_path / "alpha"
    (repo / ".git").mkdir(parents=True)
    _write(repo / ".builder" / "dispatch.yaml", """
        queue_store: {path: dispatch-queue}
        lanes:
          - {name: codex, provider: codex-cli, max_concurrency: 1}
    """)
    QueueStore(repo / ".builder" / "dispatch-queue")
    home = tmp_path / ".builder-home"
    _write(home / "builder.yaml", """
        schema_version: 1
        home_id: cutover
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


def test_live_cutover_absent_branch_dry_run_mutates_nothing(tmp_path):
    home = _home(tmp_path)
    before = sorted(str(path.relative_to(home)) for path in home.rglob("*"))
    operator = LiveCutoverOperator(home)
    results = CutoverOrchestrator(operator, dry_run=True).run_forward()
    after = sorted(str(path.relative_to(home)) for path in home.rglob("*"))
    assert len(results) == 7
    assert before == after
    assert results[0].details == ("legacy-absent",)


def test_live_cutover_present_branch_identity_mismatch_and_dual_owner_fail_closed(tmp_path):
    home = _home(tmp_path)
    legacy = home / "state" / "legacy" / "alpha.json"
    ident = [sys.executable, "-c", "synthetic"]
    _write(legacy, json.dumps({
        "repo_id": "alpha",
        "pid": os.getpid(),
        "pid_start_ticks": current_pid_start_ticks(),
        "executable": _current_executable(),
    }))
    operator = LiveCutoverOperator(home, signaler=lambda _pid, _sig: None)
    assert operator.stop_legacy(dry_run=True).details == ("stop:legacy:alpha",)
    try:
        operator.start_central(dry_run=False)
    except CutoverError as exc:
        assert "dual-owner-refused" in str(exc)
    else:
        raise AssertionError("expected live legacy owner to block central startup")

    row = json.loads(legacy.read_text(encoding="utf-8"))
    row["pid_start_ticks"] += 1
    legacy.write_text(json.dumps(row), encoding="utf-8")
    try:
        operator.stop_legacy(dry_run=False)
    except CutoverError as exc:
        assert "identity-mismatch" in str(exc)
    else:
        raise AssertionError("expected identity mismatch to fail closed")
    assert legacy.exists()


def _current_executable() -> str:
    from _dispatch_runtime.lane_common import _proc_identity

    identity = _proc_identity(os.getpid())
    assert identity is not None
    return identity[1].split()[0]


def test_live_cutover_requires_per_step_confirmation_and_preserves_rollback_inputs(tmp_path):
    home = _home(tmp_path)
    operator = LiveCutoverOperator(home, starter=lambda _argv: type("P", (), {"pid": 4242})())
    orchestrator = CutoverOrchestrator(operator, dry_run=False, confirmations=set())
    try:
        orchestrator.run_forward(steps=("start_central",))
    except CutoverError as exc:
        assert "missing-confirmation:start_central" in str(exc)
    else:
        raise AssertionError("expected per-step confirmation refusal")
    assert not operator.rollback_path.exists()


def test_start_central_spawn_detaches_into_new_session(tmp_path):
    # Regression: start_central's spawned daemon MUST run in its own session so it survives
    # the cutover CLI process exiting. The first webapp live cutover's daemon died because
    # the default _spawn used a plain (child-of-parent) Popen with no start_new_session.
    import subprocess

    operator = LiveCutoverOperator(_home(tmp_path))
    captured = {}

    class _FakePopen:
        def __init__(self, argv, **kwargs):
            captured["kwargs"] = kwargs
            self.pid = 4242

    real_popen = subprocess.Popen
    subprocess.Popen = _FakePopen
    try:
        operator._spawn(["true"])
    finally:
        subprocess.Popen = real_popen
    assert captured["kwargs"].get("start_new_session") is True
