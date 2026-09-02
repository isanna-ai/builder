"""Dispatch scheduler, lock, lease, and routing orchestration."""

from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import threading
import time
import types
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from uuid import uuid4

from _dispatch_runtime.backoff import apply_failure_backoff
from _dispatch_runtime.config import DispatchConfig
from _dispatch_runtime.cooldown import cooldown_remaining_seconds, lane_available, open_lane_cooldown
from _dispatch_runtime import gate_policy
from _dispatch_runtime.lane_executor import DispatchResultType, LaneExecutor
from _dispatch_runtime.notifier import Notifier, build_notifier
from _dispatch_runtime.personas import select_independent_review_lane
from _dispatch_runtime.phase_routing import REVIEW_LANE_PHASES, route_lane
from _dispatch_runtime.paths import RUNTIME_DIR_NAMES, runtime_dir
from _dispatch_runtime.phase_runtime import (
    PHASE_META,
    PRE_IMPLEMENT_PHASES,
    _resolve_spec_dir,
    _safe_yaml,
    effective_phase_order,
    next_phase,
    normalize_phase,
    phase_order_for_count,
    review_count_for_spec,
)
from _dispatch_runtime.queue_store import QueueStore, WorkItem
from _dispatch_runtime.routing import UnknownLaneHintError, resolve_lane
from _dispatch_runtime.state_model import TERMINAL_STATES, WorkItemState


def _owner_pid(owner_id: str) -> int | None:
    """Extract the pid from an owner id of the form 'dispatch-<pid>'."""
    try:
        return int(str(owner_id).rsplit("-", 1)[-1])
    except (ValueError, IndexError):
        return None


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:  # exists, owned by another user
        return True
    except OSError:
        return False


class SchedulerBusyError(RuntimeError):
    """Raised when another scheduler owns the queue-store lock."""


