from __future__ import annotations

import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from _dispatch_runtime.config import DispatchConfig, LaneConfig
from _dispatch_runtime.queue_store import QueueStore
from _dispatch_runtime.scheduler import DispatchScheduler, SchedulerBusyError
from _dispatch_runtime.state_model import TERMINAL_STATES, WorkItemState


def iso_at(offset_seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=offset_seconds)).isoformat().replace("+00:00", "Z")


class RecordingExecutor:
    def __init__(self, result):
        self.result = result
        self.calls: list[tuple[str, str, dict[str, object]]] = []

    def execute(self, task_ref, lane_name: str, attempt_context):
        self.calls.append((task_ref["runner_task_ref"], lane_name, attempt_context))
        return self.result


class BlockingExecutor:
    def __init__(self, result):
        self.result = result
        self.started = threading.Event()
        self.release = threading.Event()
        self.completed = threading.Event()
        self.calls: list[tuple[str, str, dict[str, object]]] = []

    def execute(self, task_ref, lane_name: str, attempt_context):
        self.calls.append((task_ref["runner_task_ref"], lane_name, attempt_context))
        self.started.set()
        self.release.wait(timeout=2)
        self.completed.set()
        return self.result


def dispatch_config(tmp_path, *, lane_order: list[str] | None = None, max_concurrency: int = 1) -> DispatchConfig:
    ordered = lane_order or ["codex-cli", "claude-code-cli"]
    lanes = {
        name: LaneConfig(name=name, provider=name, max_concurrency=max_concurrency)
        for name in ordered
    }
    return DispatchConfig(
        queue_store_path=tmp_path,
        lanes=lanes,
        routing_policy={"default": "ordered", "tie_break": "lane_order"},
        cooldown_policy={"default_seconds": 60},
        retry_policy={"max_attempts": 3, "initial_seconds": 5, "max_seconds": 30, "jitter_seconds": 0},
    )


def success_result():
    from _dispatch_runtime.lane_executor import DispatchResult, DispatchResultType

    return DispatchResult(
        result_type=DispatchResultType.SUCCESS,
        metadata={"pid": 41, "logs": ["logs/attempt-1.txt"]},
    )


