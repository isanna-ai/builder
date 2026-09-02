from __future__ import annotations

from pathlib import Path

from _dispatch_runtime.config import DispatchConfig, LaneConfig
from _dispatch_runtime.lane_executor import DispatchResult, DispatchResultType
from _dispatch_runtime.queue_store import QueueStore
from _dispatch_runtime.scheduler import DispatchScheduler
from _telemetry.memory_eval import load_memory_evals


class StubExecutor:
    def __init__(self, result: DispatchResult):
        self.result = result

    def execute(self, task_ref, lane_name, attempt_context):
        return self.result


def _config(tmp_path: Path) -> DispatchConfig:
    lanes = {"claude-code-cli": LaneConfig(name="claude-code-cli", provider="claude-code-cli", max_concurrency=1)}
    return DispatchConfig(
        queue_store_path=tmp_path,
        lanes=lanes,
        routing_policy={"default": "ordered", "tie_break": "lane_order"},
        cooldown_policy={"default_seconds": 60},
        retry_policy={"max_attempts": 3, "initial_seconds": 5, "max_seconds": 30, "jitter_seconds": 0},
    )


def _scheduler(tmp_path: Path, result: DispatchResult) -> tuple[DispatchScheduler, str]:
    queue_root = tmp_path / ".builder" / "dispatch-queue"
    store = QueueStore(queue_root)
    item = store.enqueue(
        task_ref={"kind": "builder-phase-batch", "runner_task_ref": ".builder/specs/demo/runs/phase-4-plan.yaml", "spec_id": "demo"},
        lane="claude-code-cli",
    )
    scheduler = DispatchScheduler(
        store,
        _config(tmp_path),
        StubExecutor(result),
        owner_id="scheduler-a",
        project_dir=tmp_path,
    )
    return scheduler, item.id


def _complete(scheduler: DispatchScheduler, work_id: str, lane: str) -> None:
    attempt_id = "attempt-test"
    scheduler.store.record_attempt(
        work_id, attempt_id=attempt_id, lane=lane,
        metadata={"work_id": work_id, "lane": lane, "log_path": "queue/attempts/x.log", "started_at": "now"},
    )
    scheduler._complete_attempt(work_id, attempt_id, lane, {"spec_id": "demo"}, {"work_id": work_id})


def test_plan_result_emits_one_memory_eval(tmp_path):
    result = DispatchResult(
        result_type=DispatchResultType.SUCCESS,
        metadata={"spec_id": "demo", "phase": "plan", "plan_tokens_in": 10, "plan_tokens_out": 20, "cli_duration_ms": 50},
    )
    scheduler, work_id = _scheduler(tmp_path, result)
    _complete(scheduler, work_id, "claude-code-cli")

    rows = load_memory_evals(tmp_path)
    assert len(rows) == 1
    row = rows[0]
    assert row["spec_id"] == "demo"
    assert row["phase"] == "4-plan"
    assert row["memory_mode"] == "off"
    assert row["plan_tokens_out"] == 20
    assert row["plan_wall_ms"] >= 0
    assert row["spec_outcome"] == "unknown"


def test_non_plan_result_emits_zero_records(tmp_path):
    result = DispatchResult(
        result_type=DispatchResultType.SUCCESS,
        metadata={"spec_id": "demo", "phase": "implement", "plan_tokens_in": 10, "plan_tokens_out": 20, "cli_duration_ms": 50},
    )
    scheduler, work_id = _scheduler(tmp_path, result)
    _complete(scheduler, work_id, "claude-code-cli")

    rows = load_memory_evals(tmp_path)
    assert rows == []
