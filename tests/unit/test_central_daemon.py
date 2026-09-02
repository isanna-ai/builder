import json
import os
import sys
import textwrap
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from _builder_project_model.central_daemon import CentralDaemon, _next_sleep
from _dispatch_runtime.queue_store import QueueStore
from _dispatch_runtime.scheduler import SchedulerBusyError
from _dispatch_runtime.state_model import WorkItemState


def _write(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(value), encoding="utf-8")


def _seed(tmp_path: Path) -> tuple[Path, Path, Path]:
    repos = []
    for name in ("alpha", "beta", "legacy"):
        repo = tmp_path / name
        (repo / ".git").mkdir(parents=True)
        _write(repo / ".builder" / "dispatch.yaml", """
            queue_store: {path: dispatch-queue}
            lanes:
              - {name: codex, provider: codex-cli, max_concurrency: 1}
        """)
        QueueStore(repo / ".builder" / "dispatch-queue")
        repos.append(repo)
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
          - {id: alpha, path: ../alpha}
          - {id: beta, path: ../beta}
          - {id: legacy, path: ../legacy}
    """)
    _write(home / "policy.yaml", """
        schema_version: 1
        governor: {enabled: true, drain_repos: [alpha, legacy]}
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
    return home, repos[0], repos[2]


def test_central_daemon_single_owner_allow_list_legacy_skip_and_clean_shutdown(tmp_path):
    home, alpha, legacy = _seed(tmp_path)
    legacy_lock = legacy / ".builder" / "dispatch-queue" / "queue" / ".scheduler.lock"
    legacy_lock.write_text(f"dispatch-{os.getpid()}\n", encoding="utf-8")
    daemon = CentralDaemon(home, poll_seconds=0.01, shutdown_timeout=0.1)
    daemon.start()
    try:
        assert set(daemon.drainer.controllers) == {"alpha"}
        assert any("loud refusal" in finding and "legacy" in finding for finding in daemon.findings)
        assert (alpha / ".builder" / "dispatch-queue" / "queue" / ".scheduler.lock").exists()
        assert not (tmp_path / "beta" / ".builder" / "dispatch-queue" / "queue" / ".scheduler.lock").exists()
        payload = json.loads((home / "state" / "daemon.json").read_text(encoding="utf-8"))
        assert payload["pid"] == os.getpid() and payload["config_digest"].startswith("sha256:")
        # Regression: the daemon record MUST carry `executable` so the live-cutover operator's
        # identity check (_pid_state / acquire_repo_locks) can verify it. Omitting it made
        # acquire_repo_locks fail with missing-identity-metadata against a real daemon (synthetic
        # fixtures had the field, so the green suite missed it — caught only in a live cutover).
        from _builder_project_model.governor import current_executable
        assert payload["executable"] == current_executable()
        other = CentralDaemon(home)
        try:
            other.start()
        except SchedulerBusyError:
            pass
        else:
            raise AssertionError("expected the second central daemon owner to be refused")
    finally:
        daemon.shutdown()
        legacy_lock.unlink()
    assert not (home / "state" / "scheduler.lock").exists()
    assert not (home / "state" / "daemon.json").exists()
    assert not (alpha / ".builder" / "dispatch-queue" / "queue" / ".scheduler.lock").exists()


def test_invalid_snapshot_reload_keeps_last_good_digest(tmp_path):
    home, _alpha, _legacy = _seed(tmp_path)
    daemon = CentralDaemon(home, shutdown_timeout=0.1)
    daemon.start()
    before = daemon.config_digest
    try:
        (home / "policy.yaml").write_text("schema_version: 1\n", encoding="utf-8")
        daemon.tick()
        assert daemon.config_digest == before
        assert any("snapshot reload refused" in finding for finding in daemon.findings)
    finally:
        daemon.shutdown()