def wait_for(predicate, *, timeout: float = 1.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def test_scheduler_acquires_store_lock_before_dispatch_and_rejects_second_owner(tmp_path):
    store = QueueStore(tmp_path)
    item = store.enqueue(task_ref={"kind": "builder-runner-task", "runner_task_ref": "runs/task-T3.yaml"})
    executor = RecordingExecutor(success_result())
    scheduler = DispatchScheduler(store, dispatch_config(tmp_path), executor, owner_id="scheduler-a")
    other = DispatchScheduler(store, dispatch_config(tmp_path), executor, owner_id="scheduler-b")

    with scheduler.scheduler_lock():
        try:
            other.acquire_scheduler_lock(wait=False)
        except SchedulerBusyError:
            pass
        else:
            raise AssertionError("expected a busy scheduler lock for the second owner")

        leased = store.get_item(item.id)
        assert leased is not None
        assert leased.state == WorkItemState.QUEUED

    scheduler.dispatch_once()
    assert scheduler.wait_for_attempts()

    dispatched = store.get_item(item.id)
    assert dispatched is not None
    assert dispatched.state == WorkItemState.SUCCEEDED
    assert executor.calls


def test_scheduler_acquires_item_lease_before_executor_launch_and_routes_by_explicit_hint(tmp_path):
    store = QueueStore(tmp_path)
    item = store.enqueue(
        task_ref={"kind": "builder-runner-task", "runner_task_ref": "runs/task-T3.yaml"},
        lane="claude-code-cli",
    )
    executor = RecordingExecutor(success_result())
    scheduler = DispatchScheduler(store, dispatch_config(tmp_path), executor, owner_id="scheduler-a")

    scheduler.dispatch_once()
    assert scheduler.wait_for_attempts()

    assert executor.calls[0][1] == "claude-code-cli"
    leased_item = store.get_item(item.id)
    assert leased_item is not None
    assert leased_item.attempt == 1
    assert leased_item.lane == "claude-code-cli"

    attempts = list(store.reconstruct().attempts.values())
    assert len(attempts) == 1
    attempt = attempts[0]
    assert attempt.attempt_id.startswith("attempt-")
    assert attempt.metadata["pid"] == 41


def test_scheduler_returns_without_waiting_for_completion_and_marks_running_for_in_flight_work(tmp_path):
    store = QueueStore(tmp_path)
    item = store.enqueue(task_ref={"kind": "builder-runner-task", "runner_task_ref": "runs/task-T3-async.yaml"})
    executor = BlockingExecutor(success_result())
    scheduler = DispatchScheduler(store, dispatch_config(tmp_path), executor, owner_id="scheduler-a")

    dispatch_thread = threading.Thread(target=scheduler.dispatch_once)
    dispatch_thread.start()

    assert executor.started.wait(timeout=1), "expected executor launch to start"
    dispatch_thread.join(timeout=0.2)
    assert not dispatch_thread.is_alive(), "dispatch_once should not block on completion"

    running_item = store.get_item(item.id)
    assert running_item is not None
    assert running_item.state == WorkItemState.RUNNING
    attempt = store.reconstruct().attempts[running_item.lease["attempt_id"]]
    assert attempt.metadata["work_id"] == item.id
    assert attempt.metadata["started_at"]
    assert attempt.metadata["log_path"].endswith(".log")

    executor.release.set()
    assert wait_for(lambda: executor.completed.is_set())
    assert wait_for(lambda: store.get_item(item.id).state == WorkItemState.SUCCEEDED)
    assert scheduler.wait_for_attempts()


def test_scheduler_rejects_unknown_explicit_lane_hints_as_terminal_routing_errors(tmp_path):
    store = QueueStore(tmp_path)
    item = store.enqueue(
        task_ref={"kind": "builder-runner-task", "runner_task_ref": "runs/task-T3.yaml"},
        lane="missing-lane",
    )
    executor = RecordingExecutor(success_result())
    scheduler = DispatchScheduler(store, dispatch_config(tmp_path), executor, owner_id="scheduler-a")

    scheduler.dispatch_once()

    updated = store.get_item(item.id)
    assert updated is not None
    assert updated.state == WorkItemState.FAILED
    assert updated.lease == {}
    assert "unknown lane hint" in updated.task_ref["last_error"]
    assert executor.calls == []


def test_terminal_retry_exhaustion_emits_spec_failed_notification(tmp_path):
    # audit A1: a spec that exhausts its retries (or rate-limits to terminal) must
    # ALERT — without the notify the queue dead-ends silently (the Wave-0 stall).
    from _dispatch_runtime.lane_executor import DispatchResult, DispatchResultType

    class RecordingNotifier:
        def __init__(self):
            self.events: list[tuple[str, dict]] = []

        def notify(self, kind, packet):
            self.events.append((kind, packet))

    store = QueueStore(tmp_path)
    item = store.enqueue(task_ref={"kind": "builder-runner-task", "runner_task_ref": "runs/task-T3.yaml"})
    cfg = dispatch_config(tmp_path)
    cfg.retry_policy["max_attempts"] = 1  # first retryable error exhausts → FAILED
    notifier = RecordingNotifier()
    executor = RecordingExecutor(DispatchResult(
        result_type=DispatchResultType.RETRYABLE_ERROR,
        metadata={"spec_id": "demo-spec", "phase": "3-implement", "message": "cli-failed"},
    ))
    scheduler = DispatchScheduler(
        store, cfg, executor, owner_id="scheduler-a", notifier=notifier,
    )

    scheduler.dispatch_once()
    assert scheduler.wait_for_attempts()

    updated = store.get_item(item.id)
    assert updated is not None and updated.state == WorkItemState.FAILED
    kinds = [k for k, _ in notifier.events]
    assert "spec_failed" in kinds, f"expected a spec_failed alert on terminal FAILED, got {kinds}"
    packet = next(p for k, p in notifier.events if k == "spec_failed")
    assert packet["spec_id"] == "demo-spec"
    assert packet["work_id"] == item.id
    assert packet["max_attempts"] == 1


def test_rate_limited_result_is_non_terminal_and_does_not_consume_retry_budget(tmp_path):
    # audit A2: a RATE_LIMITED result is transient throttling, NOT a failure. It must
    # re-QUEUE the item (deferred past the lane cooldown), put the lane on cooldown,
    # fire NO spec_failed notify, and — even with max_attempts=1 — repeated rate-limits
    # must NEVER reach FAILED nor consume the finite retry budget meant for REAL errors.
    from _dispatch_runtime.lane_executor import DispatchResult, DispatchResultType

    class RecordingNotifier:
        def __init__(self):
            self.events: list[tuple[str, dict]] = []

        def notify(self, kind, packet):
            self.events.append((kind, packet))

    store = QueueStore(tmp_path)
    item = store.enqueue(
        task_ref={"kind": "builder-runner-task", "runner_task_ref": "runs/task-T3.yaml"},
        lane="claude-code-cli",
    )
    cfg = dispatch_config(tmp_path)
    cfg.retry_policy["max_attempts"] = 1  # one real error would exhaust → FAILED; a rate-limit must NOT
    notifier = RecordingNotifier()
    executor = RecordingExecutor(DispatchResult(
        result_type=DispatchResultType.RATE_LIMITED,
        metadata={"spec_id": "demo-spec", "phase": "3-implement", "message": "max-throttled"},
    ))
    scheduler = DispatchScheduler(store, cfg, executor, owner_id="scheduler-a", notifier=notifier)

    now = datetime.now(timezone.utc)
    scheduler.dispatch_once()
    assert scheduler.wait_for_attempts()

    requeued = store.get_item(item.id)
    assert requeued is not None
    # NON-TERMINAL: back to QUEUED, lease released, never FAILED.
    assert requeued.state == WorkItemState.QUEUED
    assert requeued.lease == {}
    # Budget NOT consumed: lease bumped attempt to 1, the rate-limit re-queue nets it
    # back to 0 so the re-lease lands on the same attempt (no progress toward FAILED).
    assert requeued.attempt == 0
    assert requeued.task_ref["rate_limit_count"] == 1
    # Deferred until the lane cooldown clears (cooldown_policy default_seconds=60).
    assert requeued.scheduled_after is not None
    scheduled = datetime.fromisoformat(requeued.scheduled_after.replace("Z", "+00:00"))
    assert scheduled > now
    # Lane is on observable cooldown for ~60s.
    lane = store.reconstruct().lanes["claude-code-cli"]
    assert lane.cooldown_until is not None
    assert lane.reason == "rate_limited"
    # NO failure alert fired.
    assert [k for k, _ in notifier.events if k == "spec_failed"] == []

    # Drive SEVERAL more consecutive rate-limits: still never FAILED, still no budget burn.
    # Clear the lane cooldown each round so the item is dispatchable again (in production
    # the wall-clock cooldown elapses; here we fast-forward by reopening the lane).
    for expected_count in (2, 3, 4):
        current = store.get_item(item.id)
        current.scheduled_after = None  # fast-forward past the cooldown window
        store.save_item(current)
        store.set_lane_cooldown("claude-code-cli", until=iso_at(-1), reason="rate_limited")

        scheduler.dispatch_once()
        assert scheduler.wait_for_attempts()

        after = store.get_item(item.id)
        assert after.state == WorkItemState.QUEUED, f"rate-limit #{expected_count} must not be terminal"
        assert after.attempt == 0, f"rate-limit #{expected_count} must not consume the attempt budget"
        assert after.task_ref["rate_limit_count"] == expected_count

    assert [k for k, _ in notifier.events if k == "spec_failed"] == []
    assert store.get_item(item.id).state != WorkItemState.FAILED


def test_scheduler_uses_deterministic_tie_break_and_filters_cooled_down_or_at_capacity_lanes(tmp_path):
    store = QueueStore(tmp_path)
    first = store.enqueue(task_ref={"kind": "builder-runner-task", "runner_task_ref": "runs/task-T3-first.yaml"})
    second = store.enqueue(task_ref={"kind": "builder-runner-task", "runner_task_ref": "runs/task-T3-second.yaml"})
    store.set_lane_cooldown("codex-cli", until=iso_at(300), reason="rate_limited")
    busy = store.enqueue(
        task_ref={"kind": "builder-runner-task", "runner_task_ref": "runs/inflight.yaml"},
        lane="claude-code-cli",
    )
    store.transition_item(
        busy.id,
        WorkItemState.DISPATCHED,
        lease={"id": "lease-busy", "attempt_id": "attempt-busy", "lane": "claude-code-cli", "expires_at": iso_at(300)},
    )
    executor = RecordingExecutor(success_result())
    scheduler = DispatchScheduler(store, dispatch_config(tmp_path), executor, owner_id="scheduler-a")

    scheduler.dispatch_once()

    assert executor.calls == []
    assert store.get_item(first.id).state == WorkItemState.QUEUED
    assert store.get_item(second.id).state == WorkItemState.QUEUED

    store.transition_item(busy.id, WorkItemState.QUEUED, lease={})
    scheduler.dispatch_once()
    assert scheduler.wait_for_attempts()

    assert executor.calls[0][0] == "runs/task-T3-first.yaml"
    assert executor.calls[0][1] == "claude-code-cli"


def test_scheduler_reclaims_expired_or_orphaned_leases_before_redispatch(tmp_path):
    store = QueueStore(tmp_path)
    item = store.enqueue(task_ref={"kind": "builder-runner-task", "runner_task_ref": "runs/task-T3.yaml"})
    store.transition_item(
        item.id,
        WorkItemState.DISPATCHED,
        lease={"id": "lease-expired", "attempt_id": "attempt-expired", "lane": "codex-cli", "expires_at": iso_at(-5)},
    )
    executor = RecordingExecutor(success_result())
    scheduler = DispatchScheduler(store, dispatch_config(tmp_path), executor, owner_id="scheduler-a")

    reclaimed = scheduler.reclaim_stale_leases()
    assert reclaimed == [item.id]
    assert store.get_item(item.id).state == WorkItemState.QUEUED

    store.transition_item(
        item.id,
        WorkItemState.DISPATCHED,
        lease={"id": "lease-orphan", "attempt_id": "attempt-orphan", "lane": "codex-cli", "expires_at": iso_at(300)},
    )
    scheduler.reclaim_stale_leases(active_attempt_ids=set())

    assert store.get_item(item.id).state == WorkItemState.QUEUED


def _write_phase_log(tmp_path, spec: str, succeeded_phases: list[str]) -> None:
    from _yaml import yaml

    specdir = tmp_path / ".builder" / "specs" / spec
    specdir.mkdir(parents=True, exist_ok=True)
    (specdir / "phase-log.yaml").write_text(
        yaml.safe_dump({"phases": [{"phase": p, "outcome": "SUCCEEDED"} for p in succeeded_phases]}),
        encoding="utf-8",
    )


def _write_dependencies(tmp_path, spec: str, deps: list[dict]) -> None:
    from _yaml import yaml

    specdir = tmp_path / ".builder" / "specs" / spec
    specdir.mkdir(parents=True, exist_ok=True)
    (specdir / "dependencies.yaml").write_text(
        yaml.safe_dump({"artifact": "dependencies", "spec": spec, "dependencies": deps}),
        encoding="utf-8",
    )


def _phase_item(store, spec: str, phase: str):
    return store.enqueue(
        task_ref={
            "kind": "builder-phase-batch",
            "runner_task_ref": f".builder/specs/{spec}/runs/phase-{phase}.yaml",
            "spec_id": spec,
        }
    )


def test_reap_cancels_queued_items_whose_phase_already_succeeded(tmp_path):
    """The interrupt->resume duplicate-item bug: a QUEUED item for a phase that is
    already SUCCEEDED in the phase-log must be reaped (cancelled), while an item for
    an unfinished phase survives."""
    store = QueueStore(tmp_path)
    spec = "demo-spec"
    _write_phase_log(tmp_path, spec, ["spec", "spec-review", "plan", "implement"])
    stale = _phase_item(store, spec, "plan")          # plan already SUCCEEDED -> stale
    live = _phase_item(store, spec, "adversarial-review")  # not yet done -> survives
    scheduler = DispatchScheduler(
        store, dispatch_config(tmp_path), RecordingExecutor(success_result()),
        owner_id="scheduler-a", project_dir=tmp_path,
    )

    reaped = scheduler.reap_completed_phase_items()

    assert stale.id in reaped and live.id not in reaped
    assert store.get_item(stale.id).state == WorkItemState.CANCELLED
    assert store.get_item(live.id).state == WorkItemState.QUEUED
    # idempotent: a second pass reaps nothing
    assert scheduler.reap_completed_phase_items() == []


def test_cross_family_review_returns_to_original_author_lane(tmp_path):
    """A review lane is a temporary independent detour, not the new author lane."""
    spec = "persona-routing"
    specdir = tmp_path / ".builder" / "specs" / spec
    specdir.mkdir(parents=True, exist_ok=True)
    (specdir / "spec.yaml").write_text(
        "name: persona-routing\n"
        "status: implementing\n"
        "current_phase: adversarial-review\n"
        "reviews: 1\n",
        encoding="utf-8",
    )
    store = QueueStore(tmp_path / ".builder" / "dispatch-queue")
    cfg = dispatch_config(store.root)
    cfg.pipeline.update({
        "default_lane": "codex-cli",
        "reviews": {"enabled": True, "default": 1, "lane": "codex-cli"},
    })
    scheduler = DispatchScheduler(
        store,
        cfg,
        RecordingExecutor(success_result()),
        owner_id="scheduler-a",
        project_dir=tmp_path,
    )
    implement = store.enqueue(
        task_ref={
            "kind": "builder-phase-batch",
            "runner_task_ref": f".builder/specs/{spec}/runs/phase-implement.yaml",
            "spec_id": spec,
        },
        lane="codex-cli",
    )

    scheduler._advance_after_success(implement, "implement")
    review = next(
        item for item in store.reconstruct().items.values()
        if "phase-adversarial-review.yaml" in str(item.task_ref.get("runner_task_ref"))
    )
    assert review.lane == "claude-code-cli"
    assert review.task_ref["author_lane"] == "codex-cli"

    scheduler._advance_after_success(review, "adversarial-review")
    review_fix = next(
        item for item in store.reconstruct().items.values()
        if "phase-review-fix.yaml" in str(item.task_ref.get("runner_task_ref"))
    )
    assert review_fix.lane == "codex-cli"


def test_reap_only_touches_queued_items(tmp_path):
    """A live exec holds DISPATCHED/RUNNING; the reaper must not cancel it even if
    its phase shows SUCCEEDED (reclaim_stale_leases handles dead leases first)."""
    store = QueueStore(tmp_path)
    spec = "demo-spec"
    _write_phase_log(tmp_path, spec, ["plan"])
    item = _phase_item(store, spec, "plan")
    store.transition_item(item.id, WorkItemState.DISPATCHED)
    scheduler = DispatchScheduler(
        store, dispatch_config(tmp_path), RecordingExecutor(success_result()),
        owner_id="scheduler-a", project_dir=tmp_path,
    )

    assert scheduler.reap_completed_phase_items() == []
    assert store.get_item(item.id).state == WorkItemState.DISPATCHED


def _write_spec(tmp_path, spec: str, *, status: str, current_phase: str) -> None:
    from _yaml import yaml

    specdir = tmp_path / ".builder" / "specs" / spec
    specdir.mkdir(parents=True, exist_ok=True)
    (specdir / "spec.yaml").write_text(
        yaml.safe_dump({"status": status, "current_phase": current_phase}),
        encoding="utf-8",
    )


def _reaper_scheduler(tmp_path):
    return DispatchScheduler(
        QueueStore(tmp_path), dispatch_config(tmp_path), RecordingExecutor(success_result()),
        owner_id="scheduler-a", project_dir=tmp_path,
    )


def _nonterminal_phase_items(store, phase: str):
    return [
        it for it in store.reconstruct().items.values()
        if it.state not in TERMINAL_STATES
        and f"phase-{phase}." in str(it.task_ref.get("runner_task_ref", ""))
    ]


def test_reap_reenqueues_stranded_live_phase_once(tmp_path):
    """The stranding edge: a spec's ONLY non-terminal item is a stale duplicate of an
    already-SUCCEEDED phase. Reaping it would leave the spec at 0 active items — the
    reaper must self-heal by enqueuing the live (resume) phase exactly once, on the
    default author lane, so the spec keeps moving instead of stalling forever."""
    spec = "stranded-spec"
    _write_phase_log(tmp_path, spec, ["spec", "plan"])  # plan done -> implement is live
    _write_spec(tmp_path, spec, status="implementing", current_phase="implement")
    scheduler = _reaper_scheduler(tmp_path)
    store = scheduler.store
    stale = _phase_item(store, spec, "plan")  # superseded duplicate of a SUCCEEDED phase

    reaped = scheduler.reap_completed_phase_items()

    assert stale.id in reaped
    assert store.get_item(stale.id).state == WorkItemState.CANCELLED
    resumed = _nonterminal_phase_items(store, "implement")
    assert len(resumed) == 1
    assert resumed[0].state == WorkItemState.QUEUED
    assert resumed[0].lane == "claude-code-cli"  # default author lane (locked to claude)
    assert resumed[0].task_ref["spec_id"] == spec


def test_reap_resume_is_idempotent_across_passes(tmp_path):
    """Running the reaper twice must not enqueue duplicate resume items: the first pass
    creates the implement item; the second reaps nothing (the resume phase is not
    SUCCEEDED) and adds nothing."""
    spec = "stranded-spec"
    _write_phase_log(tmp_path, spec, ["spec", "plan"])
    _write_spec(tmp_path, spec, status="implementing", current_phase="implement")
    scheduler = _reaper_scheduler(tmp_path)
    store = scheduler.store
    _phase_item(store, spec, "plan")

    scheduler.reap_completed_phase_items()
    assert scheduler.reap_completed_phase_items() == []  # nothing left to reap

    assert len(_nonterminal_phase_items(store, "implement")) == 1


def test_reap_does_not_resume_verified_spec(tmp_path):
    """A verified (terminal) spec must not be resumed: reaping a stale duplicate of its
    terminal phase cancels it and enqueues nothing — every item ends terminal."""
    spec = "done-spec"
    _write_phase_log(tmp_path, spec, ["spec", "plan", "implement", "verify"])
    _write_spec(tmp_path, spec, status="verified", current_phase="verify")
    scheduler = _reaper_scheduler(tmp_path)
    store = scheduler.store
    stale = _phase_item(store, spec, "verify")  # duplicate of the terminal phase

    reaped = scheduler.reap_completed_phase_items()

    assert stale.id in reaped
    assert store.get_item(stale.id).state == WorkItemState.CANCELLED
    assert all(it.state in TERMINAL_STATES for it in store.reconstruct().items.values())


def test_reap_does_not_resume_when_another_phase_still_active(tmp_path):
    """When a live item for a different phase survives the reap, the spec is NOT
    stranded — the reaper must not enqueue a duplicate of the live phase."""
    spec = "busy-spec"
    _write_phase_log(tmp_path, spec, ["spec", "plan"])
    _write_spec(tmp_path, spec, status="implementing", current_phase="implement")
    scheduler = _reaper_scheduler(tmp_path)
    store = scheduler.store
    stale = _phase_item(store, spec, "plan")        # done -> reaped
    live = _phase_item(store, spec, "implement")    # live -> survives

    reaped = scheduler.reap_completed_phase_items()

    assert stale.id in reaped and live.id not in reaped
    implement_items = _nonterminal_phase_items(store, "implement")
    assert len(implement_items) == 1 and implement_items[0].id == live.id


def test_reap_does_not_resume_when_plan_gate_pending(tmp_path):
    """A pending plan-approval gate IS the spec's active state. The reaper must not
    enqueue a post-gate phase: lane_common folds an un-approved post-gate dispatch back
    to plan, so a resume here would churn and drop the pending human approval."""
    spec = "gated-spec"
    _write_phase_log(tmp_path, spec, ["spec", "plan"])
    _write_spec(tmp_path, spec, status="planned", current_phase="implement")
    scheduler = _reaper_scheduler(tmp_path)
    store = scheduler.store
    gates = store.queue_dir / "gates"
    gates.mkdir(parents=True, exist_ok=True)
    (gates / f"{spec}.json").write_text(
        '{"spec_id": "gated-spec", "next_phase": "implement", "lane": "claude-code-cli", "priority": 0}',
        encoding="utf-8",
    )
    stale = _phase_item(store, spec, "plan")

    reaped = scheduler.reap_completed_phase_items()

    assert stale.id in reaped
    assert store.get_item(stale.id).state == WorkItemState.CANCELLED
    assert all(it.state in TERMINAL_STATES for it in store.reconstruct().items.values())


def test_reap_stands_down_when_live_phase_already_enqueued_concurrently(tmp_path):
    """Race guard for both confirmed TOCTOUs (the lock-free worker-thread
    _advance_after_success, and a lock-free `approve`): if the live phase ALREADY has an
    active item by the time the resume would enqueue, the resume stands down — the
    active-item check reads the LIVE store (not a stale snapshot), so a concurrently
    enqueued item is seen and no duplicate is created. Shape mirrors a just-approved gated
    spec: gate consumed (.json gone, .approved written), implement already queued, then a
    stale plan duplicate is reaped."""
    spec = "approved-spec"
    _write_phase_log(tmp_path, spec, ["spec", "plan"])
    _write_spec(tmp_path, spec, status="planned", current_phase="implement")
    scheduler = _reaper_scheduler(tmp_path)
    store = scheduler.store
    gates = store.queue_dir / "gates"
    gates.mkdir(parents=True, exist_ok=True)
    (gates / f"{spec}.approved").write_text("{}", encoding="utf-8")  # gate already consumed
    concurrent = _phase_item(store, spec, "implement")  # approve/advance already enqueued it
    stale = _phase_item(store, spec, "plan")            # superseded duplicate -> reaped

    reaped = scheduler.reap_completed_phase_items()

    assert stale.id in reaped
    implement_items = _nonterminal_phase_items(store, "implement")
    assert len(implement_items) == 1 and implement_items[0].id == concurrent.id


# ---------------------------------------------------------------------------
# Tier 1 — lane_cooled notify: debounced on cooldown_until
# ---------------------------------------------------------------------------


def test_rate_limited_fires_lane_cooled_notify_debounced_on_cooldown_until(tmp_path):
    """After one RATE_LIMITED dispatch a lane_cooled packet is emitted with the
    correct fields. A SECOND rate-limit with the SAME cooldown_until is debounced
    (no second notify). A NEW (distinct) cooldown_until fires a fresh notify.
    spec_failed is NEVER emitted (budget invariant preserved throughout)."""
    from _dispatch_runtime.lane_executor import DispatchResult, DispatchResultType

    class RecordingNotifier:
        def __init__(self):
            self.events: list[tuple[str, dict]] = []

        def notify(self, kind, packet):
            self.events.append((kind, packet))

    store = QueueStore(tmp_path)
    # Enqueue on the codex-cli lane so the lane name is meaningful.
    item = store.enqueue(
        task_ref={
            "kind": "builder-runner-task",
            "runner_task_ref": "runs/task.yaml",
            "spec_id": "demo-spec",
        },
        lane="codex-cli",
    )
    cfg = dispatch_config(tmp_path)
    cfg.retry_policy["max_attempts"] = 3

    notifier = RecordingNotifier()
    rate_limited_result = DispatchResult(
        result_type=DispatchResultType.RATE_LIMITED,
        metadata={"spec_id": "demo-spec", "phase": "implement", "message": "quota hit"},
    )

    # --- Round 1: first rate-limit -> lane_cooled emitted.
    executor = RecordingExecutor(rate_limited_result)
    scheduler = DispatchScheduler(store, cfg, executor, owner_id="sched-a", notifier=notifier, project_dir=tmp_path)

    scheduler.dispatch_once()
    assert scheduler.wait_for_attempts()

    cooled_events = [(k, p) for k, p in notifier.events if k == "lane_cooled"]
    assert len(cooled_events) == 1, "expected exactly one lane_cooled on first rate-limit"
    _, pkt = cooled_events[0]
    assert pkt["lane"] == "codex-cli"
    assert pkt["project"] == tmp_path.name
    assert pkt["queued_on_lane"] >= 1
    assert pkt["cooldown_seconds"] > 0
    assert pkt["cooldown_until"]
    assert pkt["spec_id"] == "demo-spec"
    # spec_failed must NEVER fire.
    assert not any(k == "spec_failed" for k, _ in notifier.events)

    first_cooldown_until = pkt["cooldown_until"]

    # --- Round 2: same cooldown_until -> debounced (no second notify).
    current = store.get_item(item.id)
    current.scheduled_after = None
    store.save_item(current)
    # Keep the SAME cooldown (do NOT clear it), so the marker file matches.
    # We still need the lane available for scheduling, so manually clear cooldown
    # but ensure the marker stays with the first cooldown_until so debounce fires.
    # Actually: the debounce keys on cooldown_until written in the marker.
    # The lane is still cooled by the first round; manually fast-forward scheduled_after
    # to make item dispatchable again (mimics the real clock advancing), but keep the lane
    # record's cooldown_until IDENTICAL to trigger the debounce.
    executor2 = RecordingExecutor(rate_limited_result)
    scheduler2 = DispatchScheduler(store, cfg, executor2, owner_id="sched-b", notifier=notifier, project_dir=tmp_path)
    # Clear the lane so the item can dispatch, but then open a new cooldown with
    # THE SAME until value to test dedupe.
    store.set_lane_cooldown("codex-cli", until=iso_at(-1), reason="rate_limited")  # expire it
    current2 = store.get_item(item.id)
    current2.scheduled_after = None
    store.save_item(current2)

    # Use a mocked rate_limited_result that produces the SAME cooldown_until.
    # The scheduler computes cooldown_until from config.cooldown_policy.default_seconds.
    # To force the same cooldown_until, set the rate_limited result's retry_after to the
    # exact first_cooldown_until so open_lane_cooldown writes the same value.
    same_result = DispatchResult(
        result_type=DispatchResultType.RATE_LIMITED,
        metadata={"spec_id": "demo-spec", "phase": "implement"},
        retry_after=first_cooldown_until,  # exact same until -> debounced
    )
    executor2.result = same_result
    before_count = len(notifier.events)
    scheduler2.dispatch_once()
    assert scheduler2.wait_for_attempts()
    # Debounced: no new lane_cooled for the same cooldown_until.
    new_cooled = [(k, p) for k, p in notifier.events[before_count:] if k == "lane_cooled"]
    assert new_cooled == [], f"debounce failed: got {new_cooled}"

    # --- Round 3: a NEW (distinct) cooldown_until -> re-alerts.
    store.set_lane_cooldown("codex-cli", until=iso_at(-1), reason="rate_limited")
    current3 = store.get_item(item.id)
    current3.scheduled_after = None
    store.save_item(current3)
    new_until = iso_at(999)  # clearly different from first_cooldown_until
    fresh_result = DispatchResult(
        result_type=DispatchResultType.RATE_LIMITED,
        metadata={"spec_id": "demo-spec", "phase": "implement"},
        retry_after=new_until,
    )
    executor3 = RecordingExecutor(fresh_result)
    scheduler3 = DispatchScheduler(store, cfg, executor3, owner_id="sched-c", notifier=notifier, project_dir=tmp_path)
    before_count2 = len(notifier.events)
    scheduler3.dispatch_once()
    assert scheduler3.wait_for_attempts()
    new_cooled2 = [(k, p) for k, p in notifier.events[before_count2:] if k == "lane_cooled"]
    assert len(new_cooled2) == 1, "expected a fresh lane_cooled for a distinct cooldown_until"
    assert not any(k == "spec_failed" for k, _ in notifier.events)


def test_lane_cooled_notify_is_best_effort_never_raises(tmp_path):
    """A failing notifier must not break the dispatch loop: the item re-QUEUEs and
    the lane cooldown is set even when notify() raises."""
    from _dispatch_runtime.lane_executor import DispatchResult, DispatchResultType

    class ExplodingNotifier:
        def notify(self, kind, packet):
            raise RuntimeError("notify intentionally exploding")

    store = QueueStore(tmp_path)
    item = store.enqueue(
        task_ref={"kind": "builder-runner-task", "runner_task_ref": "runs/task.yaml"},
        lane="codex-cli",
    )
    cfg = dispatch_config(tmp_path)
    executor = RecordingExecutor(DispatchResult(
        result_type=DispatchResultType.RATE_LIMITED,
        metadata={"spec_id": "demo-spec", "phase": "implement"},
    ))
    scheduler = DispatchScheduler(store, cfg, executor, owner_id="sched-x", notifier=ExplodingNotifier())

    # Must not raise; dispatch_once completes normally.
    scheduler.dispatch_once()
    assert scheduler.wait_for_attempts()

    # Item re-queued and lane on cooldown — the notify failure didn't break the loop.
    requeued = store.get_item(item.id)
    assert requeued is not None
    assert requeued.state == WorkItemState.QUEUED
    assert requeued.scheduled_after is not None
    lane_rec = store.reconstruct().lanes.get("codex-cli")
    assert lane_rec is not None and lane_rec.cooldown_until is not None


# ---------------------------------------------------------------------------
# R4 — dependency-aware scheduling (pipeline.dependency_gating, default OFF)
# ---------------------------------------------------------------------------


def test_dependency_gating_off_dispatches_normally_ignoring_unverified_dependency(tmp_path):
    """Flag OFF must have ZERO effect: an item whose spec declares an unverified
    `required` dependency dispatches exactly as it did before this feature existed
    (byte-identical default behavior)."""
    store = QueueStore(tmp_path)
    _write_spec(tmp_path, "spec-a", status="planned", current_phase="implement")
    _write_dependencies(tmp_path, "spec-b", [{"spec": "spec-a", "kind": "required", "reason": "shared prereq"}])
    item = _phase_item(store, "spec-b", "implement")

    cfg = dispatch_config(tmp_path)  # dependency_gating unset -> falsy
    executor = RecordingExecutor(success_result())
    scheduler = DispatchScheduler(store, cfg, executor, owner_id="scheduler-a", project_dir=tmp_path)

    scheduler.dispatch_once()
    assert scheduler.wait_for_attempts()

    assert store.get_item(item.id).state == WorkItemState.SUCCEEDED
    assert executor.calls


def test_dependency_gating_holds_dependent_then_dispatches_once_prereq_verifies(tmp_path):
    """R4: with dependency_gating on, an item whose spec declares a `required`
    dependency on another spec must be held (BLOCKED_DEP) — never dispatched —
    while the dependency's spec.yaml is not yet verified/archived, then
    auto-recover to QUEUED and dispatch once the dependency verifies. Covers the
    'wrong-order roadmap' case: B is enqueued BEFORE A."""
    store = QueueStore(tmp_path)
    _write_spec(tmp_path, "spec-a", status="planned", current_phase="implement")
    _write_dependencies(tmp_path, "spec-b", [{"spec": "spec-a", "kind": "required", "reason": "shared prereq"}])
    item = _phase_item(store, "spec-b", "implement")  # B enqueued BEFORE A verifies

    cfg = dispatch_config(tmp_path)
    cfg.pipeline["dependency_gating"] = True
    executor = RecordingExecutor(success_result())
    scheduler = DispatchScheduler(store, cfg, executor, owner_id="scheduler-a", project_dir=tmp_path)

    scheduler.dispatch_once()
    assert scheduler.wait_for_attempts()

    held = store.get_item(item.id)
    assert held is not None and held.state == WorkItemState.BLOCKED_DEP
    assert executor.calls == []  # never dispatched while A is unverified

    # A verifies -> B auto-recovers (BLOCKED_DEP -> QUEUED) and dispatches.
    _write_spec(tmp_path, "spec-a", status="verified", current_phase="verify")
    scheduler.dispatch_once()
    assert scheduler.wait_for_attempts()

    recovered = store.get_item(item.id)
    assert recovered is not None and recovered.state == WorkItemState.SUCCEEDED
    assert executor.calls and executor.calls[0][0].endswith("phase-implement.yaml")


def test_dependency_gating_ignores_contextual_and_optional_kinds(tmp_path):
    """Only `kind: required` deps gate dispatch — `contextual`/`optional` are
    informational and must never hold an item."""
    store = QueueStore(tmp_path)
    _write_spec(tmp_path, "spec-a", status="planned", current_phase="implement")
    _write_dependencies(tmp_path, "spec-b", [{"spec": "spec-a", "kind": "contextual", "reason": "fyi"}])
    item = _phase_item(store, "spec-b", "implement")

    cfg = dispatch_config(tmp_path)
    cfg.pipeline["dependency_gating"] = True
    executor = RecordingExecutor(success_result())
    scheduler = DispatchScheduler(store, cfg, executor, owner_id="scheduler-a", project_dir=tmp_path)

    scheduler.dispatch_once()
    assert scheduler.wait_for_attempts()

    assert store.get_item(item.id).state == WorkItemState.SUCCEEDED


def test_dependency_gating_cascades_to_blocked_human_when_dependency_stalled(tmp_path):
    """R4: if a required dependency's own pipeline has permanently stalled (a
    FAILED work item and nothing else active), the dependent must not wait forever
    — it cascades BLOCKED_DEP -> BLOCKED_HUMAN with a notify."""
    store = QueueStore(tmp_path)
    _write_spec(tmp_path, "spec-a", status="planned", current_phase="implement")
    _write_dependencies(tmp_path, "spec-b", [{"spec": "spec-a", "kind": "required", "reason": "shared prereq"}])
    item = _phase_item(store, "spec-b", "implement")
    a_item = _phase_item(store, "spec-a", "implement")
    store.transition_item(a_item.id, WorkItemState.FAILED)  # spec-a's pipeline hit a dead stop

    class RecordingNotifier:
        def __init__(self):
            self.events: list[tuple[str, dict]] = []

        def notify(self, kind, packet):
            self.events.append((kind, packet))

    cfg = dispatch_config(tmp_path)
    cfg.pipeline["dependency_gating"] = True
    notifier = RecordingNotifier()
    executor = RecordingExecutor(success_result())
    scheduler = DispatchScheduler(
        store, cfg, executor, owner_id="scheduler-a", project_dir=tmp_path, notifier=notifier,
    )

    scheduler.dispatch_once()  # first pass: B -> BLOCKED_DEP
    assert scheduler.wait_for_attempts()
    assert store.get_item(item.id).state == WorkItemState.BLOCKED_DEP

    scheduler.dispatch_once()  # second pass: re-check -> cascade
    assert scheduler.wait_for_attempts()
    assert store.get_item(item.id).state == WorkItemState.BLOCKED_HUMAN
    assert any(k == "blocked_human" for k, _ in notifier.events)


# ---------------------------------------------------------------------------
# R4 dual-model review fixes: H2 (stalled misfire), H3 (archived dep), M5 (self-
# dep), L2 (path traversal), M4a (flag-off drain), M3 (BLOCKED_DEP -> CANCELLED)
# ---------------------------------------------------------------------------


def test_dep_stalled_uses_latest_item_only_ignoring_historical_failure(tmp_path):
    """H2: a dependency with an OLD FAILED item that later healed (its
    MOST-RECENT item is active) must not be treated as stalled — `_dep_stalled`
    consults only the latest item per dep spec, never the full history
    `store.reconstruct()` keeps forever. The dependent must stay BLOCKED_DEP
    across a recheck pass, never escalate to BLOCKED_HUMAN."""
    store = QueueStore(tmp_path)
    _write_spec(tmp_path, "spec-a", status="planned", current_phase="implement")
    _write_dependencies(tmp_path, "spec-b", [{"spec": "spec-a", "kind": "required", "reason": "shared prereq"}])
    item = _phase_item(store, "spec-b", "implement")

    old_failed = _phase_item(store, "spec-a", "plan")
    old_failed.created_at = iso_at(-300)
    store.save_item(old_failed)
    store.transition_item(old_failed.id, WorkItemState.FAILED)

    latest_active = _phase_item(store, "spec-a", "implement")
    latest_active.created_at = iso_at(-60)  # later than old_failed, still active (QUEUED)
    store.save_item(latest_active)

    cfg = dispatch_config(tmp_path)
    cfg.pipeline["dependency_gating"] = True
    scheduler = DispatchScheduler(
        store, cfg, RecordingExecutor(success_result()), owner_id="scheduler-a", project_dir=tmp_path,
    )

    scheduler.dispatch_once()  # first pass: B -> BLOCKED_DEP (spec-a not yet verified)
    assert scheduler.wait_for_attempts()
    assert store.get_item(item.id).state == WorkItemState.BLOCKED_DEP

    scheduler.dispatch_once()  # recheck: latest item for spec-a is active -> must NOT cascade
    assert scheduler.wait_for_attempts()
    assert store.get_item(item.id).state == WorkItemState.BLOCKED_DEP


def test_dep_stalled_treats_pending_plan_gate_as_active_not_stalled(tmp_path):
    """H2: a dependency spec with a pending plan-approval gate is ALIVE, not
    stalled — even though its only work item so far is FAILED (e.g. an earlier
    spec-review attempt failed before the plan gate armed). The dependent must
    stay BLOCKED_DEP, never cascade to BLOCKED_HUMAN, while human approval is
    pending."""
    store = QueueStore(tmp_path)
    _write_spec(tmp_path, "spec-a", status="planned", current_phase="plan")
    _write_dependencies(tmp_path, "spec-b", [{"spec": "spec-a", "kind": "required", "reason": "shared prereq"}])
    item = _phase_item(store, "spec-b", "implement")

    a_item = _phase_item(store, "spec-a", "spec-review")
    store.transition_item(a_item.id, WorkItemState.FAILED)

    gate_path = store.queue_dir / "gates" / "spec-a.json"
    gate_path.parent.mkdir(parents=True, exist_ok=True)
    gate_path.write_text('{"spec_id": "spec-a", "next_phase": "implement"}', encoding="utf-8")

    cfg = dispatch_config(tmp_path)
    cfg.pipeline["dependency_gating"] = True
    scheduler = DispatchScheduler(
        store, cfg, RecordingExecutor(success_result()), owner_id="scheduler-a", project_dir=tmp_path,
    )

    scheduler.dispatch_once()
    assert scheduler.wait_for_attempts()
    assert store.get_item(item.id).state == WorkItemState.BLOCKED_DEP

    scheduler.dispatch_once()  # recheck: gate pending -> alive, must NOT cascade
    assert scheduler.wait_for_attempts()
    assert store.get_item(item.id).state == WorkItemState.BLOCKED_DEP


def test_dep_stalled_cascades_when_latest_item_is_genuinely_failed(tmp_path):
    """H2 (negative case): when the dependency's MOST-RECENT item is FAILED and
    no plan gate is pending, the dependency is genuinely dead — the dependent
    must still cascade BLOCKED_DEP -> BLOCKED_HUMAN, even though an OLDER item
    for the same spec had SUCCEEDED (proves the fix reads 'latest', not just
    'any')."""
    store = QueueStore(tmp_path)
    _write_spec(tmp_path, "spec-a", status="planned", current_phase="implement")
    _write_dependencies(tmp_path, "spec-b", [{"spec": "spec-a", "kind": "required", "reason": "shared prereq"}])
    item = _phase_item(store, "spec-b", "implement")

    old_succeeded = _phase_item(store, "spec-a", "plan")
    old_succeeded.created_at = iso_at(-300)
    store.save_item(old_succeeded)
    store.transition_item(old_succeeded.id, WorkItemState.DISPATCHED)
    store.transition_item(old_succeeded.id, WorkItemState.RUNNING)
    store.transition_item(old_succeeded.id, WorkItemState.SUCCEEDED)

    latest_failed = _phase_item(store, "spec-a", "implement")
    latest_failed.created_at = iso_at(-60)
    store.save_item(latest_failed)
    store.transition_item(latest_failed.id, WorkItemState.FAILED)

    cfg = dispatch_config(tmp_path)
    cfg.pipeline["dependency_gating"] = True
    scheduler = DispatchScheduler(
        store, cfg, RecordingExecutor(success_result()), owner_id="scheduler-a", project_dir=tmp_path,
    )

    scheduler.dispatch_once()  # first pass: B -> BLOCKED_DEP
    assert scheduler.wait_for_attempts()
    assert store.get_item(item.id).state == WorkItemState.BLOCKED_DEP

    scheduler.dispatch_once()  # recheck: latest item is FAILED, no gate -> cascade
    assert scheduler.wait_for_attempts()
    assert store.get_item(item.id).state == WorkItemState.BLOCKED_HUMAN


def test_dependency_gating_archived_dependency_satisfies_gate(tmp_path):
    """H3: archiving MOVES a spec's dir to
    .builder/specs/archive/<YYYY-MM-DD>-<spec>/ — a dependency must resolve
    via the archive-aware `_resolve_spec_dir` (same resolver phase-completion
    validation uses) and read `status: archived` there (a satisfied status),
    letting the dependent dispatch instead of reading "" (unmet) forever from
    the now-empty canonical path."""
    from _yaml import yaml

    store = QueueStore(tmp_path)
    archive_dir = tmp_path / ".builder" / "specs" / "archive" / "2026-06-01-spec-a"
    archive_dir.mkdir(parents=True, exist_ok=True)
    (archive_dir / "spec.yaml").write_text(
        yaml.safe_dump({"status": "archived", "current_phase": "verify"}), encoding="utf-8",
    )
    _write_dependencies(tmp_path, "spec-b", [{"spec": "spec-a", "kind": "required", "reason": "shared prereq"}])
    item = _phase_item(store, "spec-b", "implement")

    cfg = dispatch_config(tmp_path)
    cfg.pipeline["dependency_gating"] = True
    executor = RecordingExecutor(success_result())
    scheduler = DispatchScheduler(store, cfg, executor, owner_id="scheduler-a", project_dir=tmp_path)

    scheduler.dispatch_once()
    assert scheduler.wait_for_attempts()

    assert store.get_item(item.id).state == WorkItemState.SUCCEEDED
    assert executor.calls


def test_dependency_gating_self_dependency_does_not_deadlock(tmp_path):
    """M5: a runtime self-dependency (a spec listing itself as a required
    dependency) can never be satisfied and must not sit BLOCKED_DEP forever.
    The validator normally rejects this at authoring time; this is the runtime
    guard for one that slips through."""
    store = QueueStore(tmp_path)
    _write_dependencies(tmp_path, "spec-a", [{"spec": "spec-a", "kind": "required", "reason": "oops"}])
    item = _phase_item(store, "spec-a", "implement")

    cfg = dispatch_config(tmp_path)
    cfg.pipeline["dependency_gating"] = True
    executor = RecordingExecutor(success_result())
    scheduler = DispatchScheduler(store, cfg, executor, owner_id="scheduler-a", project_dir=tmp_path)

    scheduler.dispatch_once()
    assert scheduler.wait_for_attempts()

    assert store.get_item(item.id).state == WorkItemState.SUCCEEDED
    assert executor.calls


def test_dependency_gating_skips_path_traversal_target_without_holding_forever(tmp_path):
    """L2: a dependency target containing a path separator (or being '.'/'..')
    must be skipped rather than used to build a filesystem path — otherwise it
    would read as permanently unmet (status "") and hold the item forever."""
    store = QueueStore(tmp_path)
    _write_dependencies(tmp_path, "spec-b", [
        {"spec": "../../etc/passwd", "kind": "required", "reason": "malicious"},
        {"spec": "..", "kind": "required", "reason": "malicious"},
    ])
    item = _phase_item(store, "spec-b", "implement")

    cfg = dispatch_config(tmp_path)
    cfg.pipeline["dependency_gating"] = True
    executor = RecordingExecutor(success_result())
    scheduler = DispatchScheduler(store, cfg, executor, owner_id="scheduler-a", project_dir=tmp_path)

    scheduler.dispatch_once()
    assert scheduler.wait_for_attempts()

    assert store.get_item(item.id).state == WorkItemState.SUCCEEDED
    assert executor.calls


def test_dependency_gating_off_still_drains_existing_blocked_dep_items(tmp_path):
    """M4a: turning dependency_gating OFF must never strand an item already held
    BLOCKED_DEP from an earlier gated run — the BLOCKED_DEP -> (QUEUED |
    BLOCKED_HUMAN) recheck runs REGARDLESS of the flag, so it always
    drains/recovers once its dependency verifies. Only the QUEUED -> BLOCKED_DEP
    hold stays gated (flag off never newly holds another item, already covered
    by test_dependency_gating_off_dispatches_normally_ignoring_unverified_dependency)."""
    store = QueueStore(tmp_path)
    _write_spec(tmp_path, "spec-a", status="planned", current_phase="implement")
    _write_dependencies(tmp_path, "spec-b", [{"spec": "spec-a", "kind": "required", "reason": "shared prereq"}])
    item = _phase_item(store, "spec-b", "implement")
    store.transition_item(item.id, WorkItemState.BLOCKED_DEP)  # pre-existing hold from an earlier gated run

    _write_spec(tmp_path, "spec-a", status="verified", current_phase="verify")  # now satisfied

    cfg = dispatch_config(tmp_path)  # dependency_gating unset/false for THIS run
    executor = RecordingExecutor(success_result())
    scheduler = DispatchScheduler(store, cfg, executor, owner_id="scheduler-a", project_dir=tmp_path)

    scheduler.dispatch_once()
    assert scheduler.wait_for_attempts()

    recovered = store.get_item(item.id)
    assert recovered is not None and recovered.state == WorkItemState.SUCCEEDED  # recovered + dispatched
    assert executor.calls


def test_blocked_dep_item_can_be_cancelled(tmp_path):
    """M3: BLOCKED_DEP -> CANCELLED must be a legal transition so the `cancel`
    CLI can cancel a dependency-held item instead of raising
    InvalidTransitionError as an operator-facing traceback."""
    store = QueueStore(tmp_path)
    item = _phase_item(store, "spec-b", "implement")
    store.transition_item(item.id, WorkItemState.BLOCKED_DEP)

    cancelled = store.transition_item(item.id, WorkItemState.CANCELLED)

    assert cancelled.state == WorkItemState.CANCELLED


# ---------------------------------------------------------------------------
# R5 — per-spec worktree isolation (pipeline.worktree_isolation, default OFF)
# ---------------------------------------------------------------------------


class _FakeGitRunner:
    """Fakes just enough of `git worktree add/remove` + `show-ref` + `status`
    to drive scheduler._ensure_worktree/_cleanup_worktree without touching a
    real repo: `add` materializes a `.git` marker at the target path (mimicking
    git's own side effect, which `_ensure_worktree` checks for); `remove`
    deletes it; `status --porcelain` defaults to a clean tree (rc=0, no
    output) so `_cleanup_worktree`'s H-3 check proceeds to remove unless a
    subclass overrides it."""

    def __init__(self):
        self.calls: list[list[str]] = []

    def run(self, argv, cwd):
        self.calls.append(list(argv))
        if argv[:3] == ["git", "show-ref", "--verify"]:
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="")
        if argv[:3] == ["git", "worktree", "add"]:
            rest = argv[3:]
            if rest and rest[0] in ("-b", "-B"):
                # `-b/-B <branch> <path> [<base>]`
                path = Path(rest[2])
            else:
                # `<path> <branch>` — the pre-M-A shape (reuse of an existing
                # branch at its stale tip); kept for backward compatibility.
                path = Path(rest[0])
            path.mkdir(parents=True, exist_ok=True)
            (path / ".git").write_text("gitdir: /fake\n", encoding="utf-8")
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        if argv[:3] == ["git", "worktree", "remove"]:
            path = Path(argv[-1])
            shutil.rmtree(path, ignore_errors=True)
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")


