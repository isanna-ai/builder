"""R6: circuit breaker (consecutive failed specs), roadmap budget (token / attempt-wall),
and the verify<->implement rework-cycle bound with durable BLOCKED_HUMAN escalation."""

from __future__ import annotations

from _dispatch_runtime.config import DispatchConfig, LaneConfig
from _dispatch_runtime.queue_store import QueueStore
from _dispatch_runtime.scheduler import DispatchScheduler
from _dispatch_runtime.state_model import WorkItemState


class _Exec:
    """Dispatch is never invoked in these unit tests (we drive the helpers directly)."""

    def execute(self, *a, **k):  # pragma: no cover - guard
        raise AssertionError("executor.execute must not be called in these tests")


def _config(tmp_path, *, pipeline=None, retry=None) -> DispatchConfig:
    lanes = {"claude-code-cli": LaneConfig(name="claude-code-cli", provider="claude-code-cli", max_concurrency=1)}
    return DispatchConfig(
        queue_store_path=tmp_path,
        lanes=lanes,
        routing_policy={"default": "ordered"},
        cooldown_policy={"default_seconds": 60},
        retry_policy={"max_attempts": 3, "initial_seconds": 5, "max_seconds": 30,
                      "jitter_seconds": 0, "rework_max": 0, **(retry or {})},
        pipeline=pipeline or {},
    )


def _scheduler(tmp_path, *, pipeline=None, retry=None):
    store = QueueStore(tmp_path)
    cfg = _config(tmp_path, pipeline=pipeline, retry=retry)
    return DispatchScheduler(store, cfg, _Exec(), owner_id="s", project_dir=tmp_path), store


# --- consecutive-failure breaker -------------------------------------------

def test_breaker_trips_and_latches(tmp_path):
    sch, _ = _scheduler(tmp_path, pipeline={"max_consecutive_failed_specs": 2})
    assert sch._circuit_tripped() is None
    sch._note_spec_outcome(success=False)
    assert sch._circuit_tripped() is None          # 1 < 2
    sch._note_spec_outcome(success=False)
    reason = sch._circuit_tripped()
    assert reason and "consecutive failed" in reason  # 2 >= 2 -> tripped
    sch._note_spec_outcome(success=True)           # a later success does NOT un-latch the trip
    assert sch._circuit_tripped() is not None      # durable — recovery is manual .drain removal


def test_breaker_counter_resets_before_threshold(tmp_path):
    sch, _ = _scheduler(tmp_path, pipeline={"max_consecutive_failed_specs": 3})
    sch._note_spec_outcome(success=False)          # 1
    sch._note_spec_outcome(success=True)           # a spec completing resets the run -> 0
    sch._note_spec_outcome(success=False)          # 1
    assert sch._circuit_tripped() is None          # never reached 3, never latched


def test_breaker_disabled_by_default(tmp_path):
    sch, _ = _scheduler(tmp_path)  # no max_consecutive_failed_specs -> off
    for _ in range(10):
        sch._note_spec_outcome(success=False)
    assert sch._circuit_tripped() is None


# --- roadmap budget ---------------------------------------------------------

def test_token_budget_trips(tmp_path):
    sch, _ = _scheduler(tmp_path, pipeline={"roadmap_budget": {"max_tokens": 100}})
    sch._accumulate_spend({"plan_tokens_out": 60, "plan_tokens_in": 30}, 0)  # 90 < 100
    assert sch._circuit_tripped() is None
    sch._accumulate_spend({"plan_tokens_out": 20}, 0)                        # 110 >= 100
    reason = sch._circuit_tripped()
    assert reason and "token budget" in reason


def test_wall_budget_counts_attempt_time_not_idle(tmp_path):
    sch, _ = _scheduler(tmp_path, pipeline={"roadmap_budget": {"max_wall_seconds": 10}})
    assert sch._circuit_tripped() is None          # idle daemon never self-pauses
    sch._accumulate_spend({}, 20_000)              # 20s of ATTEMPT time
    reason = sch._circuit_tripped()
    assert reason and "wall budget" in reason


def test_garbage_config_does_not_raise(tmp_path):
    # A bad config value must never raise on the dispatch loop.
    sch, _ = _scheduler(tmp_path, pipeline={"max_consecutive_failed_specs": "two",
                                            "roadmap_budget": {"max_tokens": "lots"}})
    sch._note_spec_outcome(success=False)
    assert sch._circuit_tripped() is None          # garbage thresholds coerce to 0 (off)


# --- pause behavior ---------------------------------------------------------

def test_pause_queue_writes_drain_and_notifies_once(tmp_path):
    sch, store = _scheduler(tmp_path, pipeline={"max_consecutive_failed_specs": 1})
    sch._pause_queue("boom")
    assert (store.queue_dir / ".drain").exists()
    assert sch._breaker_notified is True


