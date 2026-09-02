from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from _dispatch_runtime.scheduler import SchedulerBusyError, _owner_pid, _pid_alive

from .eligibility import (
    EligibilityEntry,
    LockStatus,
    ReplaySnapshot,
    inspect_scheduler_lock,
    load_repo_dispatch_config,
    normalize_provider,
    replay_observe_only,
    repo_runtime_paths,
)
from .home import BuilderHome, load_optional_home


@dataclass(frozen=True)
class HomeLockHandle:
    path: Path
    owner_id: str

    def release(self) -> None:
        if not self.path.exists():
            return
        try:
            owner = self.path.read_text(encoding="utf-8").strip()
        except OSError:
            return
        if owner == self.owner_id:
            self.path.unlink()


@dataclass(frozen=True)
class ControllerDiscovery:
    repo_id: str
    repo_root: Path
    source: str


@dataclass(frozen=True)
class ControllerSnapshot:
    repo_id: str
    repo_root: Path
    source: str
    ownership: LockStatus
    healthy: bool
    findings: list[str]
    attributed_project: str
    roadmap_indexed: int | None
    eligibility: list[EligibilityEntry]
    eligible_lanes: list[str]
    cooldowns: list[object]


@dataclass(frozen=True)
class ProviderSimulation:
    provider: str
    project_order: list[str]
    candidate_work_ids: list[str]
    selected_work_ids: list[str]


@dataclass(frozen=True)
class WatchdogModel:
    scope: str
    mode: str
    lock_path: Path | None
    heartbeat_path: Path | None


@dataclass(frozen=True)
class CentralSnapshot:
    projects_root: Path
    mode: str
    home_root: Path | None
    home_lock: LockStatus | None
    controllers: list[ControllerSnapshot]
    providers: list[ProviderSimulation]
    watchdog: WatchdogModel


def home_lock_path(home_root: Path) -> Path:
    return home_root / "state" / "scheduler.lock"


def inspect_home_lock(home_root: Path) -> LockStatus:
    return inspect_scheduler_lock(home_lock_path(home_root))


def acquire_home_lock(home_root: Path, *, owner_id: str) -> HomeLockHandle:
    lock_path = home_lock_path(home_root)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(2):
        try:
            fd = lock_path.open("x", encoding="utf-8")
        except FileExistsError as exc:
            owner = ""
            try:
                owner = lock_path.read_text(encoding="utf-8").strip()
            except OSError:
                pass
            pid = _owner_pid(owner)
            stale = bool(owner) and (owner == owner_id or (pid is not None and not _pid_alive(pid)))
            if stale:
                try:
                    lock_path.unlink()
                except FileNotFoundError:
                    pass
                continue
            raise SchedulerBusyError(
                f"central scheduler lock is owned by another process: {lock_path} (owner={owner or '?'})"
            ) from exc
        with fd:
            fd.write(f"{owner_id}\n")
        return HomeLockHandle(path=lock_path, owner_id=owner_id)
    raise SchedulerBusyError(f"central scheduler lock still contended after stale-lock recovery: {lock_path}")


@contextmanager
def held_home_lock(home_root: Path, *, owner_id: str) -> Iterator[HomeLockHandle]:
    handle = acquire_home_lock(home_root, owner_id=owner_id)
    try:
        yield handle
    finally:
        handle.release()


def _legacy_repos(projects_root: Path) -> list[ControllerDiscovery]:
    repos: list[ControllerDiscovery] = []
    for child in sorted(projects_root.iterdir()):
        if not child.is_dir():
            continue
        runtime_root, dispatch_path, queue_root = repo_runtime_paths(child)
        if runtime_root.is_dir() and dispatch_path.is_file() and queue_root.is_dir():
            repos.append(ControllerDiscovery(repo_id=child.name, repo_root=child.resolve(), source="legacy"))
    return repos


def discover_repo_controllers(
    *,
    projects_root: Path,
    start: Path | None = None,
    home: Path | None = None,
) -> tuple[BuilderHome | None, list[ControllerDiscovery]]:
    builder_home = load_optional_home(start=start, home=home, projects_root=projects_root)
    if builder_home is not None:
        controllers = [
            ControllerDiscovery(repo_id=entry.id, repo_root=builder_home.repo_roots_by_id[entry.id], source="builder-home")
            for entry in builder_home.catalog.repos
            if entry.id in builder_home.repo_roots_by_id
        ]
        return builder_home, controllers
    return None, _legacy_repos(projects_root.resolve())


def _attributed_project(home: BuilderHome | None, repo_root: Path, repo_id: str) -> str:
    if home is None:
        return f"standalone:{repo_id}"
    projects = home.projects_for_repo(repo_root)
    if not projects:
        return f"standalone:{repo_id}"
    return projects[0].id