def test_completed_central_rate_limit_opens_provider_global_cooldown(tmp_path):
    home, alpha, _legacy = _seed(tmp_path)
    queue = QueueStore(alpha / ".builder" / "dispatch-queue")
    item = queue.enqueue(task_ref={"kind": "synthetic"}, lane="codex")
    queue.record_attempt(
        item.id,
        attempt_id="attempt-rate-limited",
        lane="codex",
        metadata={"decision": "rate-limit-cooldown"},
    )
    queue.set_lane_cooldown(
        "codex",
        until="2099-01-01T00:00:00Z",
        reason="rate_limited",
    )
    daemon = CentralDaemon(home, shutdown_timeout=0.1)
    record = {
        "slot_id": "slot-rate-limited",
        "provider": "codex-cli",
        "repo_id": "alpha",
        "queue_root": str(queue.root.resolve()),
        "work_id": item.id,
        "attempt_id": "attempt-rate-limited",
        "lane": "codex",
    }

    # Simulate a daemon crash after the central slot was reaped but before the
    # repo-local result was finalized. Startup recovery must still propagate
    # the matching rate limit before it removes the durable finalize marker.
    daemon._stage_finalization(record)
    marker = daemon._finalize_marker(record)
    daemon._recover_finalizations()

    cooldown = daemon.store.read_provider("codex-cli")
    assert cooldown["cooldown_until"] == "2099-01-01T00:00:00Z"
    assert cooldown["reason_class"] == "rate-limit"
    assert cooldown["source_repo_id"] == "alpha"
    assert cooldown["source_attempt_id"] == "attempt-rate-limited"
    assert not marker.exists()


# ------------------------------------------------------------------ D3c idle-tick backoff

def test_next_sleep_stretches_geometrically_while_fully_idle():
    poll = 2.0
    sleep0, idle1 = _next_sleep(poll, 0, [], [])
    assert (sleep0, idle1) == (2.0, 1)
    sleep1, idle2 = _next_sleep(poll, idle1, [], [])
    assert (sleep1, idle2) == (4.0, 2)
    sleep2, idle3 = _next_sleep(poll, idle2, [], [])
    assert (sleep2, idle3) == (8.0, 3)


def test_next_sleep_caps_at_30_seconds():
    poll = 5.0
    sleep_seconds, idle_ticks = _next_sleep(poll, 10, [], [])
    assert sleep_seconds == 30.0
    assert idle_ticks == 11


def test_next_sleep_resets_to_poll_seconds_on_launch():
    poll = 2.0
    _sleep, idle_after_idling = _next_sleep(poll, 3, [], [])
    assert idle_after_idling == 4
    sleep_seconds, idle_ticks = _next_sleep(poll, idle_after_idling, [object()], [])
    assert (sleep_seconds, idle_ticks) == (2.0, 0)


def test_next_sleep_resets_to_poll_seconds_on_live_session():
    poll = 2.0
    _sleep, idle_after_idling = _next_sleep(poll, 3, [], [])
    sleep_seconds, idle_ticks = _next_sleep(poll, idle_after_idling, [], [{"slot_id": "s"}])
    assert (sleep_seconds, idle_ticks) == (2.0, 0)


def _running_item(queue: QueueStore, *, attempt: int, max_attempts: int = 3):
    item = queue.enqueue(task_ref={"kind": "synthetic", "spec_id": "demo-spec"}, lane="codex", max_attempts=max_attempts)
    item.attempt = attempt
    item.state = WorkItemState.RUNNING
    item.lease = {"attempt_id": "attempt-interrupted", "lane": "codex"}
    queue.save_item(item)
    return item


def test_interrupted_attempt_requeues_with_backoff_when_attempts_remain(tmp_path):
    home, alpha, _legacy = _seed(tmp_path)
    queue = QueueStore(alpha / ".builder" / "dispatch-queue")
    item = _running_item(queue, attempt=1, max_attempts=3)
    daemon = CentralDaemon(home, shutdown_timeout=0.1)
    record = {
        "slot_id": "slot-interrupted-a",
        "provider": "codex-cli",
        "repo_id": "alpha",
        "queue_root": str(queue.root.resolve()),
        "work_id": item.id,
        "attempt_id": "attempt-interrupted",
        "lane": "codex",
    }

    daemon._finalize_interrupted(record, "session proven dead")

    updated = queue.get_item(item.id)
    assert updated.state == WorkItemState.QUEUED
    assert updated.lease == {}
    assert "requeued" in updated.task_ref["last_error"]
    scheduled = datetime.fromisoformat(str(updated.scheduled_after).replace("Z", "+00:00"))
    assert scheduled > datetime.now(timezone.utc)
    assert any(
        "slot-interrupted-a" in finding and "interrupted" in finding and "requeued" in finding
        for finding in daemon.findings
    )