class _DirtyMainGitRunner(_FakeGitRunner):
    """Same as `_FakeGitRunner`, but `git status --porcelain` in MAIN reports an
    uncommitted file — the precondition under which a fresh worktree could verify a
    tree that never held the implementation."""

    def __init__(self, main: Path):
        super().__init__()
        self._main = str(main)

    def run(self, argv, cwd):
        if argv[:3] == ["git", "status", "--porcelain"] and cwd == self._main:
            self.calls.append(list(argv))
            return subprocess.CompletedProcess(argv, 0, stdout=" M src/app.py\n", stderr="")
        return super().run(argv, cwd)


def _worktree_scheduler(tmp_path, runner):
    store = QueueStore(tmp_path)
    store.enqueue(task_ref={
        "kind": "builder-phase-batch",
        "runner_task_ref": ".builder/specs/demo/runs/phase-implement.yaml",
        "spec_id": "demo",
    })
    cfg = dispatch_config(tmp_path)
    cfg.pipeline["worktree_isolation"] = True
    return store, DispatchScheduler(
        store, cfg, RecordingExecutor(success_result()), owner_id="scheduler-a",
        project_dir=tmp_path, worktree_runner=runner,
    )


def test_fresh_worktree_is_refused_while_main_has_uncommitted_work(tmp_path):
    """No phase commits before delivery, so a phase that ran in MAIN leaves its work
    uncommitted there. If that spec degraded, terminated, and had its sticky marker
    cleared, a re-dispatch into a fresh EMPTY worktree would verify a tree without
    the implementation — a host verdict about the wrong code.

    So provisioning fails closed on the precondition: main dirty -> run in main.
    """
    runner = _DirtyMainGitRunner(tmp_path)
    _store, scheduler = _worktree_scheduler(tmp_path, runner)
    scheduler._mark_fallback("demo"); scheduler._clear_fallback("demo")  # degraded once, then resolved

    resolved = scheduler._ensure_worktree("demo")

    assert resolved == tmp_path, "a fresh worktree must not be provisioned while main is dirty"
    assert not any(c[:3] == ["git", "worktree", "add"] for c in runner.calls), \
        "no worktree may be created under the fail-closed precondition"
    assert scheduler._fallback_marker("demo").exists(), \
        "the degrade must be sticky, so later phases and delivery stay in main too"