def test_dispatch_once_pauses_when_tripped(tmp_path):
    sch, store = _scheduler(tmp_path, pipeline={"max_consecutive_failed_specs": 1})
    store.enqueue(task_ref={"kind": "builder-runner-task", "runner_task_ref": "runs/task-T1.yaml"})
    sch._note_spec_outcome(success=False)          # 1 >= 1 -> tripped
    dispatched = sch.dispatch_once()
    assert dispatched == []
    assert (store.queue_dir / ".drain").exists()


# --- rework-cycle bound -----------------------------------------------------

def test_as_num_rejects_garbage_negative_and_nonfinite():
    f = DispatchScheduler._as_num
    assert f(3, 0) == 3
    assert f("nope", 0) == 0        # garbage -> default
    assert f(-5, 0) == 0            # negative threshold -> off (not "trip always")
    assert f(float("inf"), 0) == 0  # OverflowError on int(inf) -> default
    assert f(float("nan"), 0.0) == 0.0
    assert f(float("inf"), 0.0) == 0.0


def test_negative_threshold_is_off(tmp_path):
    sch, _ = _scheduler(tmp_path, pipeline={"max_consecutive_failed_specs": -1})
    for _ in range(5):
        sch._note_spec_outcome(success=False)
    assert sch._circuit_tripped() is None


def test_non_mapping_budget_does_not_crash(tmp_path):
    sch, _ = _scheduler(tmp_path, pipeline={"roadmap_budget": "all of it"})
    sch._accumulate_spend({"plan_tokens_out": 10_000}, 999)
    assert sch._circuit_tripped() is None  # non-mapping budget coerces to no-limit


def test_rework_counter_bump_and_reset(tmp_path):
    sch, _ = _scheduler(tmp_path)
    assert sch._bump_rework("demo") == 1
    assert sch._bump_rework("demo") == 2
    sch._reset_rework("demo")
    assert sch._bump_rework("demo") == 1


def test_rework_counter_fails_closed_on_corrupt_file(tmp_path):
    sch, _ = _scheduler(tmp_path)
    sch._rework_path("demo").parent.mkdir(parents=True, exist_ok=True)
    sch._rework_path("demo").write_text("not-a-number", encoding="utf-8")
    assert sch._bump_rework("demo") >= 1_000_000  # corrupt counter -> escalate, never reset to 1


def _implement_items(store, spec_id, *, state=None):
    out = []
    for it in store.reconstruct().items.values():
        if it.task_ref.get("spec_id") == spec_id and "implement" in str(it.task_ref.get("runner_task_ref", "")):
            if state is None or it.state == state:
                out.append(it)
    return out


def _verify_spec(tmp_path, store, spec_id):
    spec_dir = tmp_path / ".builder" / "specs" / spec_id
    spec_dir.mkdir(parents=True, exist_ok=True)
    # Fresh handoff that loops verify -> implement (the VERIFIED_WITH_TASKS rework).
    (spec_dir / "handoff.yaml").write_text(
        "completed_phase: verify\nnext_phase: implement\n", encoding="utf-8")
    return store.enqueue(task_ref={
        "kind": "builder-phase-batch", "spec_id": spec_id,
        "runner_task_ref": f".builder/specs/{spec_id}/runs/phase-verify.yaml"})


def test_advance_enqueues_within_rework_budget(tmp_path):
    sch, store = _scheduler(tmp_path, retry={"rework_max": 2})
    item = _verify_spec(tmp_path, store, "demo")
    sch._advance_after_success(item, "verify")     # count 1 <= 2 -> enqueue implement (QUEUED)
    queued = _implement_items(store, "demo", state=WorkItemState.QUEUED)
    assert len(queued) == 1


def test_advance_escalates_to_blocked_human_after_rework_max(tmp_path):
    sch, store = _scheduler(tmp_path, retry={"rework_max": 2})
    item = _verify_spec(tmp_path, store, "demo")
    sch._bump_rework("demo")
    sch._bump_rework("demo")                        # counter at rework_max (2)
    sch._advance_after_success(item, "verify")      # -> 3 > 2 -> durable BLOCKED_HUMAN, no normal enqueue
    assert _implement_items(store, "demo", state=WorkItemState.QUEUED) == []
    blocked = _implement_items(store, "demo", state=WorkItemState.BLOCKED_HUMAN)
    assert len(blocked) == 1
    assert "rework" in blocked[0].task_ref.get("last_error", "")


def test_advance_rework_bump_is_idempotent_vs_active_item(tmp_path):
    # A duplicate/replayed verify success must not inflate the counter when an implement
    # item already exists (bump is gated on _has_active_item).
    sch, store = _scheduler(tmp_path, retry={"rework_max": 2})
    item = _verify_spec(tmp_path, store, "demo")
    sch._advance_after_success(item, "verify")      # count 1, enqueues implement
    sch._advance_after_success(item, "verify")      # replay: implement now active -> no bump
    # Counter is 1, not 2: a third *genuine* loop would still be allowed.
    assert sch._bump_rework("demo") == 2
