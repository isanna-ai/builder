"""M5: per-item pause/continue + terminal-record gc verbs on builder-dispatch.

pause halts ONE queued item (scheduler skips it, since _dispatchable_items only reserves
QUEUED); continue re-queues it; gc removes terminal (succeeded/failed/cancelled) records.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from _yaml import yaml

from _dispatch_runtime import cli
from _dispatch_runtime.config import DispatchConfig, LaneConfig
from _dispatch_runtime.queue_store import QueueStore
from _dispatch_runtime.scheduler import DispatchScheduler
from _dispatch_runtime.state_model import (
    InvalidTransitionError,
    TERMINAL_STATES,
    WorkItemState,
    transition,
)


def dispatch_config(tmp_path) -> DispatchConfig:
    lanes = {
        name: LaneConfig(name=name, provider=name, max_concurrency=1)
        for name in ("codex-cli", "claude-code-cli")
    }
    return DispatchConfig(
        queue_store_path=tmp_path,
        lanes=lanes,
        routing_policy={"default": "ordered", "tie_break": "lane_order"},
        cooldown_policy={"default_seconds": 60},
        retry_policy={"max_attempts": 3, "initial_seconds": 5, "max_seconds": 30, "jitter_seconds": 0},
    )


class RecordingExecutor:
    def __init__(self, result):
        self.result = result
        self.calls: list[tuple] = []

    def execute(self, task_ref, lane_name, attempt_context):
        self.calls.append((task_ref["runner_task_ref"], lane_name, attempt_context))
        return self.result


def success_result():
    from _dispatch_runtime.lane_executor import DispatchResult, DispatchResultType

    return DispatchResult(result_type=DispatchResultType.SUCCESS, metadata={"pid": 41, "logs": []})


def _enqueue(store, ref="runs/task.yaml"):
    return store.enqueue(task_ref={"kind": "builder-runner-task", "runner_task_ref": ref})


def _run(store, *argv):
    return cli.run(list(argv), _store_override=store)


# ------------------------------------------------------------------ pause / continue

def test_pause_makes_item_undispatchable(tmp_path):
    store = QueueStore(tmp_path)
    item = _enqueue(store)
    assert _run(store, "pause", item.id) == 0
    assert store.get_item(item.id).state == WorkItemState.PAUSED
    executor = RecordingExecutor(success_result())
    scheduler = DispatchScheduler(store, dispatch_config(tmp_path), executor, owner_id="s")
    scheduler.dispatch_once()
    scheduler.wait_for_attempts()
    assert executor.calls == []  # a paused item is never dispatched


def test_continue_requeues_and_dispatches(tmp_path):
    store = QueueStore(tmp_path)
    item = _enqueue(store)
    _run(store, "pause", item.id)
    assert _run(store, "continue", item.id) == 0
    assert store.get_item(item.id).state == WorkItemState.QUEUED
    executor = RecordingExecutor(success_result())
    scheduler = DispatchScheduler(store, dispatch_config(tmp_path), executor, owner_id="s")
    scheduler.dispatch_once()
    scheduler.wait_for_attempts()
    assert len(executor.calls) == 1  # continued item dispatches normally


def test_pause_refuses_non_queued(tmp_path):
    store = QueueStore(tmp_path)
    item = _enqueue(store)
    assert _run(store, "cancel", item.id) == 0
    assert _run(store, "pause", item.id) == 1  # cancelled -> cannot pause
    assert store.get_item(item.id).state == WorkItemState.CANCELLED


def test_continue_refuses_non_paused(tmp_path):
    store = QueueStore(tmp_path)
    item = _enqueue(store)
    assert _run(store, "continue", item.id) == 1  # queued -> cannot continue
    assert store.get_item(item.id).state == WorkItemState.QUEUED


def test_pause_continue_not_found(tmp_path):
    store = QueueStore(tmp_path)
    assert _run(store, "pause", "work-nope") == 1
    assert _run(store, "continue", "work-nope") == 1


# ------------------------------------------------------------------ gc

def test_gc_removes_terminal_keeps_active(tmp_path):
    store = QueueStore(tmp_path)
    queued = _enqueue(store, "q")
    paused = _enqueue(store, "p")
    failed = _enqueue(store, "f")
    cancelled = _enqueue(store, "c")
    store.transition_item(paused.id, WorkItemState.PAUSED)
    store.transition_item(failed.id, WorkItemState.FAILED)
    store.transition_item(cancelled.id, WorkItemState.CANCELLED)
    assert _run(store, "gc") == 0
    assert store.get_item(failed.id) is None
    assert store.get_item(cancelled.id) is None
    assert store.get_item(queued.id) is not None  # active kept
    assert store.get_item(paused.id) is not None  # paused is non-terminal -> kept


def test_gc_dry_run_deletes_nothing(tmp_path):
    store = QueueStore(tmp_path)
    cancelled = _enqueue(store, "c")
    store.transition_item(cancelled.id, WorkItemState.CANCELLED)
    assert _run(store, "gc", "--dry-run") == 0
    assert store.get_item(cancelled.id) is not None


def test_gc_older_than_days_keeps_fresh_removes_old(tmp_path):
    store = QueueStore(tmp_path)
    cancelled = _enqueue(store, "c")
    store.transition_item(cancelled.id, WorkItemState.CANCELLED)
    # updated_at is 'now' -> newer than a 1-day cutoff -> kept
    assert _run(store, "gc", "--older-than-days", "1") == 0
    assert store.get_item(cancelled.id) is not None
    # age it 5 days -> now older than the 1-day cutoff -> removed
    item = store.get_item(cancelled.id)
    item.updated_at = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat().replace("+00:00", "Z")
    store._write_item(item)  # raw write; save_item would re-stamp updated_at to now
    assert _run(store, "gc", "--older-than-days", "1") == 0
    assert store.get_item(cancelled.id) is None


def test_gc_removes_matching_attempts_and_events_keeps_others(tmp_path):
    store = QueueStore(tmp_path)
    survivor = _enqueue(store, "s")
    doomed = _enqueue(store, "d")
    store.record_attempt(survivor.id, attempt_id="attempt-survivor", lane="claude-code-cli")
    store.record_attempt(doomed.id, attempt_id="attempt-doomed", lane="claude-code-cli")
    store.transition_item(doomed.id, WorkItemState.FAILED)

    assert _run(store, "gc") == 0

    assert store.get_item(doomed.id) is None
    assert store.get_item(survivor.id) is not None
    remaining_attempts = {p.stem for p in store.attempts_dir.glob("*.yaml")}
    assert "attempt-doomed" not in remaining_attempts
    assert "attempt-survivor" in remaining_attempts
    remaining_event_work_ids = {
        yaml.safe_load(path.read_text())["work_id"] for path in store.events_dir.glob("*.yaml")
    }
    assert doomed.id not in remaining_event_work_ids
    assert survivor.id in remaining_event_work_ids


def test_gc_orphans_removes_attempts_events_with_no_work_item(tmp_path):
    store = QueueStore(tmp_path)
    live = _enqueue(store, "live")
    store.record_attempt(live.id, attempt_id="attempt-live", lane="claude-code-cli")
    # Simulate a pre-existing orphan (e.g. from a gc run that predates this fix,
    # or a manually deleted item): an attempt/event whose work item is already gone.
    store.record_attempt("work-ghost", attempt_id="attempt-ghost", lane="claude-code-cli")
    store.append_event("work-ghost", "attempt_recorded", {"attempt_id": "attempt-ghost"})

    # Without --orphans, gc only ever touches records tied to items it removes THIS run.
    assert _run(store, "gc") == 0
    assert (store.attempts_dir / "attempt-ghost.yaml").exists()

    assert _run(store, "gc", "--orphans") == 0
    assert not (store.attempts_dir / "attempt-ghost.yaml").exists()
    assert (store.attempts_dir / "attempt-live.yaml").exists()
    assert store.get_item(live.id) is not None


def test_gc_orphans_dry_run_deletes_nothing(tmp_path):
    store = QueueStore(tmp_path)
    store.record_attempt("work-ghost", attempt_id="attempt-ghost", lane="claude-code-cli")
    assert _run(store, "gc", "--orphans", "--dry-run") == 0
    assert (store.attempts_dir / "attempt-ghost.yaml").exists()


# ------------------------------------------------------------------ state model

def test_state_model_pause_transitions():
    assert transition("queued", "paused") == WorkItemState.PAUSED
    assert transition("paused", "queued") == WorkItemState.QUEUED
    assert transition("paused", "cancelled") == WorkItemState.CANCELLED
    assert WorkItemState.PAUSED not in TERMINAL_STATES
    for illegal in ("dispatched", "running", "succeeded", "failed"):
        try:
            transition("paused", illegal)
        except InvalidTransitionError:
            continue
        raise AssertionError(f"paused -> {illegal} must be illegal")