def test_a_clean_main_still_gets_its_worktree(tmp_path):
    """The guard must cost nothing in the normal case, or isolation is dead in practice."""
    runner = _FakeGitRunner()  # status --porcelain -> clean
    _store, scheduler = _worktree_scheduler(tmp_path, runner)

    resolved = scheduler._ensure_worktree("demo")

    assert resolved != tmp_path, "a clean main must still get an isolated worktree"
    assert any(c[:3] == ["git", "worktree", "add"] for c in runner.calls)


def test_an_unreadable_git_status_is_treated_as_dirty(tmp_path):
    """Fail closed in both directions: if we cannot tell whether main is clean, we must
    not assume it is. Costing a spec its isolation is recoverable; a verdict about the
    wrong tree is not."""
    class _Exploding(_FakeGitRunner):
        def run(self, argv, cwd):
            if argv[:3] == ["git", "status", "--porcelain"]:
                raise OSError("git unavailable")
            return super().run(argv, cwd)

    runner = _Exploding()
    _store, scheduler = _worktree_scheduler(tmp_path, runner)
    scheduler._mark_fallback("demo"); scheduler._clear_fallback("demo")

    assert scheduler._ensure_worktree("demo") == tmp_path
    assert scheduler._fallback_marker("demo").exists()


def test_worktree_isolation_off_uses_shared_project_dir(tmp_path):
    """Flag OFF: workspace_root must stay str(project_dir) exactly as today — no
    worktree runner call at all."""
    store = QueueStore(tmp_path)
    store.enqueue(task_ref={
        "kind": "builder-phase-batch",
        "runner_task_ref": ".builder/specs/demo/runs/phase-implement.yaml",
        "spec_id": "demo",
    })
    executor = RecordingExecutor(success_result())
    fake_git = _FakeGitRunner()
    scheduler = DispatchScheduler(
        store, dispatch_config(tmp_path), executor, owner_id="scheduler-a",
        project_dir=tmp_path, worktree_runner=fake_git,
    )

    scheduler.dispatch_once()
    assert scheduler.wait_for_attempts()

    assert executor.calls[0][2]["workspace_root"] == str(tmp_path)
    assert fake_git.calls == []  # isolation off -> the worktree runner is never touched


