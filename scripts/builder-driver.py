#!/usr/bin/env python3
"""Single-lease supervising driver: dispatch, watch, retry-with-feedback, escalate.

Replaces a human pushing every turn with one supervising loop that holds a
single-driver lease and keeps the pipeline moving:

  - dispatch the next ready turn (reuses the scheduler's own dispatch entry
    point, `DispatchScheduler.dispatch_once`);
  - watch for its terminal (SETTLED) outcome, never acting on a mid-turn
    measurement (R6: unreliable, discarded);
  - retry a failed turn with the validator/verifier feedback bundle attached
    to the next attempt;
  - treat a lane-cooldown drain as retryable (sleep for the cooldown, retry —
    never a terminal spec failure);
  - escalate with context once retries are exhausted, rather than silently
    stalling.

The supervising loop itself is decoupled from the real scheduler/executor
behind the `TurnSource` protocol so it is fully unit-testable; production
wiring goes through `SchedulerTurnSource`, a thin adapter over the existing
`DispatchScheduler` (dispatch_once / wait_for_attempts / store) and
`_dispatch_runtime.cooldown` — no parallel orchestrator, no new lease
primitive beyond the driver's own single-instance guard below.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso_now() -> str:
    return _utc_now().isoformat().replace("+00:00", "Z")


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


class DriverBusyError(RuntimeError):
    """Raised when a second driver attempts to start against a live lease."""


class DriverLease:
    """The single-driver lease (AC-R5-1): an exclusive lock file held for the
    WHOLE supervising run — distinct from (and orthogonal to) the scheduler's
    own per-dispatch-cycle lock, which is acquired/released every
    `dispatch_once()` call. A second driver attempting to start while this
    lease is live raises `DriverBusyError` rather than silently
    double-dispatching. A lease left by a dead process (pid no longer alive)
    is reclaimed — never a stale lock wedging every future driver — but two
    LIVE drivers never hold it concurrently.
    """

    def __init__(self, lock_path: Path, owner_id: str):
        self.lock_path = Path(lock_path)
        self.owner_id = owner_id

    def acquire(self) -> None:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):  # one steal-retry, mirroring the scheduler's own lock
            try:
                fd = self.lock_path.open("x", encoding="utf-8")
            except FileExistsError:
                owner = ""
                try:
                    owner = self.lock_path.read_text(encoding="utf-8").strip()
                except OSError:
                    pass
                pid = None
                try:
                    pid = int(owner.rsplit("-", 1)[-1])
                except (ValueError, IndexError):
                    pass
                # Unknown/malformed owners fail closed. In particular, the
                # same owner id is not proof of staleness: a second driver in
                # the same process must not steal the first live lease.
                stale = pid is not None and not _pid_alive(pid)
                if stale:
                    try:
                        self.lock_path.unlink()
                    except FileNotFoundError:
                        pass
                    continue
                raise DriverBusyError(
                    f"driver lease already held: {self.lock_path} (owner={owner or '?'})"
                )
            with fd:
                fd.write(f"{self.owner_id}\n")
            return
        raise DriverBusyError(f"driver lease still contended after stealing a stale lease: {self.lock_path}")

    def release(self) -> None:
        if self.lock_path.exists():
            try:
                owner = self.lock_path.read_text(encoding="utf-8").strip()
            except OSError:
                return
            if owner == self.owner_id:
                self.lock_path.unlink(missing_ok=True)

    def __enter__(self) -> "DriverLease":
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


@dataclass(frozen=True)
class DispatchDecision:
    """What `TurnSource.dispatch_next()` decided to do this cycle."""

    kind: str  # "turn" | "cooldown" | "idle"
    turn_id: str | None = None
    cooldown_seconds: int = 0


@dataclass(frozen=True)
class TurnOutcome:
    """A turn's (possibly mid-turn, possibly settled) observed state.

    `settled=False` marks a mid-turn measurement (R6: unreliable) — the
    driver must discard it, never retry/escalate/act on it, and poll again.
    """

    turn_id: str
    status: str  # "succeeded" | "failed" | "cooldown_drain"
    settled: bool = True
    feedback: str | None = None
    cooldown_seconds: int = 0


class TurnSource(Protocol):
    def dispatch_next(self) -> DispatchDecision: ...

    def watch(self, turn_id: str) -> TurnOutcome: ...

    def retry(self, turn_id: str, *, feedback: str) -> None: ...

    def escalate(self, turn_id: str, *, feedback: str) -> None: ...


@dataclass
class BuilderDriver:
    """The supervising loop. `max_retries` bounds retries of genuine FAILURES
    only — a cooldown drain is never counted against it (AC-R6-1: retryable,
    not a strike toward escalation)."""

    turn_source: TurnSource
    lease: DriverLease
    max_retries: int = 3
    heartbeat_path: Path | None = None
    sleep: Any = time.sleep
    _retry_counts: dict[str, int] = field(default_factory=dict)

    def _heartbeat(self) -> None:
        if self.heartbeat_path is None:
            return
        try:
            self.heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.heartbeat_path.with_name(f".{self.heartbeat_path.name}.{os.getpid()}.tmp")
            temporary.write_text(
                json.dumps({"pid": os.getpid(), "at": _iso_now(), "owner": self.lease.owner_id}),
                encoding="utf-8",
            )
            temporary.replace(self.heartbeat_path)
        except OSError:
            pass

    def run_once(self) -> TurnOutcome:
        """Dispatch the next ready turn, watch it to a SETTLED outcome
        (discarding any mid-turn reads along the way), then advance / retry
        with feedback / absorb a cooldown drain / escalate on exhaustion.
        Never raises on ordinary pipeline outcomes — those are all returned,
        not thrown."""
        self._heartbeat()
        decision = self.turn_source.dispatch_next()

        if decision.kind == "idle":
            return TurnOutcome(turn_id="", status="idle", settled=True)

        if decision.kind == "cooldown":
            # AC-R6-1: a lane-cooldown drain is retryable -- sleep for the
            # cooldown and let the caller loop back, never a terminal failure.
            self.sleep(decision.cooldown_seconds)
            return TurnOutcome(
                turn_id="", status="cooldown_drain", settled=True, cooldown_seconds=decision.cooldown_seconds,
            )

        turn_id = decision.turn_id or ""
        self._heartbeat()
        outcome = self.turn_source.watch(turn_id)
        self._heartbeat()
        while not outcome.settled:
            # AC-R6-2: a mid-turn measurement is never acted upon -- discard
            # and poll again rather than retry/escalate/gate off of it.
            self._heartbeat()
            outcome = self.turn_source.watch(turn_id)
            self._heartbeat()

        if outcome.status == "succeeded":
            self._retry_counts.pop(turn_id, None)
            return outcome

        if outcome.status in {"blocked_human", "cancelled"}:
            # These are terminal host states, but neither is a successful turn
            # nor a driver-retryable failure.
            self._retry_counts.pop(turn_id, None)
            return outcome

        if outcome.status == "cooldown_drain":
            self.sleep(outcome.cooldown_seconds)
            self.turn_source.retry(turn_id, feedback=outcome.feedback or "")
            return outcome

        # "failed": retry with feedback attached, up to max_retries, then escalate.
        count = self._retry_counts.get(turn_id, 0) + 1
        self._retry_counts[turn_id] = count
        if count > self.max_retries:
            self.turn_source.escalate(turn_id, feedback=outcome.feedback or "")
            self._retry_counts.pop(turn_id, None)
        else:
            self.turn_source.retry(turn_id, feedback=outcome.feedback or "")
        return outcome


# --- production wiring: adapt the existing DispatchScheduler ----------------


class SchedulerTurnSource:
    """Adapts a real `DispatchScheduler` (+ its `QueueStore`) to `TurnSource`.
    Reuses the scheduler's own dispatch entry point and lease semantics
    (`dispatch_once`, `wait_for_attempts`) rather than forking a parallel
    orchestrator; cooldown state comes from `_dispatch_runtime.cooldown`."""

    def __init__(self, scheduler, *, watch_timeout: float = 30.0):
        self.scheduler = scheduler
        self.watch_timeout = watch_timeout
        self._pending_turn_ids: list[str] = []

    def dispatch_next(self) -> DispatchDecision:
        from _dispatch_runtime.cooldown import cooldown_remaining_seconds
        from _dispatch_runtime.state_model import WorkItemState

        if self._pending_turn_ids:
            return DispatchDecision(kind="turn", turn_id=self._pending_turn_ids.pop(0))

        dispatched = self.scheduler.dispatch_once()
        if dispatched:
            self._pending_turn_ids.extend(dispatched[1:])
            return DispatchDecision(kind="turn", turn_id=dispatched[0])

        snapshot = self.scheduler.store.reconstruct()
        has_queued = any(it.state == WorkItemState.QUEUED for it in snapshot.items.values())
        if not has_queued:
            return DispatchDecision(kind="idle")
        remaining = [
            cooldown_remaining_seconds(record) for record in snapshot.lanes.values()
            if record is not None
        ]
        remaining = [r for r in remaining if r > 0]
        if remaining:
            return DispatchDecision(kind="cooldown", cooldown_seconds=max(remaining))
        return DispatchDecision(kind="idle")

    def watch(self, turn_id: str) -> TurnOutcome:
        from _dispatch_runtime.state_model import TERMINAL_STATES, WorkItemState

        self.scheduler.wait_for_attempts(timeout=self.watch_timeout)
        item = self.scheduler.store.get_item(turn_id)
        if item is None:
            return TurnOutcome(
                turn_id=turn_id, status="failed", settled=True,
                feedback="dispatched work item disappeared from the queue store",
            )
        if item.state not in TERMINAL_STATES:
            # Not settled yet -- a mid-flight read the driver must discard.
            return TurnOutcome(turn_id=turn_id, status="failed", settled=False)
        if item.state == WorkItemState.FAILED:
            return TurnOutcome(
                turn_id=turn_id, status="failed", settled=True,
                feedback=str((item.task_ref or {}).get("last_error") or ""),
            )
        if item.state == WorkItemState.BLOCKED_HUMAN:
            return TurnOutcome(
                turn_id=turn_id, status="blocked_human", settled=True,
                feedback=str((item.task_ref or {}).get("last_error") or ""),
            )
        if item.state == WorkItemState.CANCELLED:
            return TurnOutcome(turn_id=turn_id, status="cancelled", settled=True)
        return TurnOutcome(turn_id=turn_id, status="succeeded", settled=True)

    def retry(self, turn_id: str, *, feedback: str) -> None:
        item = self.scheduler.store.get_item(turn_id)
        if item is None:
            return
        item.task_ref["retry_feedback"] = feedback
        from _dispatch_runtime.state_model import WorkItemState

        item.state = WorkItemState.QUEUED
        item.lease = {}
        self.scheduler.store.save_item(item)

    def escalate(self, turn_id: str, *, feedback: str) -> None:
        item = self.scheduler.store.get_item(turn_id)
        if item is None:
            return
        from _dispatch_runtime.state_model import WorkItemState

        item.state = WorkItemState.BLOCKED_HUMAN
        item.task_ref["last_error"] = feedback or item.task_ref.get("last_error") or "retries exhausted"
        item.lease = {}
        self.scheduler.store.save_item(item)
        self.scheduler._notify("blocked_human", {  # noqa: SLF001 - same notifier the scheduler itself uses
            "spec_id": self.scheduler._spec_id_for(item) or "?",
            "phase": "?", "lane": item.lane or "?",
            "reason": feedback or "retries exhausted", "work_id": item.id,
        })


def build_driver(project_dir: Path, *, owner_id: str, max_retries: int = 3) -> BuilderDriver:
    from _dispatch_runtime.config import load_dispatch_config
    from _dispatch_runtime.paths import runtime_dir
    from _dispatch_runtime.queue_store import QueueStore
    from _dispatch_runtime.scheduler import DispatchScheduler

    config = load_dispatch_config(runtime_dir(project_dir) / "dispatch.yaml")
    store = QueueStore(config.queue_store_path)

    class _RoutingExecutor:
        def __init__(self, cfg):
            from _dispatch_runtime.lane_claude_code_cli import ClaudeCodeCliLane
            from _dispatch_runtime.lane_codex_cli import CodexCliLane

            self._cfg = cfg
            self._claude = ClaudeCodeCliLane()
            self._codex = CodexCliLane()

        def execute(self, task_ref, lane_name, attempt_context):
            lane = self._cfg.lanes.get(lane_name)
            provider = (lane.provider if lane else lane_name) or lane_name
            return (self._codex if "codex" in provider.lower() else self._claude).execute(
                task_ref, lane_name, attempt_context
            )

    scheduler = DispatchScheduler(store, config, _RoutingExecutor(config), owner_id=owner_id, project_dir=project_dir)
    turn_source = SchedulerTurnSource(scheduler)
    lease = DriverLease(runtime_dir(project_dir) / "driver.lock", owner_id)
    heartbeat_path = runtime_dir(project_dir) / "driver-heartbeat.json"
    return BuilderDriver(turn_source=turn_source, lease=lease, max_retries=max_retries, heartbeat_path=heartbeat_path)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="builder-driver")
    parser.add_argument("--project-dir", default=".")
    parser.add_argument("--interval", type=float, default=5.0)
    parser.add_argument("--max-retries", type=int, default=3)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    project_dir = Path(args.project_dir).resolve()
    owner_id = f"driver-{os.getpid()}"
    driver = build_driver(project_dir, owner_id=owner_id, max_retries=args.max_retries)
    try:
        driver.lease.acquire()
    except DriverBusyError as exc:
        print(f"builder-driver: {exc}", flush=True)
        return 1
    try:
        while True:
            outcome = driver.run_once()
            print(f"{outcome.status}: {outcome.turn_id or '(idle)'}", flush=True)
            if args.once:
                return 0
            if outcome.status == "idle":
                time.sleep(args.interval)
    finally:
        driver.lease.release()


if __name__ == "__main__":
    raise SystemExit(main())
