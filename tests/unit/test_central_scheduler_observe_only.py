from __future__ import annotations

import hashlib
import os
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
FIXTURE = ROOT / "tests" / "fixtures" / "builder_project_model" / "central_scheduler" / "portfolio"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from _builder_project_model import (  # noqa: E402
    acquire_home_lock,
    build_central_snapshot,
    inspect_home_lock,
    inspect_scheduler_lock,
    replay_observe_only,
)
from _dispatch_runtime.config import load_dispatch_config  # noqa: E402
from _dispatch_runtime.paths import runtime_dir  # noqa: E402
from _dispatch_runtime.queue_store import QueueStore  # noqa: E402
from _dispatch_runtime.routing import UnknownLaneHintError, resolve_lane  # noqa: E402
from _dispatch_runtime.scheduler import DispatchScheduler  # noqa: E402

from _builder_project_model.repo_controller import RepoController  # noqa: E402


class _NoopExecutor:
    def execute(self, task_ref, lane_name, attempt_context):  # pragma: no cover
        raise AssertionError("legacy replay must never execute")


def _copy_fixture(tmp_path: Path) -> Path:
    target = tmp_path / "portfolio"
    shutil.copytree(FIXTURE, target)
    for repo in ("alpha-repo", "beta-repo", "locked-repo", "broken-repo"):
        (target / repo / ".git").mkdir(parents=True)
    return target


def _seed_queue(repo_root: Path) -> QueueStore:
    store = QueueStore(runtime_dir(repo_root) / "dispatch-queue")
    return store


