from __future__ import annotations

from datetime import datetime, timezone

from _dispatch_runtime.queue_store import AttemptRecord, QueueStore, WorkItem
from _dispatch_runtime.state_model import InvalidTransitionError, WorkItemState, transition


def test_enqueue_persists_queued_work_item_record(tmp_path):
    store = QueueStore(tmp_path)

    item = store.enqueue(
        task_ref={"kind": "builder-runner-task", "runner_task_ref": "runs/task-T1.yaml"},
        priority=3,
        lane="codex-cli",
        max_attempts=4,
    )

    item_path = tmp_path / "queue" / "items" / f"{item.id}.yaml"
    assert item_path.exists()

    reloaded = QueueStore(tmp_path).get_item(item.id)
    assert reloaded is not None
    assert reloaded.state == WorkItemState.QUEUED
    assert reloaded.priority == 3
    assert reloaded.attempt == 0
    assert reloaded.max_attempts == 4
    assert reloaded.lane == "codex-cli"
    assert reloaded.task_ref["runner_task_ref"] == "runs/task-T1.yaml"


def test_reconstructs_items_attempts_cooldowns_and_events_from_disk(tmp_path):
    store = QueueStore(tmp_path)
    item = store.enqueue(task_ref={"kind": "generic", "ref": "task-1"})
    store.transition_item(item.id, WorkItemState.DISPATCHED, lease={"id": "lease-1", "lane": "codex-cli"})
    store.record_attempt(
        item.id,
        attempt_id="attempt-1",
        lane="codex-cli",
        metadata={"pid": 1234, "heartbeat_at": "2026-06-02T16:00:00Z", "logs": ["logs/a.txt"]},
    )
    store.set_lane_cooldown("codex-cli", until="2026-06-02T16:10:00Z", reason="rate_limited")
    store.append_event(item.id, "custom_event", {"lane": "codex-cli"})

    reconstructed = QueueStore(tmp_path).reconstruct()

    assert reconstructed.items[item.id].state == WorkItemState.DISPATCHED
    assert reconstructed.items[item.id].lease["id"] == "lease-1"
    assert reconstructed.attempts["attempt-1"].work_id == item.id
    assert reconstructed.attempts["attempt-1"].metadata["pid"] == 1234
    assert reconstructed.lanes["codex-cli"].cooldown_until == "2026-06-02T16:10:00Z"
    assert [event.event_type for event in reconstructed.events][-1] == "custom_event"


def test_state_model_rejects_invalid_transitions():
    assert transition(WorkItemState.QUEUED, WorkItemState.DISPATCHED) == WorkItemState.DISPATCHED
    assert transition(WorkItemState.RUNNING, WorkItemState.BLOCKED_HUMAN) == WorkItemState.BLOCKED_HUMAN

    try:
        transition(WorkItemState.SUCCEEDED, WorkItemState.QUEUED)
    except InvalidTransitionError:
        pass
    else:
        raise AssertionError("terminal succeeded item transitioned back to queued")

    try:
        transition(WorkItemState.QUEUED, WorkItemState.RUNNING)
    except InvalidTransitionError:
        pass
    else:
        raise AssertionError("queued item transitioned directly to running")


def test_from_record_tolerates_mis_serialised_lease_and_metadata():
    # A record written by an old/buggy writer can carry lease/metadata as a STRING
    # (e.g. "{}") instead of a mapping. dict("{}") raises ValueError, which would
    # crash queue reconstruction mid-run. from_record must coerce to an empty dict.
    item = WorkItem.from_record({
        "id": "w1", "state": "queued", "lease": "{}",  # mis-serialised string
    })
    assert item.lease == {}

    item2 = WorkItem.from_record({"id": "w2", "state": "queued", "lease": "garbage"})
    assert item2.lease == {}

    attempt = AttemptRecord.from_record({
        "attempt_id": "a1", "work_id": "w1", "lane": "claude", "metadata": "{}",
    })
    assert attempt.metadata == {}

    # A genuine dict still round-trips untouched.
    ok = WorkItem.from_record({
        "id": "w3", "state": "queued", "lease": {"id": "lease-1", "lane": "claude"},
    })
    assert ok.lease == {"id": "lease-1", "lane": "claude"}