def _roadmap_index(home: BuilderHome | None, project_id: str, repo_id: str, spec_id: str | None) -> int | None:
    if home is None or spec_id is None or project_id.startswith("standalone:"):
        return None
    project = home.project(project_id)
    if project is None:
        return None
    for release in project.releases:
        if release.declaration.status != "active":
            continue
        for index, member in enumerate(release.declaration.specs):
            raw = member.spec
            if "/" in raw:
                alias, candidate_spec = raw.split("/", 1)
                resolved = home.resolve_project_alias(project_id, alias)
                if resolved is None:
                    continue
                candidate_repo_id = next(
                    (entry.repo_id for entry in project.declaration.repos if entry.alias == alias),
                    None,
                )
            else:
                candidate_spec = raw
                candidate_repo_id = project.declaration.default_repo
            if candidate_repo_id == repo_id and candidate_spec == spec_id:
                return index
    return None


def _simulate_provider(
    provider: str,
    controllers: list[ControllerSnapshot],
) -> ProviderSimulation:
    candidates = []
    for controller in controllers:
        if not controller.healthy:
            continue
        for entry in controller.eligibility:
            if entry.provider == provider and entry.selected_lane:
                candidates.append((controller.attributed_project, controller.roadmap_indexed, entry, controller.repo_id))
    project_order = sorted({row[0] for row in candidates})
    ordered = sorted(
        candidates,
        key=lambda row: (
            project_order.index(row[0]),
            -row[2].priority,
            row[1] is None,
            row[1] if row[1] is not None else 10**9,
            row[2].created_at,
            row[2].work_id,
        ),
    )
    return ProviderSimulation(
        provider=provider,
        project_order=project_order,
        candidate_work_ids=[row[2].work_id for row in ordered],
        selected_work_ids=[row[2].work_id for row in ordered[:1]],
    )


def snapshot_controller(
    repo_id: str,
    repo_root: Path,
    *,
    source: str,
    home: BuilderHome | None,
) -> ControllerSnapshot:
    runtime_root, dispatch_path, queue_root = repo_runtime_paths(repo_root)
    lock = inspect_scheduler_lock(queue_root / "queue" / ".scheduler.lock")
    findings: list[str] = []
    if not dispatch_path.is_file():
        findings.append(f"missing dispatch config: {dispatch_path}")
    if not queue_root.is_dir():
        findings.append(f"missing queue root: {queue_root}")
    if lock.state == "locked-live":
        findings.append(f"repo owned by live legacy scheduler: {lock.owner}")
    if lock.state in {"malformed", "unreadable"}:
        findings.append(f"repo lock {lock.state}: {lock.detail or lock.owner or '?'}")
    attributed_project = _attributed_project(home, repo_root.resolve(), repo_id)
    if findings:
        return ControllerSnapshot(
            repo_id=repo_id,
            repo_root=repo_root.resolve(),
            source=source,
            ownership=lock,
            healthy=False,
            findings=findings,
            attributed_project=attributed_project,
            roadmap_indexed=None,
            eligibility=[],
            eligible_lanes=[],
            cooldowns=[],
        )
    try:
        config = load_repo_dispatch_config(repo_root)
        for lane in config.lanes.values():
            normalize_provider(lane.provider)
        replay = replay_observe_only(repo_root)
    except Exception as exc:  # noqa: BLE001 - one bad repo must not crash the snapshot
        return ControllerSnapshot(
            repo_id=repo_id,
            repo_root=repo_root.resolve(),
            source=source,
            ownership=lock,
            healthy=False,
            findings=[str(exc)],
            attributed_project=attributed_project,
            roadmap_indexed=None,
            eligibility=[],
            eligible_lanes=[],
            cooldowns=[],
        )
    roadmap_index = None
    if replay.entries:
        roadmap_index = _roadmap_index(home, attributed_project, repo_id, replay.entries[0].spec_id)
    return ControllerSnapshot(
        repo_id=repo_id,
        repo_root=repo_root.resolve(),
        source=source,
        ownership=lock,
        healthy=True,
        findings=[],
        attributed_project=attributed_project,
        roadmap_indexed=roadmap_index,
        eligibility=replay.entries,
        eligible_lanes=replay.eligible_lanes,
        cooldowns=replay.cooldowns,
    )


def build_central_snapshot(
    *,
    projects_root: Path,
    start: Path | None = None,
    home: Path | None = None,
) -> CentralSnapshot:
    root = Path(projects_root).resolve()
    builder_home, discovered = discover_repo_controllers(projects_root=root, start=start, home=home)
    controllers = [
        snapshot_controller(entry.repo_id, entry.repo_root, source=entry.source, home=builder_home)
        for entry in discovered
    ]
    providers = [
        _simulate_provider(provider, controllers)
        for provider in ("claude-code-cli", "codex-cli")
    ]
    if builder_home is not None:
        home_root = builder_home.root
        lock = inspect_home_lock(home_root)
        watchdog = WatchdogModel(
            scope="central-daemon",
            mode="observe-only",
            lock_path=home_lock_path(home_root),
            heartbeat_path=home_root / "state" / "daemon.json",
        )
        mode = "builder-home"
    else:
        home_root = None
        lock = None
        watchdog = WatchdogModel(scope="central-daemon", mode="observe-only", lock_path=None, heartbeat_path=None)
        mode = "legacy"
    return CentralSnapshot(
        projects_root=root,
        mode=mode,
        home_root=home_root,
        home_lock=lock,
        controllers=controllers,
        providers=providers,
        watchdog=watchdog,
    )
