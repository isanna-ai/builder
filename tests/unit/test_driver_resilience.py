"""T5: the driver absorbs lane-cooldown drains as retryable and never acts on
a mid-turn (unsettled) measurement (AC-R6-1, AC-R6-2).
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
    def __init__(self, decisions, outcomes=None):
        self.decisions = list(decisions)
        self.outcomes = {k: list(v) for k, v in (outcomes or {}).items()}
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
    sleeps: list[float] = []
    driver = bd.BuilderDriver(
        turn_source=turn_source, lease=lease, heartbeat_path=tmp_path / "heartbeat.json",
        sleep=lambda s: sleeps.append(s), **kwargs,
    )
    return driver, sleeps


# --- AC-R6-1: a lane-cooldown drain is retryable, never a terminal failure ----


def test_cooldown_drain_before_dispatch_sleeps_and_is_never_a_terminal_failure_cooldown(tmp_path):
    ts = FakeTurnSource(decisions=[bd.DispatchDecision(kind="cooldown", cooldown_seconds=45)])
    driver, sleeps = _driver(ts, tmp_path)

    outcome = driver.run_once()

    assert outcome.status == "cooldown_drain"
    assert sleeps == [45]
    assert ts.retry_calls == []  # nothing was dispatched yet -- there's no turn to retry
    assert ts.escalate_calls == []  # never escalated


def test_cooldown_drain_observed_mid_turn_sleeps_then_retries_the_same_turn_cooldown(tmp_path):
    ts = FakeTurnSource(
        decisions=[bd.DispatchDecision(kind="turn", turn_id="work-1")],
        outcomes={"work-1": [bd.TurnOutcome(
            turn_id="work-1", status="cooldown_drain", cooldown_seconds=30, feedback="lane rate-limited",
        )]},
    )
    driver, sleeps = _driver(ts, tmp_path)

    outcome = driver.run_once()

    assert outcome.status == "cooldown_drain"
    assert sleeps == [30]
    assert ts.retry_calls == [("work-1", "lane rate-limited")]
    assert ts.escalate_calls == []


def test_repeated_cooldown_drains_never_escalate_even_past_max_retries_cooldown(tmp_path):
    """A cooldown drain must never count against the retry-exhaustion budget --
    otherwise a lane sitting in a long rate-limit cooldown would wrongly
    escalate a perfectly healthy spec as if it had failed repeatedly."""
    ts = FakeTurnSource(
        decisions=[bd.DispatchDecision(kind="turn", turn_id="work-2")],
        outcomes={"work-2": [bd.TurnOutcome(turn_id="work-2", status="cooldown_drain", cooldown_seconds=5)]},
    )
    driver, sleeps = _driver(ts, tmp_path, max_retries=1)

    for _ in range(5):  # far more than max_retries
        outcome = driver.run_once()
        assert outcome.status == "cooldown_drain"

    assert ts.escalate_calls == []
    assert len(ts.retry_calls) == 5
    assert sleeps == [5, 5, 5, 5, 5]


# --- AC-R6-2: a mid-turn measurement is discarded, never acted upon ----------


def test_unsettled_measurement_never_triggers_retry_or_escalation():
    ts = FakeTurnSource(
        decisions=[bd.DispatchDecision(kind="turn", turn_id="work-3")],
        outcomes={"work-3": [
            # A mid-turn read that LOOKS like a failure -- must be discarded, not acted on.
            bd.TurnOutcome(turn_id="work-3", status="failed", settled=False, feedback="looks bad mid-flight"),
            bd.TurnOutcome(turn_id="work-3", status="failed", settled=False, feedback="still mid-flight"),
            bd.TurnOutcome(turn_id="work-3", status="succeeded", settled=True),
        ]},
    )
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        driver, sleeps = _driver(ts, Path(tmp))
        outcome = driver.run_once()

    assert outcome.status == "succeeded"
    assert ts.retry_calls == []  # the mid-turn "failed"-looking reads never fired a retry
    assert ts.escalate_calls == []
    assert sleeps == []  # and never a cooldown sleep either


def test_unsettled_measurement_that_eventually_settles_failed_still_retries_once():
    ts = FakeTurnSource(
        decisions=[bd.DispatchDecision(kind="turn", turn_id="work-4")],
        outcomes={"work-4": [
            bd.TurnOutcome(turn_id="work-4", status="succeeded", settled=False),  # mid-turn noise, discarded
            bd.TurnOutcome(turn_id="work-4", status="failed", settled=True, feedback="real failure"),
        ]},
    )
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        driver, _ = _driver(ts, Path(tmp), max_retries=3)
        outcome = driver.run_once()

    # Only the SETTLED status is honored -- the mid-turn "succeeded" noise is
    # never mistaken for the real (failed) outcome.
    assert outcome.status == "failed"
    assert ts.retry_calls == [("work-4", "real failure")]
