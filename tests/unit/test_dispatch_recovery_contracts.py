from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from _dispatch_runtime.config import DispatchConfig, LaneConfig
from _dispatch_runtime.events import append_result_event
from _dispatch_runtime.lane_executor import DispatchResult, DispatchResultType
from _dispatch_runtime.queue_store import QueueStore
from _dispatch_runtime.scheduler import DispatchScheduler
from _dispatch_runtime.state_model import WorkItemState
from _dispatch_runtime.status import build_status_snapshot


def _iso(offset_seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=offset_seconds)).isoformat().replace("+00:00", "Z")


def _config(tmp_path: Path) -> DispatchConfig:
    return DispatchConfig(
        queue_store_path=tmp_path,
        lanes={"codex-cli": LaneConfig(name="codex-cli", provider="codex-cli", max_concurrency=1)},
        routing_policy={"default": "ordered", "tie_break": "lane_order"},
        cooldown_policy={"default_seconds": 60},
        retry_policy={"max_attempts": 3, "initial_seconds": 5, "max_seconds": 30, "jitter_seconds": 0},
    )


class _Executor:
    def __init__(self, result):
        self.result = result

    def execute(self, task_ref, lane_name: str, attempt_context):
        return self.result


def _scheduler(tmp_path: Path, *, result=None, project_dir: Path | None = None) -> DispatchScheduler:
    if result is None:
        result = DispatchResult(result_type=DispatchResultType.SUCCESS, metadata={"pid": 41})
    return DispatchScheduler(QueueStore(tmp_path), _config(tmp_path), _Executor(result), owner_id="scheduler-a", project_dir=project_dir)


def test_reclaim_stale_leases_resets_state_lane_and_schedule_characterization(tmp_path):
    scheduler = _scheduler(tmp_path)
    item = scheduler.store.enqueue(task_ref={"kind": "builder-runner-task", "runner_task_ref": "runs/task-T1.yaml"})
    scheduler.store.transition_item(
        item.id,
        WorkItemState.DISPATCHED,
        lease={"id": "lease-expired", "attempt_id": "attempt-1", "lane": "codex-cli", "expires_at": _iso(-5)},
    )

    reclaimed = scheduler.reclaim_stale_leases()
    updated = scheduler.store.get_item(item.id)

    assert reclaimed == [item.id]
    assert updated is not None
    assert updated.state == WorkItemState.QUEUED
    assert updated.lease == {}
    assert updated.lane is None
    assert updated.scheduled_after is None


def test_reap_completed_phase_items_cancels_superseded_duplicate_and_clears_lease(tmp_path):
    spec_dir = tmp_path / ".builder" / "specs" / "demo"
    spec_dir.mkdir(parents=True)
    (spec_dir / "phase-log.yaml").write_text(
        "phases:\n  - phase: plan\n    outcome: SUCCEEDED\n",
        encoding="utf-8",
    )
    scheduler = _scheduler(tmp_path, project_dir=tmp_path)
    item = scheduler.store.enqueue(
        task_ref={
            "kind": "builder-phase-batch",
            "runner_task_ref": ".builder/specs/demo/runs/phase-plan.yaml",
            "spec_id": "demo",
        }
    )

    reaped = scheduler.reap_completed_phase_items()
    updated = scheduler.store.get_item(item.id)

    assert reaped == [item.id]
    assert updated is not None
    assert updated.state == WorkItemState.CANCELLED
    assert updated.lease == {}
    assert updated.task_ref["last_error"] == "reaped: phase already SUCCEEDED (superseded duplicate)"


def test_rate_limited_result_requeues_with_cooldown_and_status_snapshot_stays_read_only(tmp_path):
    result = DispatchResult(
        result_type=DispatchResultType.RATE_LIMITED,
        metadata={"spec_id": "demo", "phase": "3-implement", "message": "max-throttled"},
    )
    scheduler = _scheduler(tmp_path, result=result)
    item = scheduler.store.enqueue(
        task_ref={"kind": "builder-runner-task", "runner_task_ref": "runs/task-T1.yaml"},
        lane="codex-cli",
    )

    scheduler.dispatch_once()
    assert scheduler.wait_for_attempts()
    updated = scheduler.store.get_item(item.id)
    lane = scheduler.store.reconstruct().lanes["codex-cli"]
    before_updated_at = updated.updated_at
    append_result_event(scheduler.store, item.id, attempt_id="attempt-extra", result_type="success")
    snapshot = build_status_snapshot(scheduler.store)
    after = scheduler.store.get_item(item.id)

    assert updated is not None
    assert updated.state == WorkItemState.QUEUED
    assert updated.lease == {}
    assert updated.attempt == 0
    assert updated.scheduled_after == lane.cooldown_until
    assert lane.reason == "rate_limited"
    assert snapshot.lane_cooldown_remaining["codex-cli"] > 0
    assert any(event.event_type == "result" for event in snapshot.recent_events)
    assert after is not None and after.updated_at == before_updated_at