def test_sync_era_delta_forces_isolation_even_when_legacy_flag_is_off(tmp_path):
    spec_dir = tmp_path / ".builder" / "specs" / "demo"
    spec_dir.mkdir(parents=True)
    (spec_dir / "ssot-delta.yaml").write_text(
        "capabilities: []\nbehaviors: []\njourneys: []\n", encoding="utf-8")
    store = QueueStore(tmp_path)
    store.enqueue(task_ref={
        "kind": "builder-phase-batch",
        "runner_task_ref": ".builder/specs/demo/runs/phase-implement.yaml",
        "spec_id": "demo",
    })
    executor = RecordingExecutor(success_result())
    fake_git = _FakeGitRunner()
    scheduler = DispatchScheduler(
        store, dispatch_config(tmp_path), executor, owner_id="scheduler-a",
        project_dir=tmp_path, worktree_runner=fake_git,
    )

    scheduler.dispatch_once()
    assert scheduler.wait_for_attempts()
    expected = tmp_path / ".builder" / "worktrees" / "demo"
    assert executor.calls[0][2]["workspace_root"] == str(expected)
    assert any(call[:3] == ["git", "worktree", "add"] for call in fake_git.calls)


def test_worktree_isolation_on_routes_to_per_spec_worktree_and_reuses_it(tmp_path):
    """Flag ON: workspace_root must point at a per-spec git worktree, created via
    an explicit `git worktree add` (never prune) on the spec's delivery branch,
    and REUSED (no second `add`) on a later phase of the SAME spec."""
    store = QueueStore(tmp_path)
    store.enqueue(task_ref={
        "kind": "builder-phase-batch",
        "runner_task_ref": ".builder/specs/demo/runs/phase-plan.yaml",
        "spec_id": "demo",
    })
    cfg = dispatch_config(tmp_path)
    cfg.pipeline["worktree_isolation"] = True
    executor = RecordingExecutor(success_result())
    fake_git = _FakeGitRunner()
    scheduler = DispatchScheduler(
        store, cfg, executor, owner_id="scheduler-a", project_dir=tmp_path, worktree_runner=fake_git,
    )

    scheduler.dispatch_once()
    assert scheduler.wait_for_attempts()

    expected = tmp_path / ".builder" / "worktrees" / "demo"
    assert executor.calls[0][2]["workspace_root"] == str(expected)
    add_calls = [c for c in fake_git.calls if c[:3] == ["git", "worktree", "add"]]
    assert len(add_calls) == 1
    assert "builder/demo" in add_calls[0]  # branch name derived from spec_id
    assert not any("prune" in arg for call in fake_git.calls for arg in call)

    # A second phase of the SAME spec reuses the existing worktree (no 2nd add).
    store.enqueue(task_ref={
        "kind": "builder-phase-batch",
        "runner_task_ref": ".builder/specs/demo/runs/phase-implement.yaml",
        "spec_id": "demo",
    })
    scheduler.dispatch_once()
    assert scheduler.wait_for_attempts()

    assert executor.calls[1][2]["workspace_root"] == str(expected)
    add_calls_after = [c for c in fake_git.calls if c[:3] == ["git", "worktree", "add"]]
    assert len(add_calls_after) == 1  # unchanged -> reused, not recreated


