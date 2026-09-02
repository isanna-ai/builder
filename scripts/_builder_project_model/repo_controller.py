from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from _dispatch_runtime.config import DispatchConfig
from _dispatch_runtime.cooldown import lane_available
from _dispatch_runtime.paths import runtime_dir
from _dispatch_runtime.queue_store import QueueStore, WorkItem
from _dispatch_runtime.routing import UnknownLaneHintError, resolve_lane
from _dispatch_runtime.scheduler import DispatchScheduler, SchedulerBusyError
from _dispatch_runtime.state_model import WorkItemState

from .eligibility import inspect_scheduler_lock, load_repo_dispatch_config, replay_observe_only


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class _NoopExecutor:
    def execute(self, task_ref, lane_name: str, attempt_context: dict[str, Any]):  # pragma: no cover
        raise AssertionError("repo controller does not execute via legacy scheduler")


@dataclass(frozen=True)
class Candidate:
    repo_id: str
    repo_root: Path
    work_id: str
    spec_id: str
    provider: str
    lane_name: str
    priority: int
    enqueued_at: str
    roadmap_index: int | None = None


@dataclass(frozen=True)
class LaunchLease:
    work_id: str
    attempt_id: str
    lane_name: str
    spec_id: str


@dataclass(frozen=True)
class ControllerStatus:
    repo_id: str
    owned: bool
    findings: list[str]
    candidates: list[Candidate]


class RepoController:
    def __init__(self, repo_id: str, repo_root: Path, *, owner_id: str):
        self.repo_id = repo_id
        self.repo_root = Path(repo_root).resolve()
        self.runtime_root = runtime_dir(self.repo_root)
        self.queue_root = self.runtime_root / "dispatch-queue"
        self.config: DispatchConfig = load_repo_dispatch_config(self.repo_root)
        self.store = QueueStore(self.queue_root)
        self.scheduler = DispatchScheduler(
            self.store,
            self.config,
            _NoopExecutor(),
            owner_id=owner_id,
            project_dir=self.repo_root,
            # Lease must outlast the longest lane phase timeout (claude lane
            # DEFAULT_TIMEOUT=1800s) so reclaim_stale_leases() never reclaims a
            # still-running attempt and double-dispatches it (audit A3).
            lease_seconds=2100,
        )
        self._owned = False

    def inspect_ownership(self):
        return inspect_scheduler_lock(self.store.queue_dir / ".scheduler.lock")

    def acquire_ownership(self) -> str | None:
        lock = self.inspect_ownership()
        if lock.state == "locked-live":
            raise SchedulerBusyError(f"repo owned by live legacy scheduler: {lock.owner}")
        owner = self.scheduler.acquire_scheduler_lock(wait=False)
        self._owned = True
        return owner

    def release_ownership(self) -> None:
        self.scheduler.release_scheduler_lock()
        self._owned = False

    def lane_available(self, lane_name: str) -> bool:
        snapshot = self.store.reconstruct()
        lane_record = snapshot.lanes.get(lane_name)
        if not lane_available(lane_record):
            return False
        inflight = 0
        for item in snapshot.items.values():
            if item.lane == lane_name and item.state in {WorkItemState.DISPATCHED, WorkItemState.RUNNING}:
                inflight += 1
        return inflight < self.config.lanes[lane_name].max_concurrency

    def current_candidates(self) -> ControllerStatus:
        findings: list[str] = []
        candidates: list[Candidate] = []
        try:
            replay = replay_observe_only(self.repo_root)
        except Exception as exc:  # noqa: BLE001
            return ControllerStatus(self.repo_id, self._owned, [str(exc)], [])
        for entry in replay.entries:
            if not entry.spec_id or not entry.selected_lane or not entry.provider:
                continue
            try:
                candidates.append(
                    Candidate(
                        repo_id=self.repo_id,
                        repo_root=self.repo_root,
                        work_id=entry.work_id,
                        spec_id=entry.spec_id,
                        provider=entry.provider,
                        lane_name=entry.selected_lane,
                        priority=entry.priority,
                        enqueued_at=entry.created_at,
                    )
                )
            except Exception as exc:  # noqa: BLE001
                findings.append(f"{entry.work_id}: {exc}")
        return ControllerStatus(self.repo_id, self._owned, findings, candidates)

    def recheck_candidate(self, work_id: str) -> Candidate | None:
        item = self.store.get_item(work_id)
        if item is None or item.state != WorkItemState.QUEUED:
            return None
        spec_id = str(item.task_ref.get("spec_id") or "")
        if not spec_id:
            return None
        dispatchable = {row.id: row for row in self.scheduler._dispatchable_items()}
        current = dispatchable.get(work_id)
        if current is None:
            return None
        try:
            lane_name = resolve_lane(current, self.config, self.scheduler._eligible_lanes()).lane_name
        except UnknownLaneHintError:
            return None
        provider = self.config.lanes[lane_name].provider
        return Candidate(
            repo_id=self.repo_id,
            repo_root=self.repo_root,
            work_id=current.id,
            spec_id=spec_id,
            provider=provider,
            lane_name=lane_name,
            priority=current.priority,
            enqueued_at=current.created_at,
        )

    def begin_launch(self, work_id: str) -> LaunchLease:
        item = self.store.get_item(work_id)
        if item is None:
            raise KeyError(work_id)
        spec_id = str(item.task_ref.get("spec_id") or "")
        lane_name = str(item.lane or "")
        if not lane_name:
            checked = self.recheck_candidate(work_id)
            if checked is None:
                raise RuntimeError(f"work item {work_id} is no longer eligible")
            lane_name = checked.lane_name
        attempt_id = f"attempt-{uuid4().hex}"
        item.attempt += 1
        item.lane = lane_name
        item.lease = {
            "id": f"lease-{uuid4().hex}",
            "attempt_id": attempt_id,
            "lane": lane_name,
            "owner": self.scheduler.owner_id,
            "expires_at": (_utc_now() + timedelta(seconds=self.scheduler.lease_seconds)).isoformat().replace("+00:00", "Z"),
        }
        item.state = WorkItemState.DISPATCHED
        self.store.save_item(item)
        self.store.record_attempt(
            work_id,
            attempt_id=attempt_id,
            lane=lane_name,
            metadata={"work_id": work_id, "lane": lane_name, "started_at": _utc_now().isoformat().replace("+00:00", "Z")},
        )
        item.state = WorkItemState.RUNNING
        self.store.save_item(item)
        return LaunchLease(work_id=work_id, attempt_id=attempt_id, lane_name=lane_name, spec_id=spec_id)

    def revert_launch(self, work_id: str, *, error: str) -> None:
        item = self.store.get_item(work_id)
        if item is None:
            return
        item.state = WorkItemState.QUEUED
        item.task_ref["last_error"] = error
        item.lease = {}
        self.store.save_item(item)
