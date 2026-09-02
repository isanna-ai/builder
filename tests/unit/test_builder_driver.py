"""T4: the single-lease driver supervising loop (dispatch, watch, retry, escalate).

`scripts/builder-driver.py` is hyphenated, so it's loaded via importlib (same
pattern `test_ab_plangate.py` uses for `ab-memory-gain.py`).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_driver_module():
    path = REPO_ROOT / "scripts" / "builder-driver.py"
    spec = importlib.util.spec_from_file_location("builder_driver", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["builder_driver"] = module
    spec.loader.exec_module(module)
    return module


bd = _load_driver_module()


class FakeTurnSource:
    """A scripted `TurnSource`: `decisions` feeds `dispatch_next()` in order
    (repeating the last entry once exhausted), `outcomes[turn_id]` feeds
    `watch()` in order. `retry_calls`/`escalate_calls` record what the driver
    did, so tests can assert on retry-with-feedback / escalate-with-context."""

    def __init__(self, decisions, outcomes):
        self.decisions = list(decisions)
        self.outcomes = {k: list(v) for k, v in outcomes.items()}
        self.retry_calls: list[tuple[str, str]] = []
        self.escalate_calls: list[tuple[str, str]] = []

    def dispatch_next(self):
        if len(self.decisions) > 1:
            return self.decisions.pop(0)
        return self.decisions[0]

    def watch(self, turn_id):
        queue = self.outcomes.get(turn_id, [])
        if len(queue) > 1:
            return queue.pop(0)
        return queue[0]

    def retry(self, turn_id, *, feedback):
        self.retry_calls.append((turn_id, feedback))

    def escalate(self, turn_id, *, feedback):
        self.escalate_calls.append((turn_id, feedback))


def _driver(turn_source, tmp_path, **kwargs):
    lease = bd.DriverLease(tmp_path / "driver.lock", "driver-a")
    return bd.BuilderDriver(turn_source=turn_source, lease=lease, heartbeat_path=tmp_path / "heartbeat.json", **kwargs)


# --- AC-R5-1: exactly one driver holds the lease --------------------------


def test_single_lease_second_driver_does_not_run(tmp_path):
    lock_path = tmp_path / "driver.lock"
    first = bd.DriverLease(lock_path, "driver-a")
    second = bd.DriverLease(lock_path, "driver-b")

    first.acquire()
    try:
        try:
            second.acquire()
            assert False, "expected DriverBusyError while the first lease is live"
        except bd.DriverBusyError:
            pass
    finally:
        first.release()


def test_single_lease_reclaims_a_lease_left_by_a_dead_process(tmp_path):
    lock_path = tmp_path / "driver.lock"
    lock_path.write_text("driver-999999999\n", encoding="utf-8")  # not a live pid

    lease = bd.DriverLease(lock_path, "driver-new")
    lease.acquire()  # must steal the dead lease, not raise
    assert lock_path.read_text(encoding="utf-8").strip() == "driver-new"
    lease.release()


def test_single_lease_released_lease_can_be_reacquired_by_another_driver(tmp_path):
    lock_path = tmp_path / "driver.lock"
    first = bd.DriverLease(lock_path, "driver-a")
    first.acquire()
    first.release()

    second = bd.DriverLease(lock_path, "driver-b")
    second.acquire()  # does not raise
    second.release()


def test_single_lease_same_owner_cannot_steal_a_live_lease(tmp_path):
    lock_path = tmp_path / "driver.lock"
    first = bd.DriverLease(lock_path, "driver-a")
    second = bd.DriverLease(lock_path, "driver-a")
    first.acquire()
    try:
        try:
            second.acquire()
            assert False, "same owner id must not make a live lease look stale"
        except bd.DriverBusyError:
            pass
    finally:
        first.release()


# --- AC-R5-2: dispatch the next ready turn, watch to terminal outcome -----


def test_dispatches_next_turn_and_watches_to_succeeded_outcome(tmp_path):
    ts = FakeTurnSource(
        decisions=[bd.DispatchDecision(kind="turn", turn_id="work-1")],
        outcomes={"work-1": [bd.TurnOutcome(turn_id="work-1", status="succeeded")]},
    )
    driver = _driver(ts, tmp_path)

    outcome = driver.run_once()

    assert outcome.status == "succeeded"
    assert ts.retry_calls == []
    assert ts.escalate_calls == []


def test_idle_when_nothing_is_dispatchable(tmp_path):
    ts = FakeTurnSource(decisions=[bd.DispatchDecision(kind="idle")], outcomes={})
    driver = _driver(ts, tmp_path)

    outcome = driver.run_once()

    assert outcome.status == "idle"


# --- a failed turn is retried WITH validator/verifier feedback attached ---


def test_failed_turn_is_retried_with_feedback_attached_to_next_attempt(tmp_path):
    ts = FakeTurnSource(
        decisions=[bd.DispatchDecision(kind="turn", turn_id="work-2")],
        outcomes={"work-2": [bd.TurnOutcome(
            turn_id="work-2", status="failed", feedback="verify: 2 tests failed in test_foo.py",
        )]},
    )
    driver = _driver(ts, tmp_path, max_retries=3)

    outcome = driver.run_once()

    assert outcome.status == "failed"
    assert ts.retry_calls == [("work-2", "verify: 2 tests failed in test_foo.py")]
    assert ts.escalate_calls == []


# --- retry exhaustion escalates WITH context, instead of stalling ---------


def test_retry_exhaustion_escalates_with_context_instead_of_stalling(tmp_path):
    ts = FakeTurnSource(
        decisions=[bd.DispatchDecision(kind="turn", turn_id="work-3")],
        outcomes={"work-3": [bd.TurnOutcome(turn_id="work-3", status="failed", feedback="attempt failed")]},
    )
    driver = _driver(ts, tmp_path, max_retries=2)

    for _ in range(2):  # attempts 1, 2 -> still within budget -> retried
        outcome = driver.run_once()
        assert outcome.status == "failed"
    assert ts.escalate_calls == []
    assert len(ts.retry_calls) == 2

    outcome = driver.run_once()  # attempt 3 -> budget exhausted -> escalate, not retry
    assert outcome.status == "failed"
    assert ts.escalate_calls == [("work-3", "attempt failed")]
    assert len(ts.retry_calls) == 2  # no additional retry was queued


def test_a_later_success_resets_the_retry_budget_for_a_turn_id(tmp_path):
    ts = FakeTurnSource(
        decisions=[bd.DispatchDecision(kind="turn", turn_id="work-4")],
        outcomes={"work-4": [
            bd.TurnOutcome(turn_id="work-4", status="failed", feedback="first failure"),
            bd.TurnOutcome(turn_id="work-4", status="succeeded"),
        ]},
    )
    driver = _driver(ts, tmp_path, max_retries=1)

    first = driver.run_once()
    assert first.status == "failed"
    second = driver.run_once()
    assert second.status == "succeeded"

    assert driver._retry_counts.get("work-4") is None  # cleared on success


# --- heartbeat is written every cycle (used by the T6 liveness watchdog) --


def test_run_once_writes_a_heartbeat(tmp_path):
    ts = FakeTurnSource(decisions=[bd.DispatchDecision(kind="idle")], outcomes={})
    driver = _driver(ts, tmp_path)

    driver.run_once()

    hb_path = tmp_path / "heartbeat.json"
    assert hb_path.exists()
    import json
    hb = json.loads(hb_path.read_text(encoding="utf-8"))
    assert "at" in hb and "pid" in hb


def test_heartbeat_is_refreshed_while_waiting_for_a_settled_measurement(tmp_path):
    ts = FakeTurnSource(
        decisions=[bd.DispatchDecision(kind="turn", turn_id="work-watch")],
        outcomes={"work-watch": [
            bd.TurnOutcome(turn_id="work-watch", status="failed", settled=False),
            bd.TurnOutcome(turn_id="work-watch", status="succeeded", settled=True),
        ]},
    )
    driver = _driver(ts, tmp_path)
    beats: list[int] = []
    driver._heartbeat = lambda: beats.append(1)

    assert driver.run_once().status == "succeeded"
    assert len(beats) >= 5


def test_scheduler_turn_source_preserves_every_id_from_multi_lane_dispatch():
    class Scheduler:
        def __init__(self):
            self.calls = 0

        def dispatch_once(self):
            self.calls += 1
            return ["work-a", "work-b"]

    scheduler = Scheduler()
    source = bd.SchedulerTurnSource(scheduler)
    assert source.dispatch_next().turn_id == "work-a"
    assert source.dispatch_next().turn_id == "work-b"
    assert scheduler.calls == 1


def test_retry_feedback_is_injected_into_the_next_phase_goal(tmp_path):
    from _dispatch_runtime.phase_runtime import build_phase_goal

    specs_dir = tmp_path / ".builder" / "specs"
    spec_dir = specs_dir / "demo"
    spec_dir.mkdir(parents=True)
    (spec_dir / "spec.yaml").write_text(
        "name: demo\nstatus: implementing\ncurrent_phase: implement\n", encoding="utf-8",
    )
    goal = build_phase_goal(
        tmp_path, specs_dir, "demo", "implement", None,
        retry_feedback="validator: test_widget failed",
    )
    assert "DRIVER RETRY FEEDBACK" in goal
    assert "test_widget failed" in goal


def test_scheduler_retry_persists_feedback_for_the_lane_to_consume(tmp_path):
    from _dispatch_runtime.queue_store import QueueStore

    store = QueueStore(tmp_path)
    item = store.enqueue(task_ref={"spec_id": "demo"})

    class Scheduler:
        pass

    scheduler = Scheduler()
    scheduler.store = store
    source = bd.SchedulerTurnSource(scheduler)
    source.retry(item.id, feedback="validator: test_widget failed")

    updated = store.get_item(item.id)
    assert updated.task_ref["retry_feedback"] == "validator: test_widget failed"
    assert updated.state.value == "queued"


def test_scheduler_watch_does_not_report_blocked_or_cancelled_as_success(tmp_path):
    from _dispatch_runtime.queue_store import QueueStore
    from _dispatch_runtime.state_model import WorkItemState

    store = QueueStore(tmp_path)

    class Scheduler:
        def wait_for_attempts(self, *, timeout):
            return True

    scheduler = Scheduler()
    scheduler.store = store
    source = bd.SchedulerTurnSource(scheduler)

    blocked = store.enqueue(task_ref={"last_error": "human decision required"})
    blocked.state = WorkItemState.BLOCKED_HUMAN
    store.save_item(blocked)
    assert source.watch(blocked.id).status == "blocked_human"

    cancelled = store.enqueue(task_ref={})
    cancelled.state = WorkItemState.CANCELLED
    store.save_item(cancelled)
    assert source.watch(cancelled.id).status == "cancelled"
