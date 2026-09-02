from __future__ import annotations

from datetime import datetime, timedelta, timezone
import random

from _dispatch_runtime.backoff import apply_failure_backoff
from _dispatch_runtime.config import DispatchConfig, LaneConfig
from _dispatch_runtime.cooldown import cooldown_remaining_seconds, open_lane_cooldown
from _dispatch_runtime.lane_executor import DispatchResult, DispatchResultType
from _dispatch_runtime.queue_store import QueueStore
from _dispatch_runtime.scheduler import DispatchScheduler
from _dispatch_runtime.state_model import WorkItemState


def iso_at(offset_seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=offset_seconds)).isoformat().replace("+00:00", "Z")


def build_config(tmp_path) -> DispatchConfig:
    return DispatchConfig(
        queue_store_path=tmp_path,
        lanes={"codex-cli": LaneConfig(name="codex-cli", provider="codex-cli", max_concurrency=1)},
        routing_policy={"default": "ordered", "tie_break": "lane_order"},
        cooldown_policy={"default_seconds": 120},
        retry_policy={"max_attempts": 3, "initial_seconds": 10, "max_seconds": 40, "jitter_seconds": 0},
    )


class FixedResultExecutor:
    def __init__(self, result: DispatchResult):
        self.result = result

    def execute(self, task_ref, lane_name: str, attempt_context):
        return self.result


def test_rate_limited_results_open_lane_cooldown_from_retry_after_or_policy_default(tmp_path):
    store = QueueStore(tmp_path)
    retry_after = iso_at(90)

    open_lane_cooldown(
        store,
        "codex-cli",
        DispatchResult(result_type=DispatchResultType.RATE_LIMITED, retry_after=retry_after),
        {"default_seconds": 120},
    )

    persisted = store.reconstruct().lanes["codex-cli"]
    assert persisted.cooldown_until == retry_after
    assert persisted.reason == "rate_limited"

    fallback = open_lane_cooldown(
        store,
        "codex-cli",
        DispatchResult(result_type=DispatchResultType.RATE_LIMITED),
        {"default_seconds": 120},
    )
    assert fallback.cooldown_until is not None
    assert cooldown_remaining_seconds(fallback, now=datetime.now(timezone.utc)) <= 120


def test_cooled_down_lanes_are_excluded_from_routing(tmp_path):
    store = QueueStore(tmp_path)
    item = store.enqueue(task_ref={"kind": "builder-runner-task", "runner_task_ref": "runs/task-T4.yaml"})
    store.set_lane_cooldown("codex-cli", until=iso_at(300), reason="rate_limited")
    scheduler = DispatchScheduler(
        store,
        build_config(tmp_path),
        FixedResultExecutor(DispatchResult(result_type=DispatchResultType.SUCCESS)),
        owner_id="scheduler-a",
    )

    scheduler.dispatch_once()
    assert scheduler.wait_for_attempts()

    assert store.get_item(item.id).state == WorkItemState.QUEUED


def test_retryable_failures_compute_bounded_exponential_backoff_without_exceeding_policy_cap(tmp_path):
    store = QueueStore(tmp_path)
    item = store.enqueue(task_ref={"kind": "builder-runner-task", "runner_task_ref": "runs/task-T4.yaml"})

    first = apply_failure_backoff(
        store,
        item.id,
        DispatchResult(result_type=DispatchResultType.RETRYABLE_ERROR, metadata={"message": "try again"}),
        {"max_attempts": 4, "initial_seconds": 10, "max_seconds": 40, "jitter_seconds": 0},
        now=datetime(2026, 6, 2, 16, 0, tzinfo=timezone.utc),
    )
    second = apply_failure_backoff(
        store,
        item.id,
        DispatchResult(result_type=DispatchResultType.RETRYABLE_ERROR, metadata={"message": "try again"}),
        {"max_attempts": 4, "initial_seconds": 10, "max_seconds": 40, "jitter_seconds": 0},
        now=datetime(2026, 6, 2, 16, 0, tzinfo=timezone.utc),
    )
    third = apply_failure_backoff(
        store,
        item.id,
        DispatchResult(result_type=DispatchResultType.RETRYABLE_ERROR, metadata={"message": "try again"}),
        {"max_attempts": 4, "initial_seconds": 10, "max_seconds": 40, "jitter_seconds": 0},
        now=datetime(2026, 6, 2, 16, 0, tzinfo=timezone.utc),
    )

    assert first.state == WorkItemState.QUEUED
    assert first.scheduled_after == "2026-06-02T16:00:10Z"
    assert second.scheduled_after == "2026-06-02T16:00:20Z"
    assert third.scheduled_after == "2026-06-02T16:00:40Z"


def test_retryable_failures_apply_deterministic_jitter_without_exceeding_policy_cap(tmp_path):
    store = QueueStore(tmp_path)
    item = store.enqueue(task_ref={"kind": "builder-runner-task", "runner_task_ref": "runs/task-T4-jitter.yaml"})
    jitter_values = iter([3, 5, 5])

    original_randint = random.randint
    random.randint = lambda start, end: next(jitter_values)
    try:
        first = apply_failure_backoff(
            store,
            item.id,
            DispatchResult(result_type=DispatchResultType.RETRYABLE_ERROR, metadata={"message": "try again"}),
            {"max_attempts": 4, "initial_seconds": 10, "max_seconds": 25, "jitter_seconds": 5},
            now=datetime(2026, 6, 2, 16, 0, tzinfo=timezone.utc),
        )
        second = apply_failure_backoff(
            store,
            item.id,
            DispatchResult(result_type=DispatchResultType.RETRYABLE_ERROR, metadata={"message": "try again"}),
            {"max_attempts": 4, "initial_seconds": 10, "max_seconds": 25, "jitter_seconds": 5},
            now=datetime(2026, 6, 2, 16, 0, tzinfo=timezone.utc),
        )
        third = apply_failure_backoff(
            store,
            item.id,
            DispatchResult(result_type=DispatchResultType.RETRYABLE_ERROR, metadata={"message": "try again"}),
            {"max_attempts": 4, "initial_seconds": 10, "max_seconds": 25, "jitter_seconds": 5},
            now=datetime(2026, 6, 2, 16, 0, tzinfo=timezone.utc),
        )
    finally:
        random.randint = original_randint

    assert first.scheduled_after == "2026-06-02T16:00:13Z"
    assert second.scheduled_after == "2026-06-02T16:00:25Z"
    assert third.scheduled_after == "2026-06-02T16:00:25Z"


def test_exhausted_attempt_budget_fails_terminally(tmp_path):
    store = QueueStore(tmp_path)
    item = store.enqueue(task_ref={"kind": "builder-runner-task", "runner_task_ref": "runs/task-T4.yaml"}, max_attempts=2)

    first = apply_failure_backoff(
        store,
        item.id,
        DispatchResult(result_type=DispatchResultType.RETRYABLE_ERROR),
        {"max_attempts": 2, "initial_seconds": 10, "max_seconds": 40, "jitter_seconds": 0},
        now=datetime(2026, 6, 2, 16, 0, tzinfo=timezone.utc),
    )
    second = apply_failure_backoff(
        store,
        item.id,
        DispatchResult(result_type=DispatchResultType.RETRYABLE_ERROR),
        {"max_attempts": 2, "initial_seconds": 10, "max_seconds": 40, "jitter_seconds": 0},
        now=datetime(2026, 6, 2, 16, 0, tzinfo=timezone.utc),
    )

    assert first.state == WorkItemState.QUEUED
    assert second.state == WorkItemState.FAILED
    assert second.attempt == 2