def test_interrupted_attempt_fails_when_retry_budget_exhausted(tmp_path):
    home, alpha, _legacy = _seed(tmp_path)
    queue = QueueStore(alpha / ".builder" / "dispatch-queue")
    item = _running_item(queue, attempt=3, max_attempts=3)
    daemon = CentralDaemon(home, shutdown_timeout=0.1)
    record = {
        "slot_id": "slot-interrupted-b",
        "provider": "codex-cli",
        "repo_id": "alpha",
        "queue_root": str(queue.root.resolve()),
        "work_id": item.id,
        "attempt_id": "attempt-interrupted",
        "lane": "codex",
    }

    daemon._finalize_interrupted(record, "session proven dead")

    updated = queue.get_item(item.id)
    assert updated.state == WorkItemState.FAILED
    assert updated.lease == {}
    assert updated.scheduled_after is None
    assert "retry budget exhausted" in updated.task_ref["last_error"]
    assert any(
        "slot-interrupted-b" in finding and "interrupted" in finding and "failed" in finding
        for finding in daemon.findings
    )


def test_interrupted_attempt_notifies_and_writes_a_packet(tmp_path):
    home, alpha, _legacy = _seed(tmp_path)
    queue = QueueStore(alpha / ".builder" / "dispatch-queue")
    item = _running_item(queue, attempt=1, max_attempts=3)
    daemon = CentralDaemon(home, shutdown_timeout=0.1)
    record = {
        "slot_id": "slot-interrupted-notify",
        "provider": "codex-cli",
        "repo_id": "alpha",
        "queue_root": str(queue.root.resolve()),
        "work_id": item.id,
        "attempt_id": "attempt-interrupted",
        "lane": "codex",
    }

    daemon._finalize_interrupted(record, "session proven dead")

    notifications_dir = queue.root / "queue" / "notifications"
    packets = list(notifications_dir.glob("*attempt-interrupted*"))
    assert packets, "expected a FileNotifier packet for the attempt-interrupted notification"
    body = packets[0].read_text(encoding="utf-8")
    assert "demo-spec" in body
    assert "requeued" in body
    assert not any("unable to notify" in finding for finding in daemon.findings)


def test_interrupted_attempt_notifier_exception_does_not_block_finalization(tmp_path):
    home, alpha, _legacy = _seed(tmp_path)
    queue = QueueStore(alpha / ".builder" / "dispatch-queue")
    item = _running_item(queue, attempt=1, max_attempts=3)
    daemon = CentralDaemon(home, shutdown_timeout=0.1)
    record = {
        "slot_id": "slot-interrupted-boom",
        "provider": "codex-cli",
        "repo_id": "alpha",
        "queue_root": str(queue.root.resolve()),
        "work_id": item.id,
        "attempt_id": "attempt-interrupted",
        "lane": "codex",
    }

    import _dispatch_runtime.notifier as notifier_module

    def _boom(*args, **kwargs):
        raise RuntimeError("notifier misconfigured")

    original = notifier_module.build_notifier
    notifier_module.build_notifier = _boom
    try:
        daemon._finalize_interrupted(record, "session proven dead")
    finally:
        notifier_module.build_notifier = original

    updated = queue.get_item(item.id)
    assert updated.state == WorkItemState.QUEUED  # finalization still completed
    assert any("interrupted" in finding and "requeued" in finding for finding in daemon.findings)
    assert any("unable to notify attempt-interrupted" in finding for finding in daemon.findings)


def test_finalize_interrupted_double_finalize_is_a_no_op(tmp_path):
    # _recover_finalizations can re-run _finalize_interrupted for a marker that
    # was already resolved (e.g. a daemon restart between finalize and marker
    # unlink). The state/attempt_id guards must make a second call a pure no-op
    # regardless of the requeue-vs-fail disposition chosen the first time.
    home, alpha, _legacy = _seed(tmp_path)
    queue = QueueStore(alpha / ".builder" / "dispatch-queue")
    item = _running_item(queue, attempt=1, max_attempts=3)
    daemon = CentralDaemon(home, shutdown_timeout=0.1)
    record = {
        "slot_id": "slot-interrupted-c",
        "provider": "codex-cli",
        "repo_id": "alpha",
        "queue_root": str(queue.root.resolve()),
        "work_id": item.id,
        "attempt_id": "attempt-interrupted",
        "lane": "codex",
    }

    daemon._finalize_interrupted(record, "first finalize")
    first = queue.get_item(item.id)
    findings_after_first = list(daemon.findings)

    daemon._finalize_interrupted(record, "second finalize (replayed)")
    second = queue.get_item(item.id)

    assert second.state == first.state
    assert second.lease == first.lease
    assert second.scheduled_after == first.scheduled_after
    assert second.task_ref["last_error"] == first.task_ref["last_error"]
    # No new finding: the attempt_id on the (now-QUEUED) item no longer matches
    # the stale record, so the guard clause returns before appending anything.
    assert daemon.findings == findings_after_first
