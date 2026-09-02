"""Tests for T6: immutable events, read-only status views, operator state transitions."""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from _dispatch_runtime.events import (
    EventType,
    append_cooldown_open_event,
    append_enqueue_event,
    append_heartbeat_event,
    append_human_block_event,
    append_lease_acquired_event,
    append_process_start_event,
    append_result_event,
)
from _dispatch_runtime.queue_store import QueueStore
from _dispatch_runtime.state_model import WorkItemState
from _dispatch_runtime.status import (
    StatusSnapshot,
    build_status_snapshot,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _future(seconds: int) -> str:
    dt = datetime.now(timezone.utc) + timedelta(seconds=seconds)
    return dt.isoformat().replace("+00:00", "Z")


def _past(seconds: int) -> str:
    dt = datetime.now(timezone.utc) - timedelta(seconds=seconds)
    return dt.isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Event appender tests
# ---------------------------------------------------------------------------

def test_append_enqueue_event_writes_immutable_record(tmp_path):
    store = QueueStore(tmp_path)
    item = store.enqueue(task_ref={"kind": "generic", "ref": "t1"})
    event = append_enqueue_event(store, item.id, lane="codex-cli")
    assert event.event_type == EventType.ENQUEUE
    assert event.work_id == item.id
    assert event.payload["lane"] == "codex-cli"
    # Re-reading should return same record (immutable)
    snap = store.reconstruct()
    persisted = next(e for e in snap.events if e.event_id == event.event_id)
    assert persisted.event_type == EventType.ENQUEUE


def test_append_lease_acquired_event(tmp_path):
    store = QueueStore(tmp_path)
    item = store.enqueue(task_ref={"kind": "generic", "ref": "t2"})
    event = append_lease_acquired_event(store, item.id, lane="codex-cli", attempt_id="attempt-1")
    assert event.event_type == EventType.LEASE_ACQUIRED
    assert event.payload["attempt_id"] == "attempt-1"


def test_append_process_start_event(tmp_path):
    store = QueueStore(tmp_path)
    item = store.enqueue(task_ref={"kind": "generic", "ref": "t3"})
    event = append_process_start_event(store, item.id, attempt_id="attempt-1", pid=1234)
    assert event.event_type == EventType.PROCESS_START
    assert event.payload["pid"] == 1234


def test_append_heartbeat_event(tmp_path):
    store = QueueStore(tmp_path)
    item = store.enqueue(task_ref={"kind": "generic", "ref": "t4"})
    event = append_heartbeat_event(store, item.id, attempt_id="attempt-1")
    assert event.event_type == EventType.HEARTBEAT
    assert "heartbeat_at" in event.payload


def test_append_result_event(tmp_path):
    store = QueueStore(tmp_path)
    item = store.enqueue(task_ref={"kind": "generic", "ref": "t5"})
    event = append_result_event(store, item.id, attempt_id="attempt-1", result_type="success")
    assert event.event_type == EventType.RESULT
    assert event.payload["result_type"] == "success"


def test_append_cooldown_open_event(tmp_path):
    store = QueueStore(tmp_path)
    item = store.enqueue(task_ref={"kind": "generic", "ref": "t6"})
    event = append_cooldown_open_event(store, item.id, lane="codex-cli", until=_future(300))
    assert event.event_type == EventType.COOLDOWN_OPEN
    assert event.payload["lane"] == "codex-cli"


def test_append_human_block_event(tmp_path):
    store = QueueStore(tmp_path)
    item = store.enqueue(task_ref={"kind": "generic", "ref": "t7"})
    event = append_human_block_event(store, item.id, attempt_id="attempt-1")
    assert event.event_type == EventType.HUMAN_BLOCK
    assert event.work_id == item.id


# ---------------------------------------------------------------------------
# Status snapshot tests — read-only, derived from persisted records
# ---------------------------------------------------------------------------

def test_status_reports_queue_depth_by_state(tmp_path):
    store = QueueStore(tmp_path)
    store.enqueue(task_ref={"kind": "generic", "ref": "q1"})
    store.enqueue(task_ref={"kind": "generic", "ref": "q2"})
    item3 = store.enqueue(task_ref={"kind": "generic", "ref": "q3"})
    store.transition_item(item3.id, WorkItemState.DISPATCHED, lease={"id": "L1", "lane": "codex-cli"})
    store.transition_item(item3.id, WorkItemState.RUNNING)

    snap: StatusSnapshot = build_status_snapshot(store)
    assert snap.queue_depth["queued"] == 2
    assert snap.queue_depth["running"] == 1
    assert snap.queue_depth.get("succeeded", 0) == 0


def test_status_reports_per_lane_in_flight_counts(tmp_path):
    store = QueueStore(tmp_path)
    item1 = store.enqueue(task_ref={"kind": "g", "ref": "1"}, lane="codex-cli")
    store.transition_item(item1.id, WorkItemState.DISPATCHED, lease={"id": "L1", "lane": "codex-cli"})
    store.transition_item(item1.id, WorkItemState.RUNNING)

    item2 = store.enqueue(task_ref={"kind": "g", "ref": "2"}, lane="claude-code-cli")
    store.transition_item(item2.id, WorkItemState.DISPATCHED, lease={"id": "L2", "lane": "claude-code-cli"})

    snap = build_status_snapshot(store)
    assert snap.lane_inflight["codex-cli"] == 1
    assert snap.lane_inflight["claude-code-cli"] == 1


def test_status_reports_cooldown_remaining(tmp_path):
    store = QueueStore(tmp_path)
    store.set_lane_cooldown("codex-cli", until=_future(300), reason="rate_limited")
    store.set_lane_cooldown("claude-code-cli", until=_past(10), reason="rate_limited")

    snap = build_status_snapshot(store)
    assert snap.lane_cooldown_remaining["codex-cli"] > 0
    assert snap.lane_cooldown_remaining.get("claude-code-cli", 0) == 0


def test_status_reports_current_attempt_heartbeat_age(tmp_path):
    store = QueueStore(tmp_path)
    item = store.enqueue(task_ref={"kind": "g", "ref": "1"}, lane="codex-cli")
    store.transition_item(item.id, WorkItemState.DISPATCHED, lease={"id": "L1", "lane": "codex-cli"})
    store.transition_item(item.id, WorkItemState.RUNNING)
    heartbeat_ts = _past(30)
    store.record_attempt(
        item.id,
        attempt_id="attempt-heartbeat",
        lane="codex-cli",
        metadata={"heartbeat_at": heartbeat_ts, "log_path": "queue/attempts/attempt-heartbeat.log"},
    )

    snap = build_status_snapshot(store)
    hb = snap.attempt_heartbeats.get("attempt-heartbeat")
    assert hb is not None
    assert hb >= 28  # at least ~30 s ago


def test_status_reports_bounded_log_references(tmp_path):
    store = QueueStore(tmp_path)
    item = store.enqueue(task_ref={"kind": "g", "ref": "1"}, lane="codex-cli")
    store.record_attempt(
        item.id,
        attempt_id="attempt-logs",
        lane="codex-cli",
        metadata={"log_path": "queue/attempts/attempt-logs.log"},
    )

    snap = build_status_snapshot(store)
    log_refs = snap.attempt_log_refs.get("attempt-logs")
    assert log_refs == "queue/attempts/attempt-logs.log"


def test_status_does_not_mutate_state(tmp_path):
    store = QueueStore(tmp_path)
    item = store.enqueue(task_ref={"kind": "g", "ref": "1"})
    before = store.get_item(item.id)

    build_status_snapshot(store)

    after = store.get_item(item.id)
    assert before is not None and after is not None
    assert before.state == after.state
    assert before.updated_at == after.updated_at


def test_status_reports_recent_events(tmp_path):
    store = QueueStore(tmp_path)
    item = store.enqueue(task_ref={"kind": "g", "ref": "1"})
    append_enqueue_event(store, item.id)
    append_result_event(store, item.id, attempt_id="a1", result_type="success")

    snap = build_status_snapshot(store)
    assert len(snap.recent_events) >= 2
    types = {e.event_type for e in snap.recent_events}
    assert EventType.ENQUEUE in types
    assert EventType.RESULT in types


# ---------------------------------------------------------------------------
# Operator state transition tests — cancel, drain, lanes pause/resume
# ---------------------------------------------------------------------------

def test_cancel_transitions_queued_item_to_cancelled(tmp_path):
    store = QueueStore(tmp_path)
    item = store.enqueue(task_ref={"kind": "g", "ref": "1"})

    from _dispatch_runtime.cli import run
    run(["--config", "nonexistent.yaml", "cancel", item.id], _store_override=store)

    updated = store.get_item(item.id)
    assert updated is not None
    assert updated.state == WorkItemState.CANCELLED


def test_cancel_transitions_running_item_to_cancelled(tmp_path):
    store = QueueStore(tmp_path)
    item = store.enqueue(task_ref={"kind": "g", "ref": "1"})
    store.transition_item(item.id, WorkItemState.DISPATCHED, lease={"id": "L1", "lane": "codex-cli"})
    store.transition_item(item.id, WorkItemState.RUNNING)

    from _dispatch_runtime.cli import run
    run(["--config", "nonexistent.yaml", "cancel", item.id], _store_override=store)

    updated = store.get_item(item.id)
    assert updated is not None
    assert updated.state == WorkItemState.CANCELLED


def test_cancel_refuses_terminal_item(tmp_path):
    store = QueueStore(tmp_path)
    item = store.enqueue(task_ref={"kind": "g", "ref": "1"})
    store.transition_item(item.id, WorkItemState.DISPATCHED, lease={"id": "L1", "lane": "codex-cli"})
    store.transition_item(item.id, WorkItemState.RUNNING)
    store.transition_item(item.id, WorkItemState.SUCCEEDED)

    from _dispatch_runtime.cli import run
    result = run(["--config", "nonexistent.yaml", "cancel", item.id], _store_override=store)
    assert result != 0


def test_drain_writes_drain_flag_file(tmp_path):
    store = QueueStore(tmp_path)

    from _dispatch_runtime.cli import run
    run(["--config", "nonexistent.yaml", "drain"], _store_override=store)

    drain_flag = store.queue_dir / ".drain"
    assert drain_flag.exists()


def test_lanes_pause_writes_paused_marker(tmp_path):
    store = QueueStore(tmp_path)

    from _dispatch_runtime.cli import run
    run(["--config", "nonexistent.yaml", "lanes", "pause", "codex-cli"], _store_override=store)

    paused_path = store.lanes_dir / "codex-cli.paused"
    assert paused_path.exists()


def test_lanes_resume_removes_paused_marker(tmp_path):
    store = QueueStore(tmp_path)
    (store.lanes_dir / "codex-cli.paused").write_text("paused", encoding="utf-8")

    from _dispatch_runtime.cli import run
    run(["--config", "nonexistent.yaml", "lanes", "resume", "codex-cli"], _store_override=store)

    paused_path = store.lanes_dir / "codex-cli.paused"
    assert not paused_path.exists()