class _GitWorktreeRunner:
    """Default git-worktree command runner: a real-subprocess wrapper matching
    delivery.py's `_DefaultRunner` interface (`.run(argv, cwd) -> CompletedProcess`),
    so R5 worktree ops and delivery share one injectable seam (tests can pass a fake
    via `worktree_runner=` without touching a real repo)."""

    def run(self, argv: list[str], cwd: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(argv, cwd=cwd, text=True, capture_output=True, check=False)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso_now() -> str:
    return _utc_now().isoformat().replace("+00:00", "Z")


def _parse_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _porcelain_residual_paths(porcelain_output: str) -> list[str]:
    """H-3: parse `git status --porcelain` output into the list of (new) paths
    it reports, taking the right-hand side of a rename (`R  old -> new`).
    Used by `_cleanup_worktree` to tell the EXPECTED `.builder/` control-dir
    redirect noise (Model A's rmtree+symlink shows as a delete+typechange)
    apart from real unrecorded source changes an agent left uncommitted."""
    paths: list[str] = []
    for line in porcelain_output.splitlines():
        if not line.strip():
            continue
        rest = line[3:] if len(line) > 3 else line.lstrip()
        if " -> " in rest:
            rest = rest.split(" -> ", 1)[1]
        paths.append(rest.strip().strip('"'))
    return paths


class DispatchScheduler:
    def __init__(
        self,
        store: QueueStore,
        config: DispatchConfig,
        executor: LaneExecutor,
        *,
        owner_id: str,
        lease_seconds: int = 300,
        project_dir: Path | None = None,
        notifier: Notifier | None = None,
        worktree_runner: Any | None = None,
    ):
        self.store = store
        self.config = config
        self.executor = executor
        self.owner_id = owner_id
        self.lease_seconds = lease_seconds
        # R5 per-spec worktree isolation (opt-in via pipeline.worktree_isolation):
        # injectable so tests never touch a real repo; defaults to a real subprocess
        # runner. Also handed to delivery.deliver() as its `runner=` when isolation
        # is on, so a single fake can observe both worktree ops and delivery's git
        # calls in one test.
        self._worktree_runner = worktree_runner or _GitWorktreeRunner()
        # The repo the queue belongs to: queue_store path is <project>/.builder/dispatch-queue,
        # so the project root is two levels above store.root unless given explicitly.
        self.project_dir = Path(project_dir) if project_dir else self.store.root.parent.parent
        self.pipeline = dict(getattr(config, "pipeline", {}) or {})
        self.notifier = notifier or build_notifier(self.pipeline, self.store.root)
        # Independent-review pipeline (opt-in per dispatcher). When enabled, the
        # active phase order gains spec-review + adversarial-review + review-fix, and
        # the two review phases route to the review lane (codex/gpt-5.4) so the
        # reviewer is a different model than the author. Set the active order ONCE
        # here (one dispatcher == one process == one config) so next_phase() — used by
        # both the agent's completion bookkeeping and this scheduler — stays consistent.
        from _dispatch_runtime.phase_runtime import set_active_phase_order
        _reviews_cfg = self.pipeline.get("reviews") or {}
        self._reviews_enabled = bool(_reviews_cfg.get("enabled", False))
        self._reviews_default = int(_reviews_cfg.get("default", 1 if self._reviews_enabled else 0))
        self._review_lane = str(_reviews_cfg.get("lane", "codex"))
        set_active_phase_order(effective_phase_order(self._reviews_default > 0))
        self.lock_path = self.store.queue_dir / ".scheduler.lock"
        self._attempt_threads: set[threading.Thread] = set()
        self._attempt_threads_lock = threading.Lock()
        # R6 circuit-breaker / budget metrics. Updated by attempt threads, read by
        # dispatch_once — guarded by _metrics_lock. Wall is summed across ATTEMPTS (real
        # work), not daemon uptime, so an idle daemon never self-pauses on the wall budget.
        self._metrics_lock = threading.Lock()
        self._consecutive_failed = 0
        self._spent_tokens = 0
        self._spent_wall_ms = 0
        self._breaker_notified = False
        # Latched trip reason: set (under the lock) the instant a threshold is crossed, so
        # a later reset can never lose the trip. Durable within the run — recovery is a
        # manual .drain removal + restart, never an automatic un-pause.
        self._tripped_reason: str | None = None

    def acquire_scheduler_lock(self, *, wait: bool = False) -> str:
        # One steal-retry: a daemon killed without releasing leaves a stale lock
        # that would otherwise wedge every future daemon. If the recorded owner's
        # process is dead (or the lock is our own prior incarnation), steal it.
        for _ in range(2):
            try:
                fd = self.lock_path.open("x", encoding="utf-8")
            except FileExistsError as exc:
                owner = ""
                try:
                    owner = self.lock_path.read_text(encoding="utf-8").strip()
                except OSError:
                    pass
                pid = _owner_pid(owner)
                stale = bool(owner) and (
                    owner == self.owner_id or (pid is not None and not _pid_alive(pid))
                )
                if stale:
                    try:
                        self.lock_path.unlink()
                    except FileNotFoundError:
                        pass
                    continue  # retry the exclusive create
                raise SchedulerBusyError(
                    f"scheduler lock is owned by another process: {self.lock_path} (owner={owner or '?'})"
                ) from exc
            with fd:
                fd.write(f"{self.owner_id}\n")
            return self.owner_id
        raise SchedulerBusyError(
            f"scheduler lock still contended after stealing a stale lock: {self.lock_path}"
        )

    def release_scheduler_lock(self) -> None:
        if self.lock_path.exists():
            owner = self.lock_path.read_text(encoding="utf-8").strip()
            if owner == self.owner_id:
                self.lock_path.unlink()

    @contextmanager
    def scheduler_lock(self):
        self.acquire_scheduler_lock(wait=False)
        try:
            yield
        finally:
            self.release_scheduler_lock()

    def dispatch_once(self) -> list[str]:
        with self.scheduler_lock():
            self.reclaim_stale_leases()
            self.reap_completed_phase_items()
            self._process_veto_windows()
            # R6: circuit breaker / roadmap budget. If tripped, pause the queue
            # (write .drain, honored by the daemon loop) and admit no new work.
            tripped = self._circuit_tripped()
            if tripped:
                self._pause_queue(tripped)
                return []
            dispatched: list[str] = []
            for item in self._dispatchable_items():
                lane_name = self._route_item(item)
                if lane_name is None:
                    continue
                self._execute_item(item, lane_name)
                dispatched.append(item.id)
                break
            return dispatched

    # --- R6 circuit breaker / roadmap budget ------------------------------
    @staticmethod
    def _as_num(value, default=0):
        """Coerce a config value to a finite, non-negative number, defaulting on garbage
        (a bad config value must never raise on the attempt thread / dispatch loop, and a
        negative threshold must not 'trip always')."""
        try:
            n = type(default)(value)
        except (TypeError, ValueError, OverflowError):
            return default
        if isinstance(n, float) and not math.isfinite(n):
            return default
        return type(default)(0) if n < 0 else n

    def _latch_locked(self) -> None:
        """Latch the trip reason if any threshold is now crossed. MUST be called while
        holding _metrics_lock (right after a metric update), so the trip is durable even
        if a later reset lowers the metric. Opt-in thresholds (0 = off)."""
        if self._tripped_reason is not None:
            return
        max_fail = self._as_num(self.pipeline.get("max_consecutive_failed_specs", 0), 0)
        if max_fail and self._consecutive_failed >= max_fail:
            self._tripped_reason = f"{self._consecutive_failed} consecutive failed specs (max {max_fail})"
            return
        budget = self.pipeline.get("roadmap_budget")
        if not isinstance(budget, dict):  # a non-mapping budget must not reach .get()
            budget = {}
        max_tokens = self._as_num(budget.get("max_tokens", 0), 0)
        if max_tokens and self._spent_tokens >= max_tokens:
            self._tripped_reason = f"token budget spent ({self._spent_tokens} >= {max_tokens})"
            return
        max_wall = self._as_num(budget.get("max_wall_seconds", 0), 0.0)
        if max_wall and (self._spent_wall_ms / 1000.0) >= max_wall:
            self._tripped_reason = (f"wall budget spent ({int(self._spent_wall_ms/1000)}s "
                                    f">= {int(max_wall)}s of attempt time)")

    def _circuit_tripped(self) -> str | None:
        """The latched trip reason (consecutive failures / token / wall budget), or None.
        Latched at threshold-crossing time so a concurrent reset can never lose the trip."""
        with self._metrics_lock:
            return self._tripped_reason

    def _pause_queue(self, reason: str) -> None:
        """Pause the repo queue (write .drain, honored by the daemon loop) + notify ONCE."""
        try:
            (self.store.queue_dir / ".drain").write_text(
                f"circuit breaker: {reason}\n", encoding="utf-8")
        except Exception:  # noqa: BLE001 - pause marker best-effort
            pass
        with self._metrics_lock:
            already = self._breaker_notified
            self._breaker_notified = True
        if not already:
            self._notify("circuit_breaker", {"reason": reason, "work_id": "circuit-breaker"})

    def _accumulate_spend(self, metadata: dict[str, Any] | None, wall_ms: int) -> None:
        """R6: accumulate tokens (in+out) and real attempt wall-time for the budget."""
        md = metadata or {}
        tokens = self._as_num(md.get("plan_tokens_out"), 0) + self._as_num(md.get("plan_tokens_in"), 0)
        with self._metrics_lock:
            self._spent_tokens += tokens
            self._spent_wall_ms += max(0, self._as_num(wall_ms, 0))
            self._latch_locked()

    def _note_spec_outcome(self, *, success: bool) -> None:
        """Track consecutive terminal failures for the circuit breaker (reset on any
        success so the breaker fires only on an unbroken run of failures)."""
        with self._metrics_lock:
            if success:
                self._consecutive_failed = 0
            else:
                self._consecutive_failed += 1
            self._latch_locked()

    def _rework_path(self, spec_id: str) -> Path:
        return self.store.queue_dir / "rework" / str(spec_id)

    _REWORK_FAIL_CLOSED = 1_000_000  # any read/write error -> force escalation, never loop unbounded

    def _bump_rework(self, spec_id: str) -> int:
        """Increment and return the per-spec verify<->implement rework counter.
        Fails CLOSED: only a genuinely-absent counter is 0; a corrupt/unreadable counter or
        a failed write returns a large value so the loop escalates rather than running
        unbounded (a reset-to-1 would let the bound be bypassed)."""
        p = self._rework_path(spec_id)
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            return self._REWORK_FAIL_CLOSED
        if not p.exists():
            count = 0
        else:
            try:
                count = int(p.read_text(encoding="utf-8").strip() or "0")
            except (OSError, ValueError):
                return self._REWORK_FAIL_CLOSED  # corrupt/unreadable -> escalate
        count += 1
        try:
            p.write_text(str(count), encoding="utf-8")
        except OSError:
            return self._REWORK_FAIL_CLOSED  # cannot persist -> escalate
        return count

    def _reset_rework(self, spec_id: str) -> None:
        try:
            self._rework_path(spec_id).unlink(missing_ok=True)
        except OSError:
            pass

    def _block_rework_exceeded(self, spec_id: str, nxt: str, item: WorkItem, count: int) -> None:
        """Escalate a runaway rework loop to a HUMAN: enqueue the next phase as a durable
        BLOCKED_HUMAN item (visible in `status`, with a real reason) instead of silently
        stranding the spec, and clear the counter so a human re-dispatch starts fresh."""
        routed = self._route_phase(nxt, item.lane)
        reason = f"verify<->implement rework loop exceeded rework_max ({count} loops)"
        # Construct the item ALREADY blocked and write it in ONE store op — never a QUEUED
        # window the concurrent dispatch loop (which runs unlocked vs this attempt thread)
        # could claim.
        blocked = WorkItem(
            id=f"work-{uuid4().hex}",
            state=WorkItemState.BLOCKED_HUMAN,
            task_ref={"kind": "builder-phase-batch",
                      "runner_task_ref": f"{runtime_dir(self.project_dir).name}/specs/{spec_id}/runs/phase-{nxt}.yaml",
                      "spec_id": spec_id, "last_error": reason},
            priority=item.priority,
            lane=routed,
        )
        self.store.save_item(blocked)
        self._reset_rework(spec_id)  # a human requeue starts a fresh rework budget
        synthetic = types.SimpleNamespace(
            result_type=DispatchResultType.HUMAN_BLOCK,
            metadata={"spec_id": spec_id, "phase": nxt, "reason": reason, "count": count})
        self._notify_blocked(blocked, synthetic, {})

    def reclaim_stale_leases(self, active_attempt_ids: set[str] | None = None) -> list[str]:
        reclaimed: list[str] = []
        active = active_attempt_ids
        for item in self.store.reconstruct().items.values():
            if item.state not in {WorkItemState.DISPATCHED, WorkItemState.RUNNING}:
                continue
            lease = item.lease or {}
            attempt_id = lease.get("attempt_id")
            expires_at = lease.get("expires_at")
            expired = bool(expires_at) and _parse_datetime(str(expires_at)) <= _utc_now()
            orphaned = active is not None and attempt_id not in active
            if not expired and not orphaned:
                continue
            item.state = WorkItemState.QUEUED
            item.lease = {}
            item.lane = None
            item.scheduled_after = None
            self.store.save_item(item)
            reclaimed.append(item.id)
        return reclaimed

    def reap_completed_phase_items(self) -> list[str]:
        """Cancel QUEUED items whose target phase is already SUCCEEDED in the spec's
        phase-log. Such duplicates arise from the interrupt -> resume-same-session
        path: the resume completes and advances the phase, but the original item is
        re-queued (attempt+1) instead of being recognized as done — so the lane burns
        cycles re-running a finished phase (and can regress current_phase). Reaping
        only QUEUED items is safe: a live exec holds DISPATCHED/RUNNING and is left to
        reclaim_stale_leases (which requeues dead-lease items, after which this catches
        them). Idempotent, lane-agnostic, self-healing."""
        reaped: list[str] = []
        succeeded_cache: dict[str, set[str]] = {}
        affected: dict[str, dict] = {}
        for item in self.store.reconstruct().items.values():
            if item.state != WorkItemState.QUEUED:
                continue
            ref = str((item.task_ref or {}).get("runner_task_ref", ""))
            if "/runs/phase-" not in ref:
                continue
            spec_id = (item.task_ref or {}).get("spec_id") or ref.split("/specs/")[-1].split("/")[0]
            phase = ref.split("/runs/phase-")[-1].rsplit(".yaml", 1)[0]
            if spec_id not in succeeded_cache:
                succeeded_cache[spec_id] = self._succeeded_phases(spec_id)
            if phase not in succeeded_cache[spec_id]:
                continue
            item.state = WorkItemState.CANCELLED
            item.lease = {}
            item.scheduled_after = None
            item.task_ref["last_error"] = "reaped: phase already SUCCEEDED (superseded duplicate)"
            self.store.save_item(item)
            reaped.append(item.id)
            # Record the strand so the resume pass (below) can self-heal it. Carry the
            # original author lane across a review detour; the review item's own lane
            # is deliberately independent and must not pin the resumed author phase.
            ctx = affected.setdefault(spec_id, {"lane": None, "priority": 0})
            if not ctx["lane"]:
                if normalize_phase(phase) in REVIEW_LANE_PHASES:
                    ctx["lane"] = item.task_ref.get("author_lane")
                elif item.lane:
                    ctx["lane"] = item.lane
            ctx["priority"] = max(ctx["priority"], item.priority or 0)
        # Self-heal: re-enqueue the live phase for any spec the reaping left with zero
        # non-terminal items, so a reaped duplicate can't strand a spec at 0 work.
        self._resume_stalled_specs(affected)
        return reaped

    def _succeeded_phases(self, spec_id: str) -> set[str]:
        """Phases recorded SUCCEEDED in a spec's phase-log (the completion ledger)."""
        log = _safe_yaml(runtime_dir(self.project_dir) / "specs" / spec_id / "phase-log.yaml") or {}
        return {
            str(entry.get("phase"))
            for entry in (log.get("phases") or [])
            if isinstance(entry, dict) and entry.get("outcome") == "SUCCEEDED"
        }

    def _has_any_active_item(self, spec_id: str) -> bool:
        """True if the spec has ANY non-terminal queue item. The precise stranding guard
        for the resume path: self-heal ONLY a spec with zero active work, so a phase
        legitimately in flight is never duplicated. Reads the LIVE store (never a cached
        snapshot) so a concurrent enqueue — the lock-free worker-thread
        _advance_after_success, or a `approve` — is seen and the resume stands down."""
        return any(
            it.state not in TERMINAL_STATES and self._spec_id_for(it) == spec_id
            for it in self.store.reconstruct().items.values()
        )

    def _resume_phase(self, spec_id: str) -> str | None:
        """The spec's live (resume) phase — the phase a fresh dispatch would run — or
        None when the spec is terminal (verified/archived) or unresolvable.

        Mirrors detect_phase's trust of spec.yaml.current_phase (what a lane resolves
        the phase from), but cross-checks it against the phase-log completion ledger so
        a stale/regressed current_phase pointing at an already-SUCCEEDED phase advances
        forward (via next_phase) instead of re-running it. next_phase consults both the
        active and legacy orders, so this is correct for 4-phase, review-augmented, and
        legacy specs alike. None => nothing to resume (don't strand-heal)."""
        spec = _safe_yaml(runtime_dir(self.project_dir) / "specs" / spec_id / "spec.yaml") or {}
        order = phase_order_for_count(review_count_for_spec(spec, self.pipeline))
        if str(spec.get("status", "")).strip().lower() in ("verified", "archived"):
            return None  # terminal — the pipeline completed; nothing to resume
        succeeded = {normalize_phase(p) for p in self._succeeded_phases(spec_id)}
        succeeded.discard(None)
        current = normalize_phase(spec.get("current_phase"))
        # Walk current_phase forward over any already-SUCCEEDED phases: the first
        # not-yet-done phase from current_phase on is the live phase. None => current
        # walked off the end of the order, i.e. the spec is terminal.
        phase = current
        while phase and phase in succeeded:
            phase = next_phase(phase, order=order)
        if phase:
            return phase
        if current:
            return None  # current_phase advanced past the last phase -> terminal
        # No current_phase recorded (pathological): first not-SUCCEEDED phase in order.
        return next(
            (p for p in order if p not in succeeded),
            None,
        )

    def _resume_stalled_specs(self, affected: dict[str, dict]) -> list[str]:
        """Close the stranding edge: a spec whose only non-terminal item was just reaped
        is left with 0 active items and would stall forever (nothing dispatchable, no
        success to trigger _advance_after_success). For each reaped spec that is NOT
        terminal, has NO active item, and has NO plan-approval gate pending, enqueue its
        live (resume) phase once — same routing/priority _advance_after_success uses.

        Each guard reads the LIVE store at decision time (not a one-shot snapshot): the
        worker-thread _advance_after_success and the lock-free `approve` are concurrent
        enqueue producers for the same spec, so the active-item check runs LAST, right
        before the enqueue, to shrink that TOCTOU to a near-zero window. If it still loses
        the race the worst case is a redundant QUEUED item the next reap self-heals once
        the live phase SUCCEEDS — never a stall, a duplicate-driven gate bypass, or a
        dropped approval. Idempotent across reap passes (a re-run reaps nothing, so
        `affected` is empty and this is a no-op)."""
        enqueued: list[str] = []
        for spec_id, ctx in affected.items():
            # A pending plan-approval gate IS the spec's active state: the human `approve`
            # enqueues the held phase, and lane_common folds any un-approved post-gate
            # dispatch back to plan — so an un-gated resume here would churn and drop the
            # pending approval. Checked on the live FS first so an `approve` that just
            # consumed the gate is seen. Leave gated specs to the gate.
            if (self.store.queue_dir / "gates" / f"{spec_id}.json").exists():
                continue
            resume = self._resume_phase(spec_id)
            if not resume:
                continue  # verified/terminal or unresolvable — leave it
            # Live re-read LAST, just before enqueue: skip if any active item now exists
            # (incl. one a concurrent _advance_after_success / approve just enqueued).
            if self._has_any_active_item(spec_id):
                continue
            author_lane = self._author_lane(ctx.get("lane"))
            routed = self._route_phase(resume, author_lane)
            self._enqueue_phase(
                spec_id,
                resume,
                routed,
                int(ctx.get("priority") or 0),
                author_lane=author_lane if normalize_phase(resume) in REVIEW_LANE_PHASES else None,
            )
            enqueued.append(spec_id)
        return enqueued

    def wait_for_attempts(self, *, timeout: float = 1.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._attempt_threads_lock:
                threads = list(self._attempt_threads)
            if not threads:
                return True
            for thread in threads:
                remaining = max(0.0, deadline - time.monotonic())
                thread.join(timeout=min(0.05, remaining))
        with self._attempt_threads_lock:
            return not self._attempt_threads

    def _dispatchable_items(self) -> list[WorkItem]:
        now = _utc_now()
        gating = bool(self.pipeline.get("dependency_gating"))
        items = []
        for item in self.store.reconstruct().items.values():
            # M4a: the BLOCKED_DEP -> (QUEUED | BLOCKED_HUMAN) recheck runs
            # REGARDLESS of the gating flag — mutates `item` in place, so the
            # QUEUED check right below sees the update within THIS same pass —
            # so flipping dependency_gating OFF after items were held never
            # strands them (they always drain/recover). Only the QUEUED ->
            # BLOCKED_DEP hold stays gated behind `gating`: flag ON = hold +
            # recheck; flag OFF = recheck-only (drains, never newly holds). A
            # queue that never contained a BLOCKED_DEP item makes this loop a
            # no-op either way, so flag-off stays byte-identical to pre-R4
            # behavior.
            if item.state == WorkItemState.BLOCKED_DEP:
                spec_id = self._spec_id_for(item)
                if spec_id:
                    self._recheck_dependency_hold(item, spec_id)
            elif gating and item.state == WorkItemState.QUEUED:
                spec_id = self._spec_id_for(item)
                if spec_id:
                    self._maybe_hold_for_dependencies(item, spec_id)
            if item.state != WorkItemState.QUEUED:
                continue
            if item.scheduled_after and _parse_datetime(item.scheduled_after) > now:
                continue
            items.append(item)
        return sorted(items, key=lambda item: (-item.priority, item.created_at, item.id))

    # --- R4 dependency-aware scheduling ------------------------------------
    _DEP_SATISFIED_STATUSES = ("verified", "archived")
    _DEP_TERMINAL_STATUSES = ("failed", "abandoned", "cancelled", "blocked_human")

    def _dep_target_status(self, dep_spec_id: str) -> str:
        # H3: resolve archive-aware — an archived dep's dir has MOVED to
        # specs/archive/<YYYY-MM-DD->-<id>/, so reading the hardcoded canonical
        # path would read "" (unmet) forever even though `archived` is a
        # satisfied status. Shares the same resolver phase completion validation
        # uses, so a dependency and a normal spec-lookup never disagree about
        # where an archived spec's spec.yaml lives.
        specs_dir = runtime_dir(self.project_dir) / "specs"
        spec_dir = _resolve_spec_dir(specs_dir, dep_spec_id)
        spec = _safe_yaml(spec_dir / "spec.yaml")
        if not isinstance(spec, dict):
            return ""
        return str(spec.get("status", "")).strip().lower()

    def _dep_stalled(self, dep_spec_id: str) -> bool:
        """True if the dependency spec's own pipeline has hit a genuine, durable
        dead stop: its (archive-aware) spec.yaml records a terminal-ish status,
        OR its MOST-RECENT work item (by created_at, tie-break by id) is
        FAILED/BLOCKED_HUMAN — so a dependent waiting on it would otherwise wait
        forever.

        H2: only the latest item is consulted, never the full history
        `store.reconstruct()` keeps forever (terminal items are never pruned) —
        a dep spec with ANY historical FAILED item that later healed (or hit a
        momentary zero-active window, e.g. a pending plan-gate or the unlocked
        gap between a worker saving SUCCEEDED and enqueueing the next phase)
        must not trip a permanent false escalation.

        A pending plan-approval gate is treated as ACTIVE (not stalled) — a
        spec awaiting human approval is alive, not dead.
        """
        if self._dep_target_status(dep_spec_id) in self._DEP_TERMINAL_STATUSES:
            return True
        if (self.store.queue_dir / "gates" / f"{dep_spec_id}.json").exists():
            return False
        related = [
            it for it in self.store.reconstruct().items.values()
            if self._spec_id_for(it) == dep_spec_id
        ]
        if not related:
            return False
        latest = max(related, key=lambda it: (it.created_at, it.id))
        return latest.state in (WorkItemState.FAILED, WorkItemState.BLOCKED_HUMAN)

    @staticmethod
    def _dep_target_safe(target: str) -> bool:
        """L2 path-traversal guard: a dependency `target` is only ever safe to
        build a filesystem path from (`.builder/specs/<target>/...`) if it is
        non-empty, is not `.`/`..`, and contains no path separator. Dep spec ids
        flow in unsanitized from `dependencies.yaml` — never let one escape the
        specs directory."""
        return bool(target) and target not in (".", "..") and "/" not in target and "\\" not in target

    @staticmethod
    def _cross_repo_deps_mode() -> str:
        """Cross-repo dependency gating, staged via BUILDER_CROSS_REPO_DEPS: 'off' (DEFAULT) =
        a `<alias>/<spec-id>` dep is informational (skipped, byte-identical to before this feature);
        'enforce' = it gates dispatch on the readiness ladder (default rung `merged`)."""
        return (os.environ.get("BUILDER_CROSS_REPO_DEPS", "off") or "off").strip().lower()

    def _cross_repo_registry(self):
        """The planning Registry, built once. Globs product.yaml across the projects root and maps
        a repo alias to its root with realpath containment — a cross-repo ref is NEVER turned into a
        path by string concatenation (that guard lives entirely in planning.Registry)."""
        if getattr(self, "_xrepo_registry_cache", None) is None:
            import planning
            projects_root = planning.default_projects_root(self.project_dir)
            self._xrepo_registry_cache = planning.Registry(projects_root, self.project_dir)
        return self._xrepo_registry_cache

    def _cross_repo_dep_state(self, dep: dict, target: str, *, git_runner=None) -> tuple[bool, bool]:
        """(satisfied, stalled) for a cross-repo `<alias>/<spec-id>` dep. Resolution is path-safe
        (planning.Registry); readiness is observed by the ladder (readiness.evaluate), gated on the
        dep's `ready_at` (default `merged`). A malformed ref or an unknown alias / dangling target
        is unmet AND stalled (a human must fix the ref); a resolved-but-not-yet-ready dep is unmet
        but NOT stalled (it is waiting on a merge, which is normal). `git_runner` is injectable for
        tests; production uses readiness's default subprocess git."""
        import planning
        import readiness
        ref, err = planning.parse_spec_ref(target)
        if err or ref is None:
            return False, True
        registry = self._cross_repo_registry()
        spec_dir, rerr = registry.spec_dir(ref)
        if rerr or spec_dir is None or not spec_dir.is_dir():
            return False, True
        repo_root, _ = registry.resolve(ref)
        ready_at = (str(dep.get("ready_at", "merged")).strip().lower() or "merged")
        if ready_at not in ("verified", "delivered", "merged", "available"):
            # A misconfigured `ready_at` (typo, junk) must not silently pick a weaker gate. Surface
            # it: unmet AND stalled -> BLOCKED_HUMAN, so a human fixes the ref rather than a typo
            # quietly downgrading `available` to `merged`.
            return False, True
        package = dep.get("package") if isinstance(dep.get("package"), dict) else None
        kwargs = {"required": ready_at, "package": package}
        if git_runner is not None:
            kwargs["git_runner"] = git_runner
        result = readiness.evaluate(ref.canonical, spec_dir, repo_root, **kwargs)
        return result.satisfies(ready_at), False

    def _unmet_dependencies(self, spec_id: str) -> tuple[list[str], list[str]]:
        """(unmet, stalled) sibling spec ids for `spec_id`'s `dependencies.yaml`
        (required deps only — `kind: contextual`/`optional` never gate dispatch).
        unmet = deps not yet verified/archived; stalled = the subset of unmet that
        look permanently dead. Missing/malformed dependencies.yaml, an absent
        `dependencies` key, or a non-list value all fail OPEN: ([], []) — no deps
        recorded means dispatchable, same as today."""
        deps_path = runtime_dir(self.project_dir) / "specs" / str(spec_id) / "dependencies.yaml"
        data = _safe_yaml(deps_path)
        if not isinstance(data, dict):
            return [], []
        deps = data.get("dependencies")
        if not isinstance(deps, list):
            return [], []
        unmet: list[str] = []
        stalled: list[str] = []
        for dep in deps:
            if not isinstance(dep, dict):
                continue
            kind = str(dep.get("kind", "required")).strip().lower()
            if kind in ("contextual", "optional"):
                continue  # informational only — never blocks dispatch
            target = str(dep.get("spec", "")).strip()
            if "/" in target or "\\" in target:
                # A cross-repo `<alias>/<spec-id>` ref (or junk with a separator). The same-repo
                # path guard below would reject it; instead resolve it through the planning
                # registry and gate on the readiness ladder. OFF by default -> informational,
                # exactly as before this feature (the ref was silently skipped).
                if self._cross_repo_deps_mode() != "enforce":
                    continue
                satisfied, is_stalled = self._cross_repo_dep_state(dep, target)
                if satisfied:
                    continue
                unmet.append(target)
                if is_stalled:
                    stalled.append(target)
                continue
            if not self._dep_target_safe(target):
                continue  # L2: empty, '.'/'..', or path-separator -> never build a path from it
            if target == str(spec_id):
                continue  # M5: a self-dependency can never be satisfied -> runtime deadlock guard
            if self._dep_target_status(target) in self._DEP_SATISFIED_STATUSES:
                continue
            unmet.append(target)
            if self._dep_stalled(target):
                stalled.append(target)
        return unmet, stalled

    def _maybe_hold_for_dependencies(self, item: WorkItem, spec_id: str) -> None:
        """QUEUED -> BLOCKED_DEP when `spec_id` has an unmet required dependency."""
        unmet, _stalled = self._unmet_dependencies(spec_id)
        if not unmet:
            return  # no deps, or all satisfied -> dispatchable as normal
        item.state = WorkItemState.BLOCKED_DEP
        item.lease = {}
        self.store.save_item(item)

    def _recheck_dependency_hold(self, item: WorkItem, spec_id: str) -> None:
        """Re-evaluate a previously-held BLOCKED_DEP item: auto-recover to QUEUED
        once every dependency verifies, or cascade to BLOCKED_HUMAN (+ notify) once
        a dependency has permanently stalled — never wait forever on a dep that
        will never verify."""
        unmet, stalled = self._unmet_dependencies(spec_id)
        if not unmet:
            item.state = WorkItemState.QUEUED
            self.store.save_item(item)
            return
        if stalled:
            item.state = WorkItemState.BLOCKED_HUMAN
            item.lease = {}
            self.store.save_item(item)
            synthetic = types.SimpleNamespace(
                result_type=DispatchResultType.HUMAN_BLOCK,
                metadata={
                    "spec_id": spec_id,
                    "phase": "?",
                    "reason": f"dependency will not verify (terminal): {', '.join(stalled)}",
                },
            )
            self._notify_blocked(item, synthetic, {})
            return
        # still legitimately pending -> remain BLOCKED_DEP (no-op)

    def _eligible_lanes(self) -> list[str]:
        snapshot = self.store.reconstruct()
        inflight: dict[str, int] = {}
        for item in snapshot.items.values():
            if item.state in {WorkItemState.DISPATCHED, WorkItemState.RUNNING} and item.lane:
                inflight[item.lane] = inflight.get(item.lane, 0) + 1
        eligible: list[str] = []
        for lane_name, lane_config in self.config.lanes.items():
            if not lane_available(snapshot.lanes.get(lane_name)):
                continue
            if inflight.get(lane_name, 0) >= lane_config.max_concurrency:
                continue
            eligible.append(lane_name)
        return eligible

    def _route_item(self, item: WorkItem) -> str | None:
        try:
            return resolve_lane(item, self.config, self._eligible_lanes()).lane_name
        except UnknownLaneHintError as exc:
            item.state = WorkItemState.FAILED
            item.lease = {}
            item.task_ref["last_error"] = str(exc)
            self.store.save_item(item)
            self._note_spec_outcome(success=False)  # R6: this terminal FAILED counts too
            return None

    def _execute_item(self, item: WorkItem, lane_name: str) -> None:
        attempt_id = f"attempt-{uuid4().hex}"
        item.attempt += 1
        item.lane = lane_name
        item.lease = {
            "id": f"lease-{uuid4().hex}",
            "attempt_id": attempt_id,
            "lane": lane_name,
            "owner": self.owner_id,
            "expires_at": (_utc_now() + timedelta(seconds=self.lease_seconds)).isoformat().replace("+00:00", "Z"),
        }
        item.state = WorkItemState.DISPATCHED
        self.store.save_item(item)

        spec_id = self._spec_id_for(item)
        # Sync-era specs require isolation even when the legacy global flag is off:
        # their plan-owned delta activates before implement and the later sync gate
        # must prove an uncontaminated baseline-to-verify manifest.
        workspace_root = str(self.project_dir)
        sync_isolated = bool(
            spec_id and (runtime_dir(self.project_dir) / "specs" / spec_id / "ssot-delta.yaml").is_file()
        )
        if (self.pipeline.get("worktree_isolation") or sync_isolated) and spec_id:
            workspace_root = str(self._ensure_worktree(spec_id))

        attempt_context = {
            "attempt_id": attempt_id,
            "work_id": item.id,
            "log_path": f"queue/attempts/{attempt_id}.log",
            "workspace_root": workspace_root,
            # M6: the canonical repo identity, ALWAYS the main project_dir — set
            # unconditionally (isolated or not) so `lane_common.maybe_env_up`
            # derives the env-up profile name + --projects-dir from the real
            # repo, never a spec-id-named worktree path. When not isolated this
            # equals workspace_root, so non-isolated argv is unchanged.
            "control_root": str(self.project_dir),
            "queue_root": str(self.store.root),
            "auto_env_up": bool(self.pipeline.get("auto_env_up", True)),
            "plan_gate": self._effective_plan_gate(spec_id),
        }
        self.store.record_attempt(
            item.id,
            attempt_id=attempt_id,
            lane=lane_name,
            metadata={
                "work_id": item.id,
                "lane": lane_name,
                "log_path": attempt_context["log_path"],
                "started_at": _iso_now(),
            },
        )

        item.state = WorkItemState.RUNNING
        self.store.save_item(item)
        thread = threading.Thread(
            target=self._complete_attempt,
            args=(item.id, attempt_id, lane_name, dict(item.task_ref), attempt_context),
            daemon=True,
        )
        with self._attempt_threads_lock:
            self._attempt_threads.add(thread)
        thread.start()

    # --- R5 per-spec worktree isolation (Model A) ---------------------------
    #
    # Model A: the git worktree isolates SOURCE only. The Builder control
    # plane (`.builder/specs/<id>/` — spec.yaml, phase-log.yaml, handoff.yaml,
    # traceability.yaml) stays canonical in the MAIN tree (`self.project_dir`).
    # `_ensure_worktree` redirects the worktree's copy of the spec's control dir
    # to a symlink pointing at the shared main copy (`_redirect_spec_control_dir`
    # below), so the agent's cwd-relative writes inside the worktree AND the
    # scheduler's direct reads/writes against main both resolve to the SAME
    # files — no split-brain, and an uncommitted (freshly-drafted) spec is never
    # DOA in a fresh checkout that never had it committed.
    def _worktree_path(self, spec_id: str) -> Path:
        """Stable, known root for a spec's isolated worktree — nested under this
        project's OWN `.builder/` (never a sibling of other repos under the
        shared projects root), so `lane_common.py`'s env-up arg derivation
        (`project_dir.name` / `.parent`) always reconstructs to this exact,
        just-created, existing path — it can never "not stay valid"."""
        return runtime_dir(self.project_dir) / "worktrees" / str(spec_id)

    def _fallback_marker(self, spec_id: str) -> Path:
        """M1 sticky-fallback marker path (lives in MAIN, never in a worktree)."""
        return runtime_dir(self.project_dir) / "worktrees" / f".fallback-{spec_id}"

    def _mark_fallback(self, spec_id: str) -> None:
        """M1: latch that this spec has degraded to the shared main dir at least
        once. Written on ANY provisioning failure/degrade so every LATER phase
        (and delivery, via `_delivery_cwd`) keeps running in main too — a phase
        that already ran (and wrote uncommitted work) directly in project_dir
        must never be followed by a phase running in a freshly-created, empty
        worktree that lacks that work. Best-effort: a failed write here just
        means the next call re-attempts worktree provisioning (no worse than
        pre-M1 behavior)."""
        try:
            marker = self._fallback_marker(spec_id)
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(_iso_now(), encoding="utf-8")
            # Durable: the sticky marker above is cleared on termination so the spec can be
            # re-dispatched; this one is not, because a LATER dispatch still needs to know a
            # phase once ran in main and may have left uncommitted work there.
            self._degraded_marker(spec_id).write_text(_iso_now(), encoding="utf-8")
        except OSError:
            pass

    def _degraded_marker(self, spec_id: str) -> Path:
        """Breadcrumb: this spec ran at least one phase in MAIN rather than a worktree.

        Outlives `_fallback_marker`, which is deliberately cleared on termination so a resolved
        spec can be re-dispatched. The fact that a degrade HAPPENED is what a later dispatch
        needs in order to know that uncommitted work may be stranded in main."""
        return runtime_dir(self.project_dir) / "worktrees" / f".degraded-{spec_id}"

    def _degraded_before(self, spec_id: str) -> bool:
        try:
            return self._degraded_marker(spec_id).exists()
        except OSError:
            return True  # unreadable -> assume the worst

    def _main_has_uncommitted_work(self) -> bool:
        """Does the MAIN checkout carry uncommitted changes right now?

        This is the precondition for the one way this scheduler could issue a host
        verdict about the wrong code. No phase commits before delivery, so a phase
        that ran in main leaves its work uncommitted THERE. If a spec degraded to
        main, terminated, had its sticky marker cleared, and is then re-dispatched,
        a fresh empty worktree would not contain that work — and verify would pass
        or fail on a tree that never had the implementation in it.

        Fails CLOSED in both directions: a dirty main means no fresh worktree (run
        in main, which is the supported degraded mode), and an unreadable/erroring
        git status is treated as dirty rather than assumed clean. Costing a spec
        its worktree is recoverable; a verdict about the wrong tree is not.
        """
        try:
            result = self._worktree_runner.run(
                ["git", "status", "--porcelain"], cwd=str(self.project_dir))
        except Exception:  # noqa: BLE001 - unreadable status is treated as dirty
            return True
        if result.returncode != 0:
            return True
        return bool((result.stdout or "").strip())

    def _clear_fallback(self, spec_id: str) -> None:
        try:
            self._fallback_marker(spec_id).unlink(missing_ok=True)
        except OSError:
            pass

    def _clear_fallback_for_item(self, item: WorkItem) -> None:
        """M-C(b): best-effort clear of the M1 sticky-fallback marker when a
        spec's pipeline terminates WITHOUT delivery (FAILED / BLOCKED_HUMAN
        here; `_advance_after_success` clears it separately when a pipeline
        completes with delivery disabled/not reached). Before this fix,
        `_clear_fallback` ran ONLY in `_cleanup_worktree` (the successful-
        delivery path), so a degraded spec stayed pinned to running in main
        forever — even after a human resolved it and re-queued a fresh
        attempt. Swallows everything; must never break terminal handling.

        Clearing the marker is what lets a resolved spec be re-dispatched into a
        fresh worktree. That reopened a hole — an earlier phase's UNCOMMITTED work
        can live only in main, and a later phase in a fresh, empty worktree would
        then verify a tree without it, i.e. issue a host verdict about the wrong
        code. `_ensure_worktree` now fails closed on exactly that precondition: for a spec
        that has degraded before (`_degraded_marker`, which this clear does NOT
        remove) it refuses to provision a fresh worktree while main is dirty, and
        degrades to main instead.

        Residual, still not fixed here: `_cleanup_worktree`'s H-3 refusal can leave
        pre-degrade work behind if cleanup later removes the worktree on a
        subsequent successful delivery. That one strands work; it does not
        misattribute a verdict."""
        try:
            spec_id = self._spec_id_for(item)
            if spec_id:
                self._clear_fallback(spec_id)
        except Exception:  # noqa: BLE001 - terminal handling must never break on this
            pass

    def _redirect_spec_control_dir(self, worktree_path: Path, spec_id: str) -> bool:
        """Model A core mechanism: replace `<worktree>/.builder/specs/<spec_id>`
        with a symlink to the canonical `<main>/.builder/specs/<spec_id>` (an
        ABSOLUTE target, so it resolves regardless of cwd). Idempotent: a symlink
        already pointing at the right place is left untouched (later phases reuse
        the same worktree without re-linking). A committed non-symlink copy at
        the path (e.g. leftover from a pre-Model-A delivery that `git add -A`'d
        control files into the branch) is removed first — the main copy is
        authoritative, never the checkout's.

        Returns True iff the path is a correct symlink afterward; False on any
        OSError (caller treats that the same as a worktree-provisioning failure
        and falls back to the sticky main dir, rather than let a phase run
        against a still-split control dir).

        L-B: the MAIN spec dir is created (`mkdir(parents=True, exist_ok=True)`)
        BEFORE the symlink is made, so the symlink is never dangling even when
        nothing has written the main spec dir yet — the agent (writing through
        the worktree path) can self-heal a missing spec dir exactly as it could
        pre-Model-A, when there was no symlink indirection at all.

        Git-status note (documented, not "fixed"): this rmtree+symlink shows
        `.builder/specs/<id>` as a delete+typechange in the WORKTREE's `git
        status`. `lane_common._git_source_paths` filters `.builder/` out of the
        source diff, so R2 source-diff detection is unaffected; scoped delivery
        (`delivery._scoped_add_paths`) also defensively drops any path under this
        prefix, so it is never explicitly staged. A non-scoped `git add -A`
        fallback (e.g. no traceability/handoff paths found) COULD still sweep the
        symlink itself into a commit in the rare case above — acceptable (Model A
        intentionally treats control state as living in main regardless of the
        source PR), but worth knowing about operationally.
        """
        main_spec_dir = runtime_dir(self.project_dir) / "specs" / spec_id
        wt_spec_dir = runtime_dir(worktree_path) / "specs" / spec_id
        try:
            main_spec_dir.mkdir(parents=True, exist_ok=True)  # L-B: never a dangling symlink
            wt_spec_dir.parent.mkdir(parents=True, exist_ok=True)
            if wt_spec_dir.is_symlink():
                if wt_spec_dir.resolve() == main_spec_dir.resolve():
                    return True  # already correctly redirected -> idempotent reuse
                wt_spec_dir.unlink()
            elif wt_spec_dir.is_dir():
                shutil.rmtree(wt_spec_dir)
            elif wt_spec_dir.exists():
                wt_spec_dir.unlink()
            wt_spec_dir.symlink_to(main_spec_dir)
            return True
        except OSError:
            return False

    def _ensure_worktree(self, spec_id: str) -> Path:
        """Return a per-spec git worktree checked out on the spec's delivery branch
        with its control dir redirected to main (Model A), creating it on first
        use and REUSING it on every later phase of the SAME spec. Deliberately NOT
        torn down between phases: today no phase commits between
        spec->plan->implement->verify (delivery is the FIRST commit in the whole
        pipeline), so destroying the worktree mid-pipeline would discard the
        implement phase's uncommitted edits before verify — or delivery — ever saw
        them.

        M1 sticky fallback: ANY provisioning failure/degrade (creation failure,
        unexpected non-worktree cruft at the path, or a failed control-dir
        redirect) marks this spec via `_mark_fallback` and falls back to the
        shared project_dir — and STAYS there for every later phase (checked
        FIRST, above) and for delivery (`_delivery_cwd` honors the same marker),
        so the whole pipeline stays consistent about where this spec's phases
        actually ran. Cleared only by `_cleanup_worktree` after a successful
        delivery. Never crashes the scheduler thread. NEVER `git worktree prune`
        (dual-mount trap) — only ever an explicit `add`/`remove` of this exact
        path.
        """
        if self._fallback_marker(spec_id).exists():
            return self.project_dir  # sticky: a prior phase already degraded
        path = self._worktree_path(spec_id)
        if (path / ".git").exists():
            # already provisioned (this or an earlier phase) -> reuse, but make
            # sure the control-dir redirect is (still) in place every time.
            if self._redirect_spec_control_dir(path, spec_id):
                return path
            self._mark_fallback(spec_id)
            return self.project_dir
        if path.exists():
            # Unexpected non-worktree content already at the path — do not touch
            # it destructively; degrade to the shared root (sticky).
            self._mark_fallback(spec_id)
            return self.project_dir
        # FAIL CLOSED before creating a FRESH worktree, but ONLY for a spec that has degraded
        # to main before. Clearing the sticky marker on termination is what lets a resolved
        # spec be re-dispatched; the cost is that an earlier phase's UNCOMMITTED work may live
        # only in main, and a fresh empty worktree would not contain it -- verify would then
        # report on a tree that never held the implementation. Both conditions are required:
        # a spec that never degraded has nothing stranded, and a clean main has nothing to
        # strand. Running in main is the supported degraded mode; it costs isolation, not work.
        if self._degraded_before(spec_id) and self._main_has_uncommitted_work():
            self._mark_fallback(spec_id)
            return self.project_dir
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            from _dispatch_runtime.delivery import _default_base, branch_name_for

            deliver_cfg = self.pipeline.get("deliver") or {}
            branch = branch_name_for(spec_id, deliver_cfg)
            branch_exists = self._worktree_runner.run(
                ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
                str(self.project_dir),
            ).returncode == 0
            if branch_exists:
                # M-A: a leftover delivery branch (cleanup removes the worktree
                # on successful delivery but NOT the branch) must not be
                # checked out at its stale tip — a plain `add <path> <branch>`
                # would re-carry already-merged commits into the next PR.
                # Force the branch back to the delivery BASE (derived the same
                # way `deliver()` itself derives it) with `-B`, exactly as a
                # fresh worktree would start. If `-B` can't apply (e.g. the
                # branch is checked out elsewhere), this falls through to the
                # SAME failure handling below as any other provisioning
                # failure (mark_fallback + degrade to main) — no separate
                # fallback path needed.
                base = deliver_cfg.get("base") or _default_base(self._worktree_runner, str(self.project_dir))
                result = self._worktree_runner.run(
                    ["git", "worktree", "add", "-B", branch, str(path), base], str(self.project_dir)
                )
            else:
                result = self._worktree_runner.run(
                    ["git", "worktree", "add", "-b", branch, str(path)], str(self.project_dir)
                )
            if result.returncode != 0 or not (path / ".git").exists():
                self._mark_fallback(spec_id)
                return self.project_dir  # best-effort fallback — never crash dispatch
            if not self._redirect_spec_control_dir(path, spec_id):
                self._mark_fallback(spec_id)
                return self.project_dir
            return path
        except Exception:  # noqa: BLE001 - worktree provisioning must never crash dispatch
            self._mark_fallback(spec_id)
            return self.project_dir

    def _delivery_cwd(self, spec_id: str) -> Path:
        """The directory `_deliver` should run in: the spec's isolated worktree if
        one was actually provisioned (and never degraded — the M1 sticky marker),
        else the shared project_dir (matches `_ensure_worktree`'s own fallback, so
        delivery never targets a directory that doesn't exist, or a worktree whose
        control dir isn't correctly redirected)."""
        if self._fallback_marker(spec_id).exists():
            return self.project_dir
        path = self._worktree_path(spec_id)
        if (path / ".git").exists():
            return path
        return self.project_dir

    def _dirty_worktree_marker(self, spec_id: str) -> Path:
        """H-3: marker recording that `_cleanup_worktree` REFUSED to remove this
        spec's worktree because it still carried unrecorded (non-`.builder`)
        source changes — left for forensics/recovery, mirroring `_fallback_marker`'s
        own style (a plain timestamped file living in MAIN, never in a worktree)."""
        return runtime_dir(self.project_dir) / "worktrees" / f".retained-{spec_id}"

    def _log_worktree_retained(self, spec_id: str, residual: list[str]) -> None:
        """H-3: best-effort forensic note for the case above. Never raises —
        a failed write here just means the worktree is STILL left in place
        (the safe default), only without a paper trail explaining why."""
        try:
            marker = self._dirty_worktree_marker(spec_id)
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(
                f"{_iso_now()} cleanup refused: unrecorded (non-.builder) changes remain\n"
                + "\n".join(residual),
                encoding="utf-8",
            )
        except OSError:
            pass

    def _cleanup_worktree(self, spec_id: str) -> None:
        """Best-effort removal of a per-spec worktree — called ONLY after a
        SUCCESSFUL delivery (content is safely committed + pushed upstream by
        then, so the local worktree copy is redundant). Deliberately NOT called on
        FAILED/BLOCKED_HUMAN/CANCELLED or delivery-disabled completion: with no
        delivery, the worktree may be the ONLY copy of the implemented code, and on
        a failure path a human may need it for forensics — auto-deleting either
        would risk real data loss for a cosmetic disk-hygiene win. A leftover
        worktree must never crash the scheduler; NEVER `git worktree prune`.

        H-3: scoped delivery only stages the traceability/handoff-listed paths —
        an agent-authored file omitted from those lists is committed NOWHERE, yet
        `res.ok` is True (the listed paths delivered fine) and this method would
        otherwise force-remove the only copy of that extra file. Before removing,
        `git status --porcelain` the worktree and drop every `.builder/` line
        (the EXPECTED symlink/deletion noise from Model A's control-dir redirect —
        see `_redirect_spec_control_dir`'s docstring). If any OTHER entry remains
        (or `git status` itself fails — an anomaly, treated the same way out of
        caution), REFUSE to remove the worktree: leave it in place for recovery,
        and record why via `_log_worktree_retained`. Only a source-clean tree is
        ever force-removed — `--force` is still required even then, because the
        `.builder` symlink itself keeps the working tree non-empty.

        Also clears the M1 sticky-fallback marker (if any): a spec that
        successfully delivered is done with this pipeline run, so a future
        (rare) re-dispatch of the same spec_id should re-attempt worktree
        provisioning fresh rather than staying pinned to main forever."""
        self._clear_fallback(spec_id)
        path = self._worktree_path(spec_id)
        if not path.exists():
            return
        try:
            status = self._worktree_runner.run(["git", "status", "--porcelain"], str(path))
            all_paths = _porcelain_residual_paths(status.stdout or "")
            residual = [
                p for p in all_paths
                if p not in RUNTIME_DIR_NAMES and not any(
                    p.replace("\\", "/").startswith(f"{name}/") for name in RUNTIME_DIR_NAMES
                )
            ]
            if status.returncode != 0 or residual:
                self._log_worktree_retained(
                    spec_id, residual or all_paths or [(status.stderr or "git status failed").strip()]
                )
                return  # refuse to force-delete unrecorded source work
            self._worktree_runner.run(["git", "worktree", "remove", "--force", str(path)], str(self.project_dir))
        except Exception:  # noqa: BLE001 - cleanup must never crash dispatch
            pass

    def _complete_attempt(
        self,
        work_id: str,
        attempt_id: str,
        lane_name: str,
        task_ref: dict[str, Any],
        attempt_context: dict[str, Any],
    ) -> None:
        try:
            _t0 = time.monotonic()
            # Dispatch shares the same per-spec mutation ownership as explicit sync/readmit.
            # A dispatch already selected before readmission acquired the lock finishes first;
            # one selected afterward waits without entering the mutable spec runtime.
            from contextlib import nullcontext
            from _sync.locking import spec_mutation_lock

            lock_item = self.store.get_item(work_id)
            lock_spec = self._spec_id_for(lock_item) if lock_item is not None else None
            lock_context = (
                spec_mutation_lock(self.project_dir, lock_spec, blocking=True, owner="dispatch")
                if lock_spec else nullcontext()
            )
            with lock_context:
                result = self.executor.execute(task_ref, lane_name, attempt_context)
            plan_wall_ms = int((time.monotonic() - _t0) * 1000)
            existing_attempt = self.store.reconstruct().attempts.get(attempt_id)
            attempt_metadata = dict(existing_attempt.metadata) if existing_attempt is not None else {}
            attempt_metadata.update(result.metadata)
            self.store.record_attempt(work_id, attempt_id=attempt_id, lane=lane_name, metadata=attempt_metadata)
            self._accumulate_spend(result.metadata, plan_wall_ms)  # R6 roadmap budget (tokens + wall)

            # Emit one memory_eval per planned spec (plan phase only). Best-effort:
            # a telemetry failure here must never break the dispatch loop (R2).
            self._emit_memory_eval(work_id, lane_name, result, plan_wall_ms)

            item = self.store.get_item(work_id)
            if item is None:
                return
            if result.result_type == DispatchResultType.SUCCESS:
                item.state = WorkItemState.SUCCEEDED
                item.lease = {}
                item.scheduled_after = None
                self.store.save_item(item)
                # R6 breaker reset happens on SPEC pipeline completion (in
                # _advance_after_success), NOT per phase — else an interleaved spec's
                # early phase success would reset the counter and the breaker never fires.
                self._advance_after_success(item, result.metadata.get("phase"))
                return
            if result.result_type == DispatchResultType.HUMAN_BLOCK:
                item.state = WorkItemState.BLOCKED_HUMAN
                item.lease = {}
                item.scheduled_after = None
                self.store.save_item(item)
                self._clear_fallback_for_item(item)  # M-C(b): don't stay pinned to main forever
                self._notify_blocked(item, result, attempt_context)
                return
            if result.result_type == DispatchResultType.RATE_LIMITED:
                # A2: a rate-limit is transient throttling, NOT a failure. Put the
                # lane on cooldown and re-QUEUE the item to wait the throttle out —
                # never FAILED, never consuming the retry budget meant for REAL
                # errors. The item is deferred until the lane's cooldown clears:
                # scheduled_after gates _dispatchable_items and _eligible_lanes
                # already excludes the cooled lane, so there is NO busy-loop.
                lane = open_lane_cooldown(self.store, lane_name, result, self.config.cooldown_policy)
                # Attempt-budget invariant: the counter is advanced at LEASE time
                # (_execute_item does attempt += 1), so the re-lease after this
                # re-queue WILL re-increment. Net that out here (-1) so a rate-limit
                # re-dispatch costs zero attempts; repeated rate-limits hold attempt
                # steady and can never drive the item to FAILED.
                item.attempt = max(0, item.attempt - 1)
                item.task_ref["rate_limit_count"] = int(item.task_ref.get("rate_limit_count") or 0) + 1
                item.state = WorkItemState.QUEUED
                item.lease = {}
                item.scheduled_after = lane.cooldown_until
                self._maybe_pin_gated_phase(item, result)
                self.store.save_item(item)
                # Best-effort debounced notify: after save_item so the current item
                # is counted in queued_on_lane (ensures count >= 1).
                queued_on_lane = sum(
                    1 for i in self.store.reconstruct().items.values()
                    if i.state == WorkItemState.QUEUED and i.lane == lane_name
                )
                spec_id = str((result.metadata or {}).get("spec_id") or "")
                self._notify_lane_cooled(lane_name, lane, queued_on_lane, spec_id, work_id)
                return
            if result.result_type == DispatchResultType.RETRYABLE_ERROR:
                # Persist the gated-phase pin BEFORE apply_failure_backoff, which reloads
                # the item from the store (it preserves task_ref, so the pin survives).
                if self._maybe_pin_gated_phase(item, result):
                    self.store.save_item(item)
                backed_off = apply_failure_backoff(self.store, item.id, result, self.config.retry_policy)
                if backed_off.state == WorkItemState.FAILED:
                    self._note_spec_outcome(success=False)  # R6: retries exhausted -> failure
                    self._clear_fallback_for_item(backed_off)  # M-C(b): don't stay pinned to main forever
                    self._notify_failed(backed_off, result, attempt_context)
                return

            item.state = WorkItemState.FAILED
            item.task_ref["last_error"] = result.result_type.value
            item.lease = {}
            item.scheduled_after = None
            self.store.save_item(item)
            self._note_spec_outcome(success=False)  # R6: terminal failure
            self._clear_fallback_for_item(item)  # M-C(b): don't stay pinned to main forever
            self._notify_failed(item, result, attempt_context)
        except Exception as exc:  # noqa: BLE001 - an attempt-thread exception must NEVER
            # die silently and strand a leased item (reclaim would re-dispatch it into the
            # same crash — a ~lease-length infinite loop, invisible to breaker + notifier).
            # Fail the item LOUD. Re-fetch: execute() may have raised before `item` bound.
            self._accumulate_spend({}, int((time.monotonic() - _t0) * 1000))  # R6: count the wall too
            try:
                failed = self.store.get_item(work_id)
                if failed is not None:
                    failed.state = WorkItemState.FAILED
                    failed.task_ref["last_error"] = f"attempt exception: {type(exc).__name__}: {exc}"
                    failed.lease = {}
                    failed.scheduled_after = None
                    self.store.save_item(failed)
                    self._note_spec_outcome(success=False)
                    self._clear_fallback_for_item(failed)  # M-C(b): don't stay pinned to main forever
                    synthetic = types.SimpleNamespace(
                        result_type=DispatchResultType.TERMINAL_ERROR,
                        metadata={"reason": f"attempt exception: {exc}"})
                    self._notify_failed(failed, synthetic, attempt_context)
            except Exception:  # noqa: BLE001 - failure handling must not re-raise in-thread
                pass
        finally:
            current = threading.current_thread()
            with self._attempt_threads_lock:
                self._attempt_threads.discard(current)

    def _emit_memory_eval(self, work_id: str, lane_name: str, result, plan_wall_ms: int) -> None:
        """Append one memory_eval for a terminal PLAN-phase result. Non-plan phases
        emit nothing (R2 WHERE clause). Best-effort: never raises (telemetry must
        not break the autonomous loop). memory_mode defaults to "off"; recall/
        decision counters default to 0 — S3 overwrites them later (no schema change).
        """
        try:
            from _telemetry.memory_eval import append_memory_eval, build_memory_eval

            metadata = result.metadata or {}
            # S3: when the lane's finalize_turn already emitted the (enriched)
            # memory_eval for this plan/verify turn, do not emit a duplicate here.
            if metadata.get("memory_eval_emitted"):
                return
            if normalize_phase(metadata.get("phase")) not in ("plan", "4-plan"):
                return
            if result.result_type == DispatchResultType.HUMAN_BLOCK:
                spec_outcome = "blocked"
            elif result.result_type == DispatchResultType.TERMINAL_ERROR:
                spec_outcome = "failed"
            else:
                spec_outcome = "unknown"
            record = build_memory_eval(
                run_id=work_id,
                spec_id=str(metadata.get("spec_id") or "unknown"),
                lane="codex" if "codex" in str(lane_name).lower() else "claude",
                plan_tokens_in=int(metadata.get("plan_tokens_in") or 0),
                plan_tokens_out=int(metadata.get("plan_tokens_out") or 0),
                plan_wall_ms=int(plan_wall_ms),
                spec_outcome=spec_outcome,
            )
            append_memory_eval(self.project_dir, record)
        except Exception:  # noqa: BLE001 - telemetry must never break dispatch
            pass

    # --- phase advance + gates --------------------------------------------
    def _spec_id_for(self, item: WorkItem) -> str | None:
        sid = item.task_ref.get("spec_id")
        if sid:
            return str(sid)
        ref = str(item.task_ref.get("runner_task_ref") or item.task_ref.get("ref") or "")
        parts = Path(ref).parts
        if "specs" in parts:
            i = parts.index("specs")
            if i + 1 < len(parts):
                return parts[i + 1]
        return None

    def _effective_plan_gate(self, spec_id: str | None) -> bool:
        """Per-spec plan-gate resolution. Full automation is the DEFAULT: a spec runs to
        verified/ready-to-archive with no human stop. A spec OPTS IN to the plan-approval
        gate by setting `plan_gate: true` in its spec.yaml (e.g. `draft --plan-gate`); the
        pipeline value is only the project-wide fallback when the spec omits its own.
        """
        default = bool(self.pipeline.get("plan_gate", False))
        if not spec_id:
            return default
        spec = _safe_yaml(runtime_dir(self.project_dir) / "specs" / spec_id / "spec.yaml") or {}
        val = spec.get("plan_gate")
        if val is None:
            return default
        if isinstance(val, str):  # tolerate the yaml shim stringifying booleans
            return val.strip().lower() in ("true", "1", "yes", "on")
        return bool(val)

    def _has_active_item(self, spec_id: str, phase: str) -> bool:
        """True if a non-terminal queue item already exists for (spec, phase)."""
        for it in self.store.reconstruct().items.values():
            if it.state in TERMINAL_STATES:
                continue
            tr = it.task_ref or {}
            if tr.get("spec_id") == spec_id and f"phase-{phase}" in str(tr.get("runner_task_ref", "")):
                return True
        return False

    def _enqueue_phase(
        self,
        spec_id: str,
        phase: str,
        lane: str | None,
        priority: int,
        *,
        author_lane: str | None = None,
    ) -> None:
        ref = f"{runtime_dir(self.project_dir).name}/specs/{spec_id}/runs/phase-{phase}.yaml"
        task_ref = {
            "kind": "builder-phase-batch",
            "runner_task_ref": ref,
            "spec_id": spec_id,
        }
        if author_lane:
            task_ref["author_lane"] = author_lane
        self.store.enqueue(
            task_ref=task_ref,
            lane=lane,
            priority=priority,
        )

    def _author_lane(self, requested_lane: str | None) -> str:
        """Resolve the lane that owns author-side turns for this spec."""
        return route_lane(
            "implement",
            list(self.config.lanes.keys()),
            requested_lane=requested_lane,
            default_lane=self.pipeline.get("default_lane", "claude"),
        )

    def _route_phase(self, phase: str, requested_lane: str | None) -> str:
        """Pick the lane for `phase` — locked to default_lane (claude) unless a lane is
        explicitly carried forward (requested_lane), then the review-augmented override:
        review phases prefer the configured review lane but MUST resolve to a different
        model family from their author; another configured lane is chosen when needed.
        Shared by _advance_after_success and the reaper's stranded-spec resume so a
        normally-advanced phase and a self-healed one route identically."""
        routed = route_lane(
            phase, list(self.config.lanes.keys()),
            requested_lane=requested_lane,
            default_lane=self.pipeline.get("default_lane", "claude"),
        )
        if normalize_phase(phase) in REVIEW_LANE_PHASES:
            author_lane = self._author_lane(requested_lane)
            author_phase = (
                "spec"
                if normalize_phase(phase) in ("spec-review", "spec-review-2")
                else "implement"
            )
            routed = select_independent_review_lane(
                author_phase,
                normalize_phase(phase) or phase,
                author_lane,
                self._review_lane,
                {
                    lane_name: lane_config.provider
                    for lane_name, lane_config in self.config.lanes.items()
                },
            )
        return routed

    def _maybe_pin_gated_phase(self, item: WorkItem, result) -> bool:
        """Gate robustness: when plan_gate is armed, a re-queued pre-implement phase
        (spec..plan) must re-run AS ITSELF rather than re-detect the next phase its
        turn advanced spec.yaml.current_phase to as completion bookkeeping. resolve_work
        prefers task_ref['phase'] over detect_phase, so pinning the dispatch-time phase
        keeps an interrupted gated plan/spec turn on the gated side of the
        plan->implement boundary — the gate still fires on a discrete completed=='plan'.
        Without this, an interrupted plan turn that already set current_phase: implement
        would re-dispatch as an UN-gated implement and bypass the gate. Returns True if a
        pin was written. No-op (False) when the gate is off for this spec, so the
        non-gated fast-forward-via-resume path is unchanged.
        """
        if not self._effective_plan_gate(self._spec_id_for(item)):
            return False
        phase = normalize_phase((result.metadata or {}).get("phase"))
        if phase in PRE_IMPLEMENT_PHASES and item.task_ref.get("phase") != phase:
            item.task_ref["phase"] = phase
            return True
        return False

    def _advance_after_success(self, item: WorkItem, completed_phase: str | None) -> None:
        """On phase success, enqueue the next phase — unless the plan-approval gate
        holds once after the plan phase (the plan_gate flag).

        The COMPLETED phase (from the attempt metadata) is authoritative for what
        ran; the next phase comes from the canonical phase order. A *fresh*
        handoff.yaml (completed_phase matches) may override the next phase to
        support rework (6-verify -> 5-implement). A stale handoff is ignored, so a
        terminal phase never re-enqueues itself.
        """
        spec_id = self._spec_id_for(item)
        completed = normalize_phase(completed_phase)
        if not spec_id or not completed:
            return
        spec_dir = runtime_dir(self.project_dir) / "specs" / spec_id
        spec = _safe_yaml(spec_dir / "spec.yaml") or {}
        order = phase_order_for_count(review_count_for_spec(spec, self.pipeline))
        handoff = _safe_yaml(spec_dir / "handoff.yaml") or {}
        nxt = next_phase(completed, order=order)
        if normalize_phase(handoff.get("completed_phase")) == completed:
            handoff_next = normalize_phase(handoff.get("next_phase"))
            if handoff_next:
                nxt = handoff_next  # fresh handoff wins (rework loop-back)
        # R6: bound the verify<->implement rework loop. Each VERIFIED_WITH_TASKS that
        # loops verify -> implement increments a per-spec counter; beyond rework_max the
        # spec escalates to a durable BLOCKED_HUMAN item instead of ping-ponging all night
        # at Opus-xhigh prices under the notifier's radar. A clean verify (forward) clears
        # it. The bump is gated on _has_active_item so a duplicate/replayed verify success
        # cannot inflate the counter (idempotent).
        if completed in ("verify", "6-verify"):
            if nxt in ("implement", "5-implement"):
                if not self._has_active_item(spec_id, nxt):
                    rework_max = self._as_num((self.config.retry_policy or {}).get("rework_max", 0), 0)
                    count = self._bump_rework(spec_id)
                    if rework_max and count > rework_max:
                        self._block_rework_exceeded(spec_id, nxt, item, count)
                        return  # escalated to a human — do not loop again
            else:
                self._reset_rework(spec_id)
        # Delivery occurs only after terminal sync; verified work is not complete yet.
        if completed == "sync" and (self.pipeline.get("deliver") or {}).get("enabled"):
            if not self._deliver(spec_id):
                return  # delivery failed — leave the spec verified for a human, don't archive
        if not nxt:
            self._note_spec_outcome(success=True)  # R6: a spec completed its pipeline -> reset breaker
            # M-C(b): the pipeline completed WITHOUT ever calling `_deliver` (delivery
            # disabled, or the completed phase wasn't verify) — `_cleanup_worktree`
            # (the delivery-success clear point) never ran, so clear the sticky
            # fallback marker here too. Idempotent/no-op if it was never set, and
            # harmless if delivery DID run (already cleared by `_cleanup_worktree`).
            self._clear_fallback(spec_id)
            return  # terminal phase — pipeline complete
        # Idempotent advance: if the next phase already has an active (non-terminal)
        # item, or a plan gate is already pending for this spec, do nothing — guards
        # against double-firing (resume re-success / re-dispatch enqueuing duplicates).
        if self._has_active_item(spec_id, nxt) or (self.store.queue_dir / "gates" / f"{spec_id}.json").exists():
            return
        # Route the next phase (shared with the reaper's stranded-spec resume so both
        # route identically): locked to default_lane (claude) unless this spec carries
        # an explicit lane forward, then the review-augmented per-phase override.
        requested_lane = item.lane
        if completed in REVIEW_LANE_PHASES:
            # Review is a temporary cross-family detour. Resume the lane that authored
            # the judged artifact instead of carrying the reviewer's lane forward.
            requested_lane = item.task_ref.get("author_lane")
        author_lane = self._author_lane(requested_lane)
        routed = self._route_phase(nxt, author_lane)
        if self._effective_plan_gate(spec_id) and completed in ("plan", "4-plan"):
            if self._handle_plan_gate(spec_id, nxt, routed, item, completed):
                return
        self._enqueue_phase(
            spec_id,
            nxt,
            routed,
            item.priority,
            author_lane=author_lane if normalize_phase(nxt) in REVIEW_LANE_PHASES else None,
        )

    # --- D2/D3: graduated (lane A/B/C) gate approval -----------------------
    def _handle_plan_gate(self, spec_id: str, nxt: str, routed: str, item: WorkItem, completed_phase: str) -> bool:
        """Route the armed plan-approval gate through the decided lane instead of
        the old blanket human-approve. Returns True if the gate HELD (caller must
        not enqueue `nxt` itself — either this method already did, for lane A, or
        the phase is waiting on the veto window / a human). Returns False only
        when the caller's normal un-gated enqueue should proceed (never true here
        today, since lane A enqueues itself; kept for symmetry/robustness).

        A fresh plan cycle always re-arms: any stale `.approved` token or `.hold`
        marker from a PRIOR gate on this spec is cleared before the new decision
        is recorded, so an old approval can never be replayed against a new plan.
        """
        gates_dir = self.store.queue_dir / "gates"
        gates_dir.mkdir(parents=True, exist_ok=True)
        (gates_dir / f"{spec_id}.approved").unlink(missing_ok=True)
        (gates_dir / f"{spec_id}.hold").unlink(missing_ok=True)

        spec_dir = runtime_dir(self.project_dir) / "specs" / spec_id
        # AC-R8-2: only the advisory proposed_gate_lane/gate_risk_signals keys are
        # ever read from agent-authored handoff.yaml -- a directly agent-written
        # gate_lane/final_lane/etc. is REJECTED (never consulted, let alone
        # honored); fail closed to "nothing proposed" (the safe lane-B default)
        # rather than crash the dispatch cycle on a poisoned handoff.
        from _dispatch_runtime.phase_runtime import resolve_gate_lane_proposal
        try:
            proposed_lane, risk_signals = resolve_gate_lane_proposal(
                runtime_dir(self.project_dir) / "specs", spec_id
            )
        except gate_policy.SelfClassificationError as exc:
            proposed_lane, risk_signals = None, []
            self._notify("gate_self_classification_rejected", {
                "spec_id": spec_id, "phase": completed_phase, "work_id": spec_id, "reason": str(exc),
            })
        policy = gate_policy.load_policy(gate_policy.default_policy_path(self.project_dir))
        decision = gate_policy.decide(proposed_lane, risk_signals, policy)
        quiet_period_seconds = int(
            (policy.get("veto_window") or {}).get("quiet_period_seconds", 3600)
        )

        payload = {
            "spec_id": spec_id, "next_phase": nxt, "lane": routed, "priority": item.priority,
            "gate_lane": decision.lane, "policy_version": decision.policy_version,
            "proposed_lane": decision.proposed_lane, "risk_signals": list(decision.risk_signals),
            # Freeze the decision-time policy parameter into the audit record.
            # A later edit to the policy document governs NEW decisions only;
            # it cannot silently shorten an already-open veto window.
            "quiet_period_seconds": quiet_period_seconds,
        }

        if decision.lane == "A":
            required = (PHASE_META.get(normalize_phase(completed_phase)) or {}).get("artifacts") or []
            # Reaching here means the phase already SUCCEEDED (validate_phase_completion
            # already required the artifacts + a clean host verdict), so validators_green
            # is True by construction; the artifact re-check is the AC-R2-2 belt-and-braces.
            if gate_policy.lane_a_flow_through_ready(spec_dir, required, validators_green=True):
                self._notify("gate_lane_a_flow_through", {
                    "spec_id": spec_id, "phase": completed_phase, "work_id": spec_id,
                    "policy_version": decision.policy_version,
                })
                self._enqueue_phase(spec_id, nxt, routed, item.priority)
                return True
            # Artifacts missing -> never silently auto-pass. Preserve the
            # engine's lane-A decision (the scheduler is not a second lane
            # classifier) and hold for explicit operator resolution.
            marker = gates_dir / f"{spec_id}.json"
            marker.write_text(json.dumps(payload), encoding="utf-8")
            self._notify("plan_ready", {
                "spec_id": spec_id, "phase": completed_phase, "work_id": spec_id,
                "gate_lane": "A", "policy_version": decision.policy_version,
                "reason": "lane A readiness failed: required phase artifacts are missing",
            })
            return True

        marker = gates_dir / f"{spec_id}.json"
        if payload["gate_lane"] == "B":
            payload["opened_at"] = _iso_now()
            marker.write_text(json.dumps(payload), encoding="utf-8")
            self._notify("veto_window_opened", {
                "spec_id": spec_id, "phase": completed_phase, "work_id": spec_id,
                "policy_version": decision.policy_version,
                "quiet_period_seconds": quiet_period_seconds,
            })
            return True

        # Lane C: stays closed until an explicit recorded human approval exists,
        # regardless of elapsed time -- the `approve` CLI writes the same
        # `.approved` token the admission guard already checks.
        marker.write_text(json.dumps(payload), encoding="utf-8")
        self._notify("plan_ready", {
            "spec_id": spec_id, "phase": completed_phase, "work_id": spec_id,
            "gate_lane": "C", "policy_version": decision.policy_version,
        })
        return True

    def _process_veto_windows(self) -> list[str]:
        """AC-R3-2: auto-open every pending lane-B gate whose quiet period has
        elapsed with no recorded hold. Called once per dispatch_once cycle
        (under the scheduler lock) so silence-is-consent progresses without a
        human `approve`. A lane-C marker (or one with no `opened_at`, or a
        `.hold` marker present) is left untouched."""
        gates_dir = self.store.queue_dir / "gates"
        if not gates_dir.is_dir():
            return []
        opened: list[str] = []
        now = _utc_now()
        for marker in sorted(gates_dir.glob("*.json")):
            spec_id = marker.stem
            try:
                payload = json.loads(marker.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if payload.get("gate_lane") != "B":
                continue
            if (gates_dir / f"{spec_id}.hold").exists():
                continue
            opened_at = payload.get("opened_at")
            if not opened_at:
                continue
            try:
                quiet = max(0, int(payload.get("quiet_period_seconds", 3600)))
                elapsed_ok = _parse_datetime(str(opened_at)) + timedelta(seconds=quiet) <= now
            except (TypeError, ValueError):
                continue
            if not elapsed_ok:
                continue
            self._open_gate(spec_id, payload)
            opened.append(spec_id)
        return opened

    def _open_gate(self, spec_id: str, payload: dict[str, Any]) -> None:
        gates_dir = self.store.queue_dir / "gates"
        nxt = payload.get("next_phase")
        ref = f"{runtime_dir(self.project_dir).name}/specs/{spec_id}/runs/phase-{nxt}.yaml"
        self.store.enqueue(
            task_ref={"kind": "builder-phase-batch", "runner_task_ref": ref, "spec_id": spec_id},
            lane=(payload.get("lane") or None),
            priority=int(payload.get("priority", 0)),
        )
        # Durable approval token: the SAME admission guard `approve` relies on
        # (lane_common.resolve_work) — an auto-opened lane-B window is recorded
        # exactly like a human approval, never a separate/weaker path.
        (gates_dir / f"{spec_id}.approved").write_text(
            f"approved phase: {nxt} (lane B veto window elapsed)\n", encoding="utf-8"
        )
        (gates_dir / f"{spec_id}.json").unlink(missing_ok=True)
        (gates_dir / f"{spec_id}.hold").unlink(missing_ok=True)

    def hold_veto_window(self, spec_id: str, reason: str = "") -> bool:
        """AC-R3-3: record a hold during a pending lane-B veto window so it never
        auto-opens. No-op (returns False) if no lane-B gate is pending for this
        spec — a hold can only ever suppress a REAL window, never fabricate one."""
        gates_dir = self.store.queue_dir / "gates"
        marker = gates_dir / f"{spec_id}.json"
        if not marker.exists():
            return False
        try:
            payload = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        if payload.get("gate_lane") != "B":
            return False
        gates_dir.mkdir(parents=True, exist_ok=True)
        (gates_dir / f"{spec_id}.hold").write_text(f"{_iso_now()} hold: {reason}\n", encoding="utf-8")
        self._notify("veto_hold_recorded", {"spec_id": spec_id, "reason": reason, "work_id": spec_id})
        return True

    def _notify(self, kind: str, packet: dict[str, Any]) -> None:
        try:
            self.notifier.notify(kind, packet)
        except Exception:  # noqa: BLE001 - notifications never break dispatch
            pass

    def _log_tail(self, attempt_context: dict[str, Any], n: int = 20) -> str:
        try:
            lp = Path(self.store.root) / str(attempt_context.get("log_path") or "")
            if lp.exists():
                return "\n".join(lp.read_text(encoding="utf-8").splitlines()[-n:])
        except OSError:
            pass
        return ""

    def _notify_blocked(self, item: WorkItem, result, attempt_context: dict[str, Any]) -> None:
        md = result.metadata or {}
        self._notify("blocked_human", {
            "spec_id": md.get("spec_id") or self._spec_id_for(item) or "?",
            "phase": md.get("phase") or "?",
            "lane": item.lane or "?",
            "reason": md.get("reason") or "phase did not complete",
            "work_id": item.id,
            "log_tail": self._log_tail(attempt_context),
        })

    def _notify_failed(self, item: WorkItem, result, attempt_context: dict[str, Any]) -> None:
        """Alert on terminal FAILED — retry/rate-limit exhaustion or a hard error.
        Without this the queue dead-ends silently (audit A1: the Wave-0 stall)."""
        md = result.metadata or {}
        rate = result.result_type == DispatchResultType.RATE_LIMITED
        reason = md.get("message") or item.task_ref.get("last_error") or result.result_type.value
        self._notify("spec_failed", {
            "spec_id": md.get("spec_id") or self._spec_id_for(item) or "?",
            "phase": md.get("phase") or "?",
            "lane": item.lane or "?",
            "reason": ("rate-limited; " if rate else "") + str(reason),
            "attempt": item.attempt,
            "max_attempts": item.max_attempts,
            "result_type": result.result_type.value,
            "work_id": item.id,
            "log_tail": self._log_tail(attempt_context),
        })

    def _notify_lane_cooled(
        self,
        lane_name: str,
        lane_record,
        queued_on_lane: int,
        spec_id: str,
        work_id: str,
    ) -> None:
        """Best-effort debounced notify for a lane entering rate-limit cooldown.

        Debounce key: ``<lanes_dir>/<lane>.cooled-alert`` file containing the
        last-alerted ``cooldown_until`` string. If the marker matches the current
        ``lane_record.cooldown_until``, the cooldown was already alerted — skip.
        A NEW (different) ``cooldown_until`` overwrites the marker and fires a
        fresh alert (each distinct cooldown is a new decision window). OSError on
        the marker file is swallowed so a read-only FS never breaks dispatch.
        Never raises.
        """
        try:
            marker_path = self.store.lanes_dir / f"{lane_name}.cooled-alert"
            current_until = lane_record.cooldown_until or ""
            try:
                existing = marker_path.read_text(encoding="utf-8").strip()
            except OSError:
                existing = ""
            if existing == current_until:
                return  # already alerted for this exact cooldown window
            try:
                marker_path.write_text(current_until, encoding="utf-8")
            except OSError:
                pass  # best-effort; still fire the notify even if marker fails
            self._notify("lane_cooled", {
                "lane": lane_name,
                "project": self.project_dir.name,
                "cooldown_seconds": cooldown_remaining_seconds(lane_record),
                "cooldown_until": current_until,
                "queued_on_lane": queued_on_lane,
                "spec_id": spec_id,
                "work_id": work_id,
            })
        except Exception:  # noqa: BLE001 - notify must never break dispatch
            pass

    def _deliver(self, spec_id: str) -> bool:
        """Open a PR for a verified spec + arm CI-green auto-merge. Returns ok.

        R5: when worktree_isolation is on, delivery runs in the spec's isolated
        worktree (already on the delivery branch) and adds only the traceability/
        handoff-listed paths instead of `git add -A`; on a SUCCESSFUL delivery the
        now-redundant local worktree is cleaned up. Flag off -> the exact prior
        call (`deliver(self.project_dir, spec_id, dcfg, summary=summary)`), byte
        for byte.
        """
        from _dispatch_runtime.delivery import deliver

        dcfg = self.pipeline.get("deliver") or {}
        spec = _safe_yaml(runtime_dir(self.project_dir) / "specs" / spec_id / "spec.yaml") or {}
        raw = str(spec.get("summary") or "").strip()
        summary = raw.splitlines()[0][:80] if raw else ""
        isolated = bool(
            self.pipeline.get("worktree_isolation")
            or (runtime_dir(self.project_dir) / "specs" / spec_id / "ssot-delta.yaml").is_file()
        )
        if isolated:
            res = deliver(
                self._delivery_cwd(spec_id), spec_id, dcfg, summary=summary,
                runner=self._worktree_runner, scoped=True,
            )
        else:
            res = deliver(self.project_dir, spec_id, dcfg, summary=summary)
        if res.ok:
            self._notify("pr_opened", {"spec_id": spec_id, "phase": "sync",
                                       "work_id": spec_id, "pr_url": res.pr_url})
            if isolated:
                self._cleanup_worktree(spec_id)
        else:
            self._notify("blocked_human", {"spec_id": spec_id, "phase": "delivery", "work_id": spec_id,
                                           "lane": "-", "reason": f"delivery failed: {res.reason}"})
        return res.ok