def test_worktree_persists_when_delivery_disabled_or_not_reached(tmp_path):
    """Conservative cleanup policy: with delivery disabled, a completed spec's
    worktree is intentionally left in place — it may be the only copy of the
    implemented (never-committed) code, so auto-deleting it risks real data loss.
    A leftover worktree is a disk-hygiene concern only, never a correctness one."""
    from _dispatch_runtime.lane_executor import DispatchResult, DispatchResultType

    store = QueueStore(tmp_path)
    store.enqueue(task_ref={
        "kind": "builder-phase-batch",
        "runner_task_ref": ".builder/specs/demo/runs/phase-verify.yaml",
        "spec_id": "demo",
    })
    cfg = dispatch_config(tmp_path)
    cfg.pipeline["worktree_isolation"] = True  # deliver stays unset -> disabled
    fake_git = _FakeGitRunner()
    executor = RecordingExecutor(DispatchResult(
        result_type=DispatchResultType.SUCCESS,
        metadata={"phase": "verify", "spec_id": "demo"},
    ))
    scheduler = DispatchScheduler(
        store, cfg, executor, owner_id="scheduler-a", project_dir=tmp_path, worktree_runner=fake_git,
    )

    scheduler.dispatch_once()
    assert scheduler.wait_for_attempts()

    expected = tmp_path / ".builder" / "worktrees" / "demo"
    assert expected.exists()  # left in place — delivery is disabled by default
    assert not any(c[:3] == ["git", "worktree", "remove"] for c in fake_git.calls)


def test_worktree_cleaned_up_after_successful_delivery(tmp_path):
    """The one auto-cleanup point: once sync succeeds AND delivery pushes the
    branch (content now safely upstream), the per-spec worktree is removed via an
    explicit `git worktree remove` — never `git worktree prune`."""
    from _dispatch_runtime.lane_executor import DispatchResult, DispatchResultType

    store = QueueStore(tmp_path)
    store.enqueue(task_ref={
        "kind": "builder-phase-batch",
        "runner_task_ref": ".builder/specs/demo/runs/phase-sync.yaml",
        "spec_id": "demo",
    })
    cfg = dispatch_config(tmp_path)
    cfg.pipeline["worktree_isolation"] = True
    cfg.pipeline["deliver"] = {"enabled": True, "auto_merge": False, "base": "main"}
    fake_git = _FakeGitRunner()
    executor = RecordingExecutor(DispatchResult(
        result_type=DispatchResultType.SUCCESS,
        metadata={"phase": "sync", "spec_id": "demo"},
    ))
    scheduler = DispatchScheduler(
        store, cfg, executor, owner_id="scheduler-a", project_dir=tmp_path, worktree_runner=fake_git,
    )

    scheduler.dispatch_once()
    assert scheduler.wait_for_attempts()

    expected = tmp_path / ".builder" / "worktrees" / "demo"
    remove_calls = [c for c in fake_git.calls if c[:3] == ["git", "worktree", "remove"]]
    assert remove_calls, f"expected a worktree remove call, got {fake_git.calls}"
    assert str(expected) in remove_calls[0]
    assert not any("prune" in arg for call in fake_git.calls for arg in call)