def _seed_portfolio(portfolio: Path) -> dict[str, str]:
    alpha = _seed_queue(portfolio / "alpha-repo")
    beta = _seed_queue(portfolio / "beta-repo")
    locked = _seed_queue(portfolio / "locked-repo")

    alpha_ok = alpha.enqueue(
        task_ref={"kind": "builder-phase-batch", "runner_task_ref": ".builder/specs/alpha-core/runs/phase-3-review.yaml", "spec_id": "alpha-core"},
        priority=20,
        lane="claude",
    )
    alpha_blocked = alpha.enqueue(
        task_ref={"kind": "builder-phase-batch", "runner_task_ref": ".builder/specs/alpha-blocked/runs/phase-3-review.yaml", "spec_id": "alpha-blocked"},
        priority=30,
        lane="claude",
    )
    alpha_unknown = alpha.enqueue(
        task_ref={"kind": "builder-phase-batch", "runner_task_ref": ".builder/specs/alpha-core/runs/phase-4-plan.yaml", "spec_id": "alpha-core"},
        priority=10,
        lane="missing-lane",
    )
    beta_item = beta.enqueue(
        task_ref={"kind": "builder-phase-batch", "runner_task_ref": ".builder/specs/beta-fix/runs/phase-3-review.yaml", "spec_id": "beta-fix"},
        priority=15,
        lane="claude",
    )
    (runtime_dir(portfolio / "locked-repo") / "dispatch.yaml").parent.mkdir(parents=True, exist_ok=True)
    (runtime_dir(portfolio / "locked-repo") / "dispatch.yaml").write_text(
        (runtime_dir(portfolio / "alpha-repo") / "dispatch.yaml").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (locked.queue_dir / ".scheduler.lock").write_text(f"dispatch-{os.getpid()}\n", encoding="utf-8")

    return {
        "alpha_ok": alpha_ok.id,
        "alpha_blocked": alpha_blocked.id,
        "alpha_unknown": alpha_unknown.id,
        "beta_item": beta_item.id,
    }


def _legacy_observed(repo_root: Path):
    config = load_dispatch_config(runtime_dir(repo_root) / "dispatch.yaml")
    with tempfile.TemporaryDirectory(prefix="legacy-observed-") as temp_dir:
        queue_copy = Path(temp_dir) / "dispatch-queue"
        shutil.copytree(runtime_dir(repo_root) / "dispatch-queue", queue_copy)
        copied = type(config)(
            queue_store_path=queue_copy,
            lanes=config.lanes,
            routing_policy=config.routing_policy,
            cooldown_policy=config.cooldown_policy,
            retry_policy=config.retry_policy,
            pipeline=config.pipeline,
        )
        store = QueueStore(queue_copy)
        scheduler = DispatchScheduler(store, copied, _NoopExecutor(), owner_id="legacy-observer", project_dir=repo_root)
        items = scheduler._dispatchable_items()
        eligible_lanes = scheduler._eligible_lanes()
        rows = []
        for item in items:
            try:
                lane = resolve_lane(item, copied, eligible_lanes).lane_name
                reason = None
            except UnknownLaneHintError as exc:
                lane = None
                reason = str(exc)
            rows.append((item.id, lane, reason))
        return rows


def _runtime_digest(repo_root: Path) -> dict[str, str]:
    runtime_root = runtime_dir(repo_root)
    digests: dict[str, str] = {}
    for path in sorted(p for p in runtime_root.rglob("*") if p.is_file()):
        digests[str(path.relative_to(runtime_root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return digests


def test_home_lock_health_and_acquisition_are_home_only(tmp_path):
    portfolio = _copy_fixture(tmp_path)
    home_root = portfolio / ".builder-home"

    status = inspect_home_lock(home_root)
    assert status.state == "unlocked"

    handle = acquire_home_lock(home_root, owner_id="dispatch-424242")
    try:
        held = inspect_home_lock(home_root)
        assert held.state in {"locked-live", "locked-stale"}
        assert held.owner == "dispatch-424242"
        repo_lock = inspect_scheduler_lock(runtime_dir(portfolio / "alpha-repo") / "dispatch-queue" / "queue" / ".scheduler.lock")
        assert repo_lock.state == "unlocked"
    finally:
        handle.release()
    assert inspect_home_lock(home_root).state == "unlocked"


def test_observe_only_replay_matches_legacy_scheduler_on_fixture_queues(tmp_path):
    portfolio = _copy_fixture(tmp_path)
    work_ids = _seed_portfolio(portfolio)

    observed = replay_observe_only(portfolio / "alpha-repo")
    legacy = _legacy_observed(portfolio / "alpha-repo")

    assert [(row.work_id, row.selected_lane, row.route_reason) for row in observed.entries] == legacy
    assert [row.work_id for row in observed.entries] == [work_ids["alpha_ok"], work_ids["alpha_unknown"]]

    blocked = QueueStore(runtime_dir(portfolio / "alpha-repo") / "dispatch-queue").get_item(work_ids["alpha_blocked"])
    assert blocked is not None
    assert blocked.state.value == "queued"


def test_central_snapshot_is_deterministic_and_isolates_locked_and_malformed_repos(tmp_path):
    portfolio = _copy_fixture(tmp_path)
    work_ids = _seed_portfolio(portfolio)

    first = build_central_snapshot(projects_root=portfolio, start=portfolio / "alpha-repo")
    second = build_central_snapshot(projects_root=portfolio, start=portfolio / "alpha-repo")

    assert first.mode == "builder-home"
    assert [provider.selected_work_ids for provider in first.providers] == [provider.selected_work_ids for provider in second.providers]

    controllers = {controller.repo_id: controller for controller in first.controllers}
    assert controllers["alpha-repo"].healthy is True
    assert controllers["beta-repo"].healthy is True
    assert controllers["locked-repo"].healthy is False
    assert any("live legacy scheduler" in finding for finding in controllers["locked-repo"].findings)
    assert controllers["broken-repo"].healthy is False
    assert any("missing dispatch config" in finding for finding in controllers["broken-repo"].findings)

    claude = next(provider for provider in first.providers if provider.provider == "claude-code-cli")
    assert claude.candidate_work_ids == [work_ids["alpha_ok"], work_ids["beta_item"]]
    assert claude.selected_work_ids == [work_ids["alpha_ok"]]


def test_observe_only_snapshot_performs_zero_repo_runtime_mutations_and_never_acquires_repo_lock(tmp_path):
    portfolio = _copy_fixture(tmp_path)
    work_ids = _seed_portfolio(portfolio)
    before = {
        repo: _runtime_digest(portfolio / repo)
        for repo in ("alpha-repo", "beta-repo", "locked-repo")
    }
    acquired: list[str] = []

    def _boom(*args, **kwargs):
        acquired.append("called")
        raise AssertionError("observe-only path must not acquire repo scheduler locks")

    original = DispatchScheduler.acquire_scheduler_lock
    DispatchScheduler.acquire_scheduler_lock = _boom
    try:
        snapshot = build_central_snapshot(projects_root=portfolio, start=portfolio / "alpha-repo")
        assert snapshot.controllers
        assert acquired == []
    finally:
        DispatchScheduler.acquire_scheduler_lock = original

    after = {
        repo: _runtime_digest(portfolio / repo)
        for repo in ("alpha-repo", "beta-repo", "locked-repo")
    }
    assert after == before

    blocked = QueueStore(runtime_dir(portfolio / "alpha-repo") / "dispatch-queue").get_item(work_ids["alpha_blocked"])
    assert blocked is not None
    assert blocked.state.value == "queued"


def test_repo_controller_lease_outlasts_claude_lane_attempt_timeout(tmp_path):
    # audit-A3: the lease granted by begin_launch() must outlast the longest
    # lane phase timeout (claude lane DEFAULT_TIMEOUT=1800s), else
    # reclaim_stale_leases() could reclaim a still-running attempt and
    # double-dispatch it. RepoController used to build its DispatchScheduler
    # with the 300s default lease_seconds instead of the 2100s used by the
    # CLI/attempt_runner dispatch paths.
    portfolio = _copy_fixture(tmp_path)
    work_ids = _seed_portfolio(portfolio)

    controller = RepoController(
        "alpha-repo",
        portfolio / "alpha-repo",
        owner_id="central-test",
    )
    lease = controller.begin_launch(work_ids["alpha_ok"])

    item = controller.store.get_item(lease.work_id)
    assert item is not None
    assert item.lease.get("attempt_id") == lease.attempt_id
    expires_at = datetime.fromisoformat(item.lease["expires_at"].replace("Z", "+00:00"))
    remaining = (expires_at - datetime.now(timezone.utc)).total_seconds()
    assert remaining >= 1800, remaining


def test_observe_only_replay_is_field_identical_ignoring_attempt_event_history(tmp_path):
    # perf(central): replay_observe_only's copytree now skips copying attempts/
    # and events/ FILES (keeping the directories, since ensure_observable_queue_root
    # requires the full QueueStore layout). This is only safe if nothing on the
    # replay path actually reads attempt/event content. Prove it: build a queue
    # with items + lanes + attempts + events, replay it (history files present on
    # disk, but not copied per the fix), then physically strip attempts/events off
    # the SOURCE queue and replay again. If the two field-identical, the ignored
    # history genuinely never influenced the result.
    portfolio = _copy_fixture(tmp_path)
    work_ids = _seed_portfolio(portfolio)
    store = QueueStore(runtime_dir(portfolio / "alpha-repo") / "dispatch-queue")
    store.record_attempt(work_ids["alpha_ok"], attempt_id="attempt-alpha-ok", lane="claude")
    store.record_attempt(work_ids["beta_item"], attempt_id="attempt-beta", lane="claude")
    store.append_event(work_ids["alpha_ok"], "attempt_started", {"note": "history should never be read"})
    store.set_lane_cooldown("claude", until="2999-01-01T00:00:00Z", reason="test-cooldown")

    with_history = replay_observe_only(portfolio / "alpha-repo")

    assert store.attempts_dir.exists() and any(store.attempts_dir.glob("*.yaml"))
    assert store.events_dir.exists() and any(store.events_dir.glob("*.yaml"))
    for path in store.attempts_dir.glob("*.yaml"):
        path.unlink()
    for path in store.events_dir.glob("*.yaml"):
        path.unlink()

    without_history = replay_observe_only(portfolio / "alpha-repo")

    assert with_history.eligible_lanes == without_history.eligible_lanes
    assert with_history.entries == without_history.entries
    assert with_history.cooldowns == without_history.cooldowns
    assert with_history.entries  # entries still populate
    assert with_history.cooldowns  # cooldowns still populate
    assert any(view.lane == "claude" and view.reason == "test-cooldown" for view in with_history.cooldowns)


def test_legacy_discovery_fallback_without_builder_home_stays_available(tmp_path):
    portfolio = _copy_fixture(tmp_path)
    _seed_portfolio(portfolio)
    shutil.rmtree(portfolio / ".builder-home")

    snapshot = build_central_snapshot(projects_root=portfolio, start=portfolio / "alpha-repo")

    assert snapshot.mode == "legacy"
    assert snapshot.home_root is None
    assert {controller.repo_id for controller in snapshot.controllers} >= {"alpha-repo", "beta-repo", "locked-repo"}
