from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
FIXTURES = ROOT / "tests" / "fixtures" / "builder_project_model" / "locks" / "v1"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from _dispatch_runtime.config import DispatchConfig, LaneConfig
from _dispatch_runtime.queue_store import QueueStore
from _dispatch_runtime.scheduler import DispatchScheduler, SchedulerBusyError


class _NoopExecutor:
    def execute(self, task_ref, lane_name, attempt_context):  # pragma: no cover
        raise AssertionError("executor must not run in lock tests")


def _scheduler(tmp_path: Path, owner: str) -> DispatchScheduler:
    store = QueueStore(tmp_path)
    config = DispatchConfig(
        queue_store_path=tmp_path,
        lanes={"claude-code-cli": LaneConfig(name="claude-code-cli", provider="claude-code-cli", max_concurrency=1)},
        routing_policy={"default": "ordered", "tie_break": "lane_order"},
        cooldown_policy={"default_seconds": 60},
        retry_policy={"max_attempts": 3, "initial_seconds": 5, "max_seconds": 30, "jitter_seconds": 0},
    )
    scheduler = DispatchScheduler(store, config, _NoopExecutor(), owner_id=owner)
    scheduler.lock_path.parent.mkdir(parents=True, exist_ok=True)
    return scheduler


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_lock_contract_steals_dead_owner_and_reclaims_own_prior_lock(tmp_path):
    scheduler = _scheduler(tmp_path, owner="dispatch-25127")
    scheduler.lock_path.write_text(_fixture("scheduler-lock-dead-owner.txt"), encoding="utf-8")

    owner = scheduler.acquire_scheduler_lock()
    assert owner == "dispatch-25127"
    assert scheduler.lock_path.read_text(encoding="utf-8").strip() == "dispatch-25127"
    scheduler.release_scheduler_lock()

    scheduler = _scheduler(tmp_path, owner="dispatch-555")
    scheduler.lock_path.write_text(_fixture("scheduler-lock-own-prior.txt"), encoding="utf-8")
    assert scheduler.acquire_scheduler_lock() == "dispatch-555"


def test_lock_contract_refuses_live_and_unparseable_foreign_owners(tmp_path):
    scheduler = _scheduler(tmp_path, owner="dispatch-2")
    scheduler.lock_path.write_text(f"dispatch-{os.getpid()}\n", encoding="utf-8")
    try:
        scheduler.acquire_scheduler_lock()
    except SchedulerBusyError:
        pass
    else:
        raise AssertionError("expected busy live-owner refusal")

    scheduler = _scheduler(tmp_path, owner="dispatch-2")
    scheduler.lock_path.write_text(_fixture("scheduler-lock-unparseable.txt"), encoding="utf-8")
    try:
        scheduler.acquire_scheduler_lock()
    except SchedulerBusyError:
        pass
    else:
        raise AssertionError("expected busy unparseable-owner refusal")


def test_lock_fixture_shapes_match_current_owner_string_contract():
    assert _fixture("scheduler-lock-live-owner.txt").strip() == "dispatch-1"
    assert _fixture("scheduler-lock-dead-owner.txt").strip().startswith("dispatch-")
    assert _fixture("scheduler-lock-own-prior.txt").strip() == "dispatch-555"
    assert _fixture("scheduler-lock-unparseable.txt").strip() == "scheduler-a"