# ---------------------------------------------------------------------------
# R5 Model A redesign — control-plane stays canonical in MAIN, only source is
# isolated. Core mechanism: `_ensure_worktree` redirects the worktree's
# `.builder/specs/<id>` to a symlink at the shared MAIN copy.
# ---------------------------------------------------------------------------


def test_ensure_worktree_redirects_spec_control_dir_to_main_symlink(tmp_path):
    """Model A core mechanism: after `_ensure_worktree` provisions the worktree,
    `<worktree>/.builder/specs/<id>` must be a symlink to the shared MAIN copy
    — so a write through the WORKTREE path lands in MAIN, and the scheduler
    (which reads/writes main directly) and the lane/agent (cwd-relative writes
    inside the worktree) never disagree about spec state. Reuse on a later phase
    of the same spec is idempotent (no double-symlink error, no 2nd `add`)."""
    store = QueueStore(tmp_path)
    cfg = dispatch_config(tmp_path)
    cfg.pipeline["worktree_isolation"] = True
    executor = RecordingExecutor(success_result())
    fake_git = _FakeGitRunner()
    scheduler = DispatchScheduler(
        store, cfg, executor, owner_id="scheduler-a", project_dir=tmp_path, worktree_runner=fake_git,
    )
    main_spec_dir = tmp_path / ".builder" / "specs" / "demo"
    main_spec_dir.mkdir(parents=True, exist_ok=True)

    worktree = scheduler._ensure_worktree("demo")

    wt_spec_dir = worktree / ".builder" / "specs" / "demo"
    assert wt_spec_dir.is_symlink()
    assert wt_spec_dir.resolve() == main_spec_dir.resolve()

    # A write through the WORKTREE path must land in MAIN.
    (wt_spec_dir / "phase-log.yaml").write_text("phases: []\n", encoding="utf-8")
    assert (main_spec_dir / "phase-log.yaml").read_text(encoding="utf-8") == "phases: []\n"

    # Reuse (a second phase's call) is idempotent.
    worktree_again = scheduler._ensure_worktree("demo")
    assert worktree_again == worktree
    assert wt_spec_dir.is_symlink()
    add_calls = [c for c in fake_git.calls if c[:3] == ["git", "worktree", "add"]]
    assert len(add_calls) == 1  # unchanged -> no re-add just to re-check the symlink


def test_ensure_worktree_uncommitted_spec_resolves_via_symlink_no_doa(tmp_path):
    """H1(a): a freshly-drafted spec whose spec.yaml was never committed on the
    delivery branch must not DOA in the worktree — the redirect symlink resolves
    THROUGH to main, where the live (uncommitted) spec.yaml actually lives. The
    fake `git worktree add` here never materializes a real spec.yaml in the
    checkout (matching a real `git worktree add` of an untracked spec dir) — only
    the redirect makes it resolvable."""
    store = QueueStore(tmp_path)
    cfg = dispatch_config(tmp_path)
    cfg.pipeline["worktree_isolation"] = True
    executor = RecordingExecutor(success_result())
    fake_git = _FakeGitRunner()
    scheduler = DispatchScheduler(
        store, cfg, executor, owner_id="scheduler-a", project_dir=tmp_path, worktree_runner=fake_git,
    )
    main_spec_dir = tmp_path / ".builder" / "specs" / "demo"
    main_spec_dir.mkdir(parents=True, exist_ok=True)
    (main_spec_dir / "spec.yaml").write_text("status: drafted\ncurrent_phase: plan\n", encoding="utf-8")

    worktree = scheduler._ensure_worktree("demo")

    wt_spec_yaml = worktree / ".builder" / "specs" / "demo" / "spec.yaml"
    assert wt_spec_yaml.exists()
    assert wt_spec_yaml.read_text(encoding="utf-8") == "status: drafted\ncurrent_phase: plan\n"


def test_ensure_worktree_sticky_fallback_after_provisioning_failure(tmp_path):
    """M1: once worktree provisioning fails/degrades for a spec, EVERY later call
    (`_ensure_worktree` AND `_delivery_cwd`) must return the main dir WITHOUT
    attempting another `git worktree add` — so a phase that ran (and wrote
    uncommitted work) directly in main is never followed by a phase running in a
    fresh, empty worktree that lacks that work. `_cleanup_worktree` clears the
    marker (post-successful-delivery reset)."""
    store = QueueStore(tmp_path)
    cfg = dispatch_config(tmp_path)
    cfg.pipeline["worktree_isolation"] = True
    executor = RecordingExecutor(success_result())

    class _AlwaysFailGitRunner:
        def __init__(self):
            self.calls: list[list[str]] = []

        def run(self, argv, cwd):
            self.calls.append(list(argv))
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="fatal: boom")

    fake_git = _AlwaysFailGitRunner()
    scheduler = DispatchScheduler(
        store, cfg, executor, owner_id="scheduler-a", project_dir=tmp_path, worktree_runner=fake_git,
    )

    first = scheduler._ensure_worktree("demo")
    assert first == tmp_path

    marker = tmp_path / ".builder" / "worktrees" / ".fallback-demo"
    assert marker.exists()
    add_calls_after_first = len([c for c in fake_git.calls if c[:3] == ["git", "worktree", "add"]])
    assert add_calls_after_first == 1  # the failed attempt DID try once

    # A second call must NOT try `git worktree add` again — sticky.
    second = scheduler._ensure_worktree("demo")
    assert second == tmp_path
    add_calls_after_second = len([c for c in fake_git.calls if c[:3] == ["git", "worktree", "add"]])
    assert add_calls_after_second == add_calls_after_first  # no new attempt

    # `_delivery_cwd` must honor the same marker.
    assert scheduler._delivery_cwd("demo") == tmp_path

    # Cleanup (post-successful-delivery) clears the marker.
    scheduler._cleanup_worktree("demo")
    assert not marker.exists()


# ---------------------------------------------------------------------------
# R5 pass-2 (fable adversarial review) — M-A, H-3, L-B, M-C(b)
# ---------------------------------------------------------------------------


def test_ensure_worktree_leftover_branch_resets_to_base_via_dash_cap_b(tmp_path):
    """M-A: a leftover delivery branch (from a prior squash-merged delivery
    whose cleanup removed the worktree but not the branch) must NOT be checked
    out at its stale tip — `_ensure_worktree` forces it back to the delivery
    BASE with `git worktree add -B <branch> <path> <base>`, exactly as a fresh
    worktree would start, instead of re-carrying already-merged commits into
    the next PR."""
    store = QueueStore(tmp_path)
    cfg = dispatch_config(tmp_path)
    cfg.pipeline["worktree_isolation"] = True
    executor = RecordingExecutor(success_result())

    class _LeftoverBranchGitRunner(_FakeGitRunner):
        def run(self, argv, cwd):
            if argv[:3] == ["git", "show-ref", "--verify"]:
                self.calls.append(list(argv))
                return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")  # branch exists
            return super().run(argv, cwd)

    fake_git = _LeftoverBranchGitRunner()
    scheduler = DispatchScheduler(
        store, cfg, executor, owner_id="scheduler-a", project_dir=tmp_path, worktree_runner=fake_git,
    )

    worktree = scheduler._ensure_worktree("demo")

    assert worktree == tmp_path / ".builder" / "worktrees" / "demo"
    add_calls = [c for c in fake_git.calls if c[:3] == ["git", "worktree", "add"]]
    assert len(add_calls) == 1
    assert add_calls[0][3] == "-B"
    assert add_calls[0][4] == "builder/demo"
    assert add_calls[0][5] == str(worktree)
    assert add_calls[0][6] == "main"  # resolved base (no origin/HEAD configured -> falls back to "main")


def test_ensure_worktree_reuse_path_is_not_reset_by_m_a(tmp_path):
    """M-A must NOT touch the REUSE path (an in-progress worktree with `.git`
    already present carries THIS run's uncommitted work) — only the CREATION
    path (a leftover branch with no worktree yet) is reset to base."""
    store = QueueStore(tmp_path)
    cfg = dispatch_config(tmp_path)
    cfg.pipeline["worktree_isolation"] = True
    executor = RecordingExecutor(success_result())
    fake_git = _FakeGitRunner()  # show-ref -> branch does NOT exist -> fresh `-b` create
    scheduler = DispatchScheduler(
        store, cfg, executor, owner_id="scheduler-a", project_dir=tmp_path, worktree_runner=fake_git,
    )

    first = scheduler._ensure_worktree("demo")
    add_calls_after_first = [c for c in fake_git.calls if c[:3] == ["git", "worktree", "add"]]
    assert len(add_calls_after_first) == 1
    assert add_calls_after_first[0][3] == "-b"  # fresh branch, not a reset

    # A second phase's call reuses the existing `.git` -> no second `add` at all
    # (neither `-b` nor `-B`), confirming the reuse path is untouched by M-A.
    second = scheduler._ensure_worktree("demo")
    assert second == first
    add_calls_after_second = [c for c in fake_git.calls if c[:3] == ["git", "worktree", "add"]]
    assert len(add_calls_after_second) == 1


def test_redirect_spec_control_dir_creates_missing_main_spec_dir(tmp_path):
    """L-B: `_redirect_spec_control_dir` must create the MAIN spec dir BEFORE
    symlinking to it, so the symlink is never dangling even when nothing has
    ever written the main spec dir yet — self-heals a missing spec dir exactly
    as the agent could pre-Model-A (no symlink indirection at all)."""
    store = QueueStore(tmp_path)
    cfg = dispatch_config(tmp_path)
    cfg.pipeline["worktree_isolation"] = True
    executor = RecordingExecutor(success_result())
    fake_git = _FakeGitRunner()
    scheduler = DispatchScheduler(
        store, cfg, executor, owner_id="scheduler-a", project_dir=tmp_path, worktree_runner=fake_git,
    )
    main_spec_dir = tmp_path / ".builder" / "specs" / "demo"
    assert not main_spec_dir.exists()  # nothing has ever written it

    worktree = scheduler._ensure_worktree("demo")

    assert main_spec_dir.is_dir()  # self-healed, not left missing
    wt_spec_dir = worktree / ".builder" / "specs" / "demo"
    assert wt_spec_dir.is_symlink()
    assert wt_spec_dir.exists()  # resolves through the symlink -> False if dangling


