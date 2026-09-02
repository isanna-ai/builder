from __future__ import annotations

import shutil
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from _dispatch_runtime.config import DispatchConfig, load_dispatch_config
from _dispatch_runtime.cooldown import cooldown_remaining_seconds
from _dispatch_runtime.paths import runtime_dir
from _dispatch_runtime.queue_store import LaneRecord, QueueStore, WorkItem
from _dispatch_runtime.routing import UnknownLaneHintError, resolve_lane
from _dispatch_runtime.scheduler import DispatchScheduler, _owner_pid, _pid_alive

from .common import CANONICAL_PROVIDERS


@dataclass(frozen=True)
class LockStatus:
    path: Path
    state: str
    owner: str | None
    detail: str | None = None


@dataclass(frozen=True)
class CooldownView:
    lane: str
    cooldown_until: str | None
    remaining_seconds: int
    reason: str | None


@dataclass(frozen=True)
class EligibilityEntry:
    work_id: str
    spec_id: str | None
    lane_hint: str | None
    selected_lane: str | None
    provider: str | None
    priority: int
    created_at: str
    scheduled_after: str | None
    route_reason: str | None = None


@dataclass(frozen=True)
class ReplaySnapshot:
    eligible_lanes: list[str]
    entries: list[EligibilityEntry]
    cooldowns: list[CooldownView]


class _NoopExecutor:
    def execute(self, task_ref, lane_name: str, attempt_context: dict[str, Any]):  # pragma: no cover
        raise AssertionError("observe-only replay must never execute a lane")


def inspect_scheduler_lock(lock_path: Path) -> LockStatus:
    if not lock_path.exists():
        return LockStatus(path=lock_path, state="unlocked", owner=None)
    try:
        owner = lock_path.read_text(encoding="utf-8").strip() or None
    except OSError as exc:
        return LockStatus(path=lock_path, state="unreadable", owner=None, detail=str(exc))
    if not owner:
        return LockStatus(path=lock_path, state="malformed", owner=None, detail="empty owner")
    pid = _owner_pid(owner)
    if pid is None:
        return LockStatus(path=lock_path, state="malformed", owner=owner, detail="owner is not dispatch-<pid>")
    if _pid_alive(pid):
        return LockStatus(path=lock_path, state="locked-live", owner=owner)
    return LockStatus(path=lock_path, state="locked-stale", owner=owner)


def normalize_provider(provider: str) -> str:
    value = str(provider or "").strip()
    if value not in CANONICAL_PROVIDERS:
        raise ValueError(f"unknown provider {value!r}")
    return value


def repo_runtime_paths(repo_root: Path) -> tuple[Path, Path, Path]:
    runtime_root = runtime_dir(repo_root)
    dispatch_path = runtime_root / "dispatch.yaml"
    queue_root = runtime_root / "dispatch-queue"
    return runtime_root, dispatch_path, queue_root


def load_repo_dispatch_config(repo_root: Path) -> DispatchConfig:
    _runtime_root, dispatch_path, _queue_root = repo_runtime_paths(repo_root)
    return load_dispatch_config(dispatch_path)


def ensure_observable_queue_root(queue_root: Path) -> None:
    required = (
        queue_root,
        queue_root / "queue",
        queue_root / "queue" / "items",
        queue_root / "queue" / "attempts",
        queue_root / "queue" / "lanes",
        queue_root / "queue" / "events",
    )
    missing = [str(path) for path in required if not path.is_dir()]
    if missing:
        raise FileNotFoundError(f"queue runtime missing required paths: {', '.join(missing)}")


def _ignore_history(directory: str, names: list[str]) -> set[str]:
    # perf(central): the observe-only replay below only ever reads .items and
    # .lanes (via _dispatchable_items()/_eligible_lanes()/reconstruct().lanes) -
    # attempts/ and events/ history is parsed by QueueStore.reconstruct() but
    # never consulted on this path. Skip copying those FILES (attempts/events
    # can be thousands of records on a long-lived queue) while still creating
    # the empty attempts/ and events/ directories themselves, since
    # ensure_observable_queue_root() requires the full QueueStore layout to
    # exist.
    if Path(directory).name in ("attempts", "events"):
        return set(names)
    return set()


def _scheduler_for_copy(repo_root: Path, queue_copy_root: Path, config: DispatchConfig) -> DispatchScheduler:
    copied = replace(config, queue_store_path=queue_copy_root)
    store = QueueStore(queue_copy_root)
    return DispatchScheduler(store, copied, _NoopExecutor(), owner_id="observe-only", project_dir=repo_root)


def _lane_cooldowns(lanes: dict[str, LaneRecord]) -> list[CooldownView]:
    rows = []
    for lane_name in sorted(lanes):
        record = lanes[lane_name]
        rows.append(
            CooldownView(
                lane=lane_name,
                cooldown_until=record.cooldown_until,
                remaining_seconds=cooldown_remaining_seconds(record),
                reason=record.reason,
            )
        )
    return rows


def replay_observe_only(repo_root: Path) -> ReplaySnapshot:
    repo = Path(repo_root).resolve()
    _runtime_root, _dispatch_path, queue_root = repo_runtime_paths(repo)
    ensure_observable_queue_root(queue_root)
    config = load_repo_dispatch_config(repo)
    for lane_name, lane in config.lanes.items():
        normalize_provider(lane.provider)
        if lane_name != lane.name:
            raise ValueError(f"lane key/name mismatch for {lane_name!r}")
    with tempfile.TemporaryDirectory(prefix="observe-only-queue-") as temp_dir:
        queue_copy_root = Path(temp_dir) / "dispatch-queue"
        shutil.copytree(queue_root, queue_copy_root, ignore=_ignore_history)
        scheduler = _scheduler_for_copy(repo, queue_copy_root, config)
        items = scheduler._dispatchable_items()
        eligible_lanes = list(scheduler._eligible_lanes())
        entries: list[EligibilityEntry] = []
        for item in items:
            try:
                lane_name = resolve_lane(item, scheduler.config, eligible_lanes).lane_name
                provider = None if lane_name is None else normalize_provider(scheduler.config.lanes[lane_name].provider)
                reason = None
            except UnknownLaneHintError as exc:
                lane_name = None
                provider = None
                reason = str(exc)
            entries.append(
                EligibilityEntry(
                    work_id=item.id,
                    spec_id=scheduler._spec_id_for(item),
                    lane_hint=item.lane,
                    selected_lane=lane_name,
                    provider=provider,
                    priority=item.priority,
                    created_at=item.created_at,
                    scheduled_after=item.scheduled_after,
                    route_reason=reason,
                )
            )
        lanes = scheduler.store.reconstruct().lanes
        return ReplaySnapshot(
            eligible_lanes=eligible_lanes,
            entries=entries,
            cooldowns=_lane_cooldowns(lanes),
        )


def legacy_replay_snapshot(repo_root: Path) -> ReplaySnapshot:
    return replay_observe_only(repo_root)


def route_sort_key(item: WorkItem) -> tuple[int, str, str]:
    return (-item.priority, item.created_at, item.id)
