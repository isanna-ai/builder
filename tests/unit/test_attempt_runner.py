import textwrap
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from _dispatch_runtime.attempt_runner import AttemptRefused, run_reserved_attempt
from _dispatch_runtime.attempt_runner import _attempt_context
from _dispatch_runtime.config import load_dispatch_config
from _dispatch_runtime.scheduler import DispatchScheduler
from _dispatch_runtime.lane_executor import DispatchResult, DispatchResultType
from _dispatch_runtime.queue_store import QueueStore
from _dispatch_runtime.state_model import WorkItemState
from _builder_project_model.governor import current_pid_start_ticks
from _builder_project_model.session_store import SessionStore
import os


def _write(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(value), encoding="utf-8")


def _fixture(tmp_path: Path):
    repo = tmp_path / "alpha-repo"
    (repo / ".git").mkdir(parents=True)
    config = repo / ".builder" / "dispatch.yaml"
    _write(config, """
        queue_store:
          path: dispatch-queue
        lanes:
          - name: codex
            provider: codex-cli
            max_concurrency: 1
    """)
    home = tmp_path / ".builder-home"
    _write(home / "builder.yaml", """
        schema_version: 1
        home_id: test
        repositories: repositories.yaml
        policy: policy.yaml
        projects: []
    """)
    _write(home / "repositories.yaml", """
        schema_version: 1
        repos:
          - id: alpha-repo
            path: ../alpha-repo
    """)
    _write(home / "policy.yaml", """
        schema_version: 1
        governor:
          enabled: true
          drain_repos:
            - alpha-repo
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
    return repo, config, QueueStore(repo / ".builder" / "dispatch-queue")


class _Executor:
    def __init__(self):
        self.calls = []

    def execute(self, task_ref, lane_name, attempt_context):
        self.calls.append((task_ref, lane_name, attempt_context))
        return DispatchResult(DispatchResultType.SUCCESS, metadata={"phase": None})


def _centrally_lease(store: QueueStore, item) -> str:
    attempt_id = "attempt-central-a"
    item.attempt += 1
    item.lane = "codex"
    item.state = WorkItemState.RUNNING
    item.lease = {
        "id": "lease-a",
        "attempt_id": attempt_id,
        "lane": "codex",
        "owner": "central-test",
        "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
    }
    store.save_item(item)
    store.record_attempt(item.id, attempt_id=attempt_id, lane="codex", metadata={"work_id": item.id})
    session_store = SessionStore(store.root.parents[2] / ".builder-home")
    session_store.reserve_slot(
        provider="codex-cli",
        max_sessions=1,
        daemon_instance_id="test",
        owner_pid=os.getpid(),
        owner_pid_start_ticks=current_pid_start_ticks(),
        repo_id="alpha-repo",
        queue_root=store.root,
        work_id=item.id,
        attempt_id=attempt_id,
        lane="codex",
        project_attribution="standalone:alpha-repo",
        release_name=None,
    )
    return attempt_id


def test_attempt_runner_executes_only_matching_central_lease(tmp_path):
    _repo, config, store = _fixture(tmp_path)
    item_a = store.enqueue(task_ref={"kind": "synthetic", "runner_task_ref": "A"}, lane="codex")
    item_b = store.enqueue(task_ref={"kind": "synthetic", "runner_task_ref": "B"}, lane="codex")
    attempt_id = _centrally_lease(store, item_a)
    executor = _Executor()

    run_reserved_attempt(item_a.id, config_path=config, executor=executor, expected_attempt_id=attempt_id)

    assert [call[0]["runner_task_ref"] for call in executor.calls] == ["A"]
    assert store.get_item(item_a.id).state == WorkItemState.SUCCEEDED
    untouched = store.get_item(item_b.id)
    assert untouched.state == WorkItemState.QUEUED
    assert untouched.attempt == 0 and untouched.lease == {}
    assert not (store.queue_dir / ".scheduler.lock").exists()


def test_attempt_runner_refuses_unleased_item_without_execution(tmp_path):
    _repo, config, store = _fixture(tmp_path)
    item = store.enqueue(task_ref={"kind": "synthetic", "runner_task_ref": "B"}, lane="codex")
    executor = _Executor()
    try:
        run_reserved_attempt(item.id, config_path=config, executor=executor)
    except AttemptRefused as exc:
        assert "not RUNNING" in str(exc)
    else:
        raise AssertionError("expected an unleased item to be refused")
    assert executor.calls == []
    assert store.get_item(item.id).state == WorkItemState.QUEUED


def test_attempt_context_forces_sync_era_isolation_even_when_pipeline_flag_is_off(tmp_path):
    repo, config, store = _fixture(tmp_path)
    spec_dir = repo / ".builder" / "specs" / "demo"
    spec_dir.mkdir(parents=True, exist_ok=True)
    spec_dir.joinpath("ssot-delta.yaml").write_text(
        "capabilities: []\nbehaviors: []\njourneys: []\n", encoding="utf-8"
    )
    item = store.enqueue(task_ref={
        "kind": "builder-phase-batch",
        "runner_task_ref": ".builder/specs/demo/runs/phase-implement.yaml",
        "spec_id": "demo",
    }, lane="codex")
    scheduler = DispatchScheduler(
        store,
        load_dispatch_config(config),
        _Executor(),
        owner_id="central-test",
        project_dir=repo,
    )
    expected = repo / ".builder" / "worktrees" / "demo"
    scheduler._ensure_worktree = lambda spec_id: expected  # type: ignore[method-assign]

    context = _attempt_context(scheduler, item, "attempt-central-a")

    assert context["workspace_root"] == str(expected)
    assert context["control_root"] == str(repo)