def test_cleanup_worktree_refuses_when_non_runtime_changes_remain(tmp_path):
    """H-3: `_cleanup_worktree` must NOT force-delete a worktree that still
    carries an agent-authored file scoped delivery's traceability/handoff
    lists omitted (so it was committed nowhere) — only the `.specpilot/`
    symlink/deletion noise from Model A's control-dir redirect is tolerated."""
    store = QueueStore(tmp_path)
    cfg = dispatch_config(tmp_path)
    cfg.pipeline["worktree_isolation"] = True
    executor = RecordingExecutor(success_result())

    class _DirtyStatusGitRunner(_FakeGitRunner):
        def run(self, argv, cwd):
            if argv[:2] == ["git", "status"]:
                self.calls.append(list(argv))
                return subprocess.CompletedProcess(
                    argv, 0,
                    stdout=" D .specpilot/specs/demo/spec.yaml\n?? src/undelivered.py\n",
                    stderr="",
                )
            return super().run(argv, cwd)

    fake_git = _DirtyStatusGitRunner()
    scheduler = DispatchScheduler(
        store, cfg, executor, owner_id="scheduler-a", project_dir=tmp_path, worktree_runner=fake_git,
    )
    worktree = scheduler._ensure_worktree("demo")
    assert worktree.exists()

    scheduler._cleanup_worktree("demo")

    assert worktree.exists()  # refused -> left in place for recovery
    assert not any(c[:3] == ["git", "worktree", "remove"] for c in fake_git.calls)
    marker = tmp_path / ".builder" / "worktrees" / ".retained-demo"
    assert marker.exists()
    assert "src/undelivered.py" in marker.read_text(encoding="utf-8")


def test_cleanup_worktree_removes_when_only_specpilot_status_noise(tmp_path):
    """Counterpart: `.specpilot/`-only status noise (the EXPECTED control-dir
    redirect artifact) must NOT block cleanup — the worktree is still removed."""
    store = QueueStore(tmp_path)
    cfg = dispatch_config(tmp_path)
    cfg.pipeline["worktree_isolation"] = True
    executor = RecordingExecutor(success_result())

    class _OnlyLegacyRuntimeStatusGitRunner(_FakeGitRunner):
        def run(self, argv, cwd):
            if argv[:2] == ["git", "status"]:
                self.calls.append(list(argv))
                return subprocess.CompletedProcess(
                    argv, 0,
                    stdout=" D .specpilot/specs/demo/spec.yaml\n?? .specpilot/specs/demo\n",
                    stderr="",
                )
            return super().run(argv, cwd)

    fake_git = _OnlyLegacyRuntimeStatusGitRunner()
    scheduler = DispatchScheduler(
        store, cfg, executor, owner_id="scheduler-a", project_dir=tmp_path, worktree_runner=fake_git,
    )
    worktree = scheduler._ensure_worktree("demo")
    assert worktree.exists()

    scheduler._cleanup_worktree("demo")

    assert not worktree.exists()  # removed -> only .specpilot noise, no real residue
    remove_calls = [c for c in fake_git.calls if c[:3] == ["git", "worktree", "remove"]]
    assert remove_calls
    marker = tmp_path / ".builder" / "worktrees" / ".retained-demo"
    assert not marker.exists()


def test_terminal_failed_clears_sticky_fallback_marker(tmp_path):
    """M-C(b): a spec that goes terminal FAILED (retry exhaustion) must have
    its M1 sticky-fallback marker cleared too — otherwise a degraded spec stays
    pinned to running in main forever, even after a human resolves it and
    re-queues a fresh attempt (before this fix, `_clear_fallback` ran ONLY in
    `_cleanup_worktree`, the successful-delivery path)."""
    from _dispatch_runtime.lane_executor import DispatchResult, DispatchResultType

    store = QueueStore(tmp_path)
    store.enqueue(task_ref={
        "kind": "builder-phase-batch",
        "runner_task_ref": ".builder/specs/demo/runs/phase-implement.yaml",
        "spec_id": "demo",
    })
    cfg = dispatch_config(tmp_path)
    cfg.retry_policy["max_attempts"] = 1  # first retryable error exhausts -> FAILED
    executor = RecordingExecutor(DispatchResult(
        result_type=DispatchResultType.RETRYABLE_ERROR,
        metadata={"spec_id": "demo", "phase": "implement", "message": "cli-failed"},
    ))
    scheduler = DispatchScheduler(
        store, cfg, executor, owner_id="scheduler-a", project_dir=tmp_path,
    )
    scheduler._mark_fallback("demo")  # simulate an earlier phase having degraded
    marker = tmp_path / ".builder" / "worktrees" / ".fallback-demo"
    assert marker.exists()

    scheduler.dispatch_once()
    assert scheduler.wait_for_attempts()

    assert not marker.exists()  # cleared on terminal FAILED, not left pinned


def test_blocked_human_clears_sticky_fallback_marker(tmp_path):
    """M-C(b): a spec that goes terminal BLOCKED_HUMAN must also have its M1
    sticky-fallback marker cleared."""
    from _dispatch_runtime.lane_executor import DispatchResult, DispatchResultType

    store = QueueStore(tmp_path)
    store.enqueue(task_ref={
        "kind": "builder-phase-batch",
        "runner_task_ref": ".builder/specs/demo/runs/phase-implement.yaml",
        "spec_id": "demo",
    })
    cfg = dispatch_config(tmp_path)
    executor = RecordingExecutor(DispatchResult(
        result_type=DispatchResultType.HUMAN_BLOCK,
        metadata={"spec_id": "demo", "phase": "implement", "reason": "predicate failed"},
    ))
    scheduler = DispatchScheduler(
        store, cfg, executor, owner_id="scheduler-a", project_dir=tmp_path,
    )
    scheduler._mark_fallback("demo")
    marker = tmp_path / ".builder" / "worktrees" / ".fallback-demo"
    assert marker.exists()

    scheduler.dispatch_once()
    assert scheduler.wait_for_attempts()

    assert not marker.exists()


def test_advance_after_success_clears_fallback_when_pipeline_completes_without_delivery(tmp_path):
    """M-C(b): a spec that completes its WHOLE pipeline (no next phase) with
    delivery disabled/not reached must also have its fallback marker cleared —
    `_cleanup_worktree` (the delivery-success clear point) never runs in that
    case, so without this fix the marker would stay set forever."""
    store = QueueStore(tmp_path)
    cfg = dispatch_config(tmp_path)  # deliver stays unset -> disabled
    executor = RecordingExecutor(success_result())
    scheduler = DispatchScheduler(
        store, cfg, executor, owner_id="scheduler-a", project_dir=tmp_path,
    )
    scheduler._mark_fallback("demo")
    marker = tmp_path / ".builder" / "worktrees" / ".fallback-demo"
    assert marker.exists()

    item = store.enqueue(task_ref={
        "kind": "builder-phase-batch",
        "runner_task_ref": ".builder/specs/demo/runs/phase-sync.yaml",
        "spec_id": "demo",
    })
    # "sync" is the terminal phase in the default order -> next_phase() is None.
    scheduler._advance_after_success(item, "sync")

    assert not marker.exists()


def test_maybe_env_up_under_isolation_uses_canonical_repo_and_target_dir(tmp_path):
    """M6: under worktree isolation, `lane_common.maybe_env_up` must select the
    profile name + `--projects-dir` from `control_root` (the canonical MAIN
    repo), NOT from the worktree — a worktree's `.name` is the spec id and its
    `.parent` is `.builder/worktrees`, neither of which is a real repo/profile
    key — and pass the worktree along as `--target-dir` so prereqs (npm ci, etc.)
    still run where the isolated source actually lives."""
    from _dispatch_runtime.lane_common import Work, maybe_env_up

    main_dir = tmp_path / "builder"
    main_dir.mkdir(parents=True, exist_ok=True)
    worktree_dir = main_dir / ".builder" / "worktrees" / "demo"
    worktree_dir.mkdir(parents=True, exist_ok=True)

    work = Work(
        work_id="w1", spec_id="demo", phase="implement",
        project_dir=worktree_dir, specs_dir=worktree_dir / ".builder" / "specs",
        runner_task_ref=None, capability_class=None,
        queue_root=tmp_path / "queue", log_path=tmp_path / "log.txt",
    )
    attempt_context = {"auto_env_up": True, "control_root": str(main_dir)}
    captured: list[list[str]] = []
    ran = maybe_env_up(work, attempt_context, runner=captured.append)

    assert ran
    assert len(captured) == 1
    argv = captured[0]
    assert argv[2:] == [
        "up", "builder",  # canonical repo NAME (main_dir.name), never "demo"
        "--projects-dir", str(main_dir.parent),
        "--target-dir", str(worktree_dir),
    ]


def test_maybe_env_up_non_isolated_argv_is_byte_identical_no_target_dir(tmp_path):
    """Flag-off / non-isolated byte-identity (M6): when `control_root` equals
    `workspace_root`/`project_dir` — or is simply absent, the legacy-caller case
    — `--target-dir` must never appear and the argv must match the pre-M6 shape
    exactly."""
    from _dispatch_runtime.lane_common import Work, maybe_env_up

    proj_dir = tmp_path / "builder"
    proj_dir.mkdir(parents=True, exist_ok=True)
    work = Work(
        work_id="w1", spec_id="demo", phase="implement",
        project_dir=proj_dir, specs_dir=proj_dir / ".builder" / "specs",
        runner_task_ref=None, capability_class=None,
        queue_root=tmp_path / "queue", log_path=tmp_path / "log.txt",
    )

    # Case 1: control_root explicitly set, equal to workspace_root/project_dir.
    captured_a: list[list[str]] = []
    ran_a = maybe_env_up(
        work,
        {"auto_env_up": True, "workspace_root": str(proj_dir), "control_root": str(proj_dir)},
        runner=captured_a.append,
    )
    assert ran_a
    argv_a = captured_a[0]
    assert argv_a[2:] == ["up", "builder", "--projects-dir", str(tmp_path)]
    assert "--target-dir" not in argv_a

    # Case 2: control_root absent entirely (an older/direct caller) -> falls
    # back to workspace_root, same result.
    captured_b: list[list[str]] = []
    ran_b = maybe_env_up(
        work, {"auto_env_up": True, "workspace_root": str(proj_dir)}, runner=captured_b.append,
    )
    assert ran_b
    assert captured_b[0] == argv_a
