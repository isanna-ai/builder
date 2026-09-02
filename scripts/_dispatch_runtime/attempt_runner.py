"""Lease-scoped execution seam for work selected by the central drainer."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from datetime import datetime, timezone

from _builder_project_model.live_runtime import CENTRAL_OWNER_PREFIX, activated_home_for_repo
from _dispatch_runtime.config import load_dispatch_config
from _dispatch_runtime.paths import runtime_dir
from _dispatch_runtime.queue_store import QueueStore
from _dispatch_runtime.scheduler import DispatchScheduler
from _dispatch_runtime.state_model import WorkItemState


class AttemptRefused(RuntimeError):
    pass


def _attempt_context(scheduler: DispatchScheduler, item, attempt_id: str) -> dict[str, Any]:
    spec_id = scheduler._spec_id_for(item)
    workspace_root = str(scheduler.project_dir)
    sync_isolated = bool(
        spec_id and (runtime_dir(scheduler.project_dir) / "specs" / spec_id / "ssot-delta.yaml").is_file()
    )
    if (scheduler.pipeline.get("worktree_isolation") or sync_isolated) and spec_id:
        workspace_root = str(scheduler._ensure_worktree(spec_id))
    return {
        "attempt_id": attempt_id,
        "work_id": item.id,
        "log_path": f"queue/attempts/{attempt_id}.log",
        "workspace_root": workspace_root,
        "control_root": str(scheduler.project_dir),
        "queue_root": str(scheduler.store.root),
        "auto_env_up": bool(scheduler.pipeline.get("auto_env_up", True)),
        "plan_gate": scheduler._effective_plan_gate(spec_id),
    }


def run_reserved_attempt(
    work_id: str,
    *,
    config_path: Path,
    executor=None,
    expected_attempt_id: str | None = None,
    home_path: Path | None = None,
) -> None:
    """Execute exactly one already-RUNNING item without selection or lock acquisition."""

    config_file = Path(config_path).resolve()
    project_dir = config_file.parent.parent
    home, _repo_id = activated_home_for_repo(repo_root=project_dir, home_path=home_path)
    config = load_dispatch_config(config_file)
    store = QueueStore(config.queue_store_path)
    item = store.get_item(work_id)
    if item is None:
        raise AttemptRefused(f"unknown work item: {work_id}")
    lease = item.lease if isinstance(item.lease, dict) else {}
    attempt_id = str(lease.get("attempt_id") or "")
    owner = str(lease.get("owner") or "")
    if item.state != WorkItemState.RUNNING:
        raise AttemptRefused(f"work item {work_id} is not RUNNING")
    if not owner.startswith(CENTRAL_OWNER_PREFIX):
        raise AttemptRefused(f"work item {work_id} lease is not central-owned")
    expires_at = lease.get("expires_at")
    try:
        expires = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        raise AttemptRefused(f"work item {work_id} lease has no valid expiry") from None
    if expires <= datetime.now(timezone.utc):
        raise AttemptRefused(f"work item {work_id} central lease is expired")
    if not attempt_id or (expected_attempt_id is not None and attempt_id != expected_attempt_id):
        raise AttemptRefused(f"work item {work_id} attempt_id does not match reservation")
    recorded = store.reconstruct().attempts.get(attempt_id)
    if recorded is None or recorded.work_id != work_id or recorded.lane != item.lane:
        raise AttemptRefused(f"work item {work_id} has no matching attempt record")
    from _builder_project_model.session_store import SessionStore

    session_store = SessionStore(home.root)
    session_rows = session_store.list_sessions() if session_store.paths.sessions_dir.is_dir() else []
    central_sessions = [
        row for row in session_rows
        if row.get("state") in {"starting", "active", "reaping"}
        and row.get("work_id") == work_id
        and row.get("attempt_id") == attempt_id
        and Path(str(row.get("queue_root") or "")).resolve() == Path(store.root).resolve()
    ]
    if len(central_sessions) != 1:
        raise AttemptRefused(f"work item {work_id} has no unique matching central slot")

    if executor is None:
        from _dispatch_runtime.cli import _RoutingExecutor

        executor = _RoutingExecutor(config)
    scheduler = DispatchScheduler(
        store,
        config,
        executor,
        owner_id=owner,
        project_dir=project_dir,
        lease_seconds=2100,
    )
    # Deliberately call only the existing completion/finalization path.  No
    # dispatch_once(), selection, retry reset, or scheduler_lock() occurs here.
    scheduler._complete_attempt(
        work_id,
        attempt_id,
        str(item.lane),
        dict(item.task_ref),
        _attempt_context(scheduler, item, attempt_id),
    )
