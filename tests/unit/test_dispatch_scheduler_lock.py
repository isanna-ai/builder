"""Scheduler lock stale-owner recovery.

A daemon killed without releasing the lock (SIGKILL, crash, container stop)
leaves .scheduler.lock owned by a dead pid. Without recovery that wedges every
future daemon ("scheduler lock is owned by another process" forever — observed
live 2026-06-05). acquire_scheduler_lock steals the lock when the recorded
owner's process is dead, but never when it is alive.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from _dispatch_runtime.config import DispatchConfig, LaneConfig
from _dispatch_runtime.queue_store import QueueStore
from _dispatch_runtime.scheduler import DispatchScheduler, SchedulerBusyError


class _NoopExecutor:
    def execute(self, task_ref, lane_name, attempt_context):  # pragma: no cover
        raise AssertionError("executor must not run in lock tests")


def _config(tmp_path) -> DispatchConfig:
    return DispatchConfig(
        queue_store_path=tmp_path,
        lanes={"claude-code-cli": LaneConfig(name="claude-code-cli", provider="claude-code-cli", max_concurrency=1)},
        routing_policy={"default": "ordered", "tie_break": "lane_order"},
        cooldown_policy={"default_seconds": 60},
        retry_policy={"max_attempts": 3, "initial_seconds": 5, "max_seconds": 30, "jitter_seconds": 0},
    )


def _scheduler(tmp_path, owner: str) -> DispatchScheduler:
    store = QueueStore(tmp_path)
    sched = DispatchScheduler(store, _config(tmp_path), _NoopExecutor(), owner_id=owner)
    sched.lock_path.parent.mkdir(parents=True, exist_ok=True)
    return sched


def test_acquire_steals_stale_lock_from_dead_owner(tmp_path):
    sched = _scheduler(tmp_path, owner="dispatch-25127")
    sched.lock_path.write_text("dispatch-999999\n", encoding="utf-8")  # dead pid
    owner = sched.acquire_scheduler_lock()
    assert owner == "dispatch-25127"
    assert sched.lock_path.read_text(encoding="utf-8").strip() == "dispatch-25127"
    sched.release_scheduler_lock()
    assert not sched.lock_path.exists()


def test_acquire_rejects_lock_held_by_live_owner(tmp_path):
    sched = _scheduler(tmp_path, owner="dispatch-1")
    live = f"dispatch-{os.getpid()}"  # our own pid — definitely alive
    sched.lock_path.write_text(live + "\n", encoding="utf-8")
    try:
        sched.acquire_scheduler_lock()
    except SchedulerBusyError:
        pass
    else:
        raise AssertionError("expected SchedulerBusyError for a lock held by a live owner")
    assert sched.lock_path.read_text(encoding="utf-8").strip() == live  # not stolen


def test_acquire_reclaims_own_prior_lock(tmp_path):
    sched = _scheduler(tmp_path, owner="dispatch-555")
    sched.lock_path.write_text("dispatch-555\n", encoding="utf-8")  # our own owner id, leftover
    owner = sched.acquire_scheduler_lock()
    assert owner == "dispatch-555"
    sched.release_scheduler_lock()


def test_acquire_rejects_unparseable_owner(tmp_path):
    # Non-'dispatch-<pid>' owners (e.g. test owners) have no pid to liveness-check;
    # they must NOT be stolen.
    sched = _scheduler(tmp_path, owner="scheduler-b")
    sched.lock_path.write_text("scheduler-a\n", encoding="utf-8")
    try:
        sched.acquire_scheduler_lock()
    except SchedulerBusyError:
        pass
    else:
        raise AssertionError("expected SchedulerBusyError for an unparseable live owner")
