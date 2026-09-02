from __future__ import annotations

import threading
from datetime import datetime, timezone
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from _dispatch_runtime.scheduler import SchedulerBusyError

from .attribution import AdmissionStore, Membership, receipt_for_work
from .fair_share import FairShareCandidate, choose_candidate
from .governor import BuilderGovernor
from .home import BuilderHome
from .readiness import evaluate_cross_repo_dependencies
from .repo_controller import Candidate, RepoController
from .live_command import live_command_builder


@dataclass(frozen=True)
class DrainLaunch:
    provider: str
    project_id: str
    repo_id: str
    work_id: str
    attempt_id: str
    slot_id: str


class FederatedDrainer:
    def __init__(
        self,
        home: BuilderHome,
        *,
        owner_id: str,
        governor: BuilderGovernor | None = None,
        command_builder: Callable[[Candidate], list[str]] | None = None,
        git_runner=None,
        registry_query=None,
    ):
        self.home = home
        self.owner_id = owner_id
        self.governor = governor or BuilderGovernor(home)
        self.command_builder = command_builder or live_command_builder
        self.git_runner = git_runner
        self.registry_query = registry_query
        self._launch_lock = threading.Lock()
        self.controllers: dict[str, RepoController] = {}
        self.findings: list[str] = []

    def allowed_repo_ids(self) -> tuple[str, ...]:
        if not self.home.policy.governor_enabled:
            return ()
        return tuple(repo_id for repo_id in self.home.policy.drain_repos if repo_id in self.home.repo_roots_by_id)

    def start(self) -> list[str]:
        self.findings = []
        self.controllers = {}
        for repo_id in self.allowed_repo_ids():
            repo_root = self.home.repo_roots_by_id[repo_id]
            try:
                controller = RepoController(repo_id, repo_root, owner_id=self.owner_id)
                controller.acquire_ownership()
            except SchedulerBusyError as exc:
                self.findings.append(f"{repo_id}: loud refusal: {exc}")
                continue
            except Exception as exc:  # noqa: BLE001 - one malformed repo must not suppress healthy siblings
                self.findings.append(f"{repo_id}: controller unavailable: {exc}")
                continue
            self.controllers[repo_id] = controller
        return list(self.findings)

    def stop(self) -> None:
        for controller in self.controllers.values():
            controller.release_ownership()
        self.controllers = {}

    def _candidate_project(self, candidate: Candidate) -> tuple[str, str | None, int | None]:
        receipt = receipt_for_work(self.home.root, repo_id=candidate.repo_id, spec_id=candidate.spec_id)
        if receipt is not None:
            return receipt.project_id, receipt.release_name, receipt.roadmap_index
        return f"standalone:{candidate.repo_id}", None, None

    def _collect_provider_candidates(self, provider: str) -> list[FairShareCandidate]:
        rows: list[FairShareCandidate] = []
        for controller in self.controllers.values():
            status = controller.current_candidates()
            self.findings.extend(f"{controller.repo_id}: {item}" for item in status.findings)
            for candidate in status.candidates:
                if candidate.provider != provider:
                    continue
                blocks = evaluate_cross_repo_dependencies(
                    home=self.home,
                    repo_id=candidate.repo_id,
                    repo_root=candidate.repo_root,
                    spec_id=candidate.spec_id,
                    git_runner=self.git_runner,
                    registry_query=self.registry_query,
                )
                if blocks:
                    self.findings.extend(
                        f"{candidate.repo_id}:{candidate.spec_id}: blocked on {block.ref} ({block.required}) {block.observation}"
                        for block in blocks
                    )
                    continue
                project_id, _release_name, roadmap_index = self._candidate_project(candidate)
                rows.append(
                    FairShareCandidate(
                        provider=provider,
                        project_id=project_id,
                        repo_id=candidate.repo_id,
                        work_id=candidate.work_id,
                        lane_name=candidate.lane_name,
                        priority=candidate.priority,
                        roadmap_index=roadmap_index,
                        enqueued_at=candidate.enqueued_at,
                    )
                )
        return rows

    def drain_once(self) -> list[DrainLaunch]:
        launches: list[DrainLaunch] = []
        for provider in ("claude-code-cli", "codex-cli"):
            cooldown = self.governor.store.read_provider(provider).get("cooldown_until")
            if cooldown:
                try:
                    until = datetime.fromisoformat(str(cooldown).replace("Z", "+00:00"))
                except ValueError:
                    self.findings.append(f"{provider}: invalid global cooldown; launch refused")
                    continue
                if until > datetime.now(timezone.utc):
                    continue
            max_sessions = self.home.policy.providers[provider].max_sessions
            while self.governor.store.capacity_remaining(provider, max_sessions=max_sessions) > 0:
                candidates = self._collect_provider_candidates(provider)
                selected = choose_candidate(home_root=self.home.root, provider=provider, candidates=candidates)
                if selected is None:
                    break
                controller = self.controllers.get(selected.repo_id)
                if controller is None:
                    break
                with self._launch_lock:
                    current = controller.recheck_candidate(selected.work_id)
                    if current is None:
                        continue
                    if not controller.lane_available(current.lane_name):
                        continue
                    blocks = evaluate_cross_repo_dependencies(
                        home=self.home,
                        repo_id=current.repo_id,
                        repo_root=current.repo_root,
                        spec_id=current.spec_id,
                        git_runner=self.git_runner,
                        registry_query=self.registry_query,
                    )
                    if blocks:
                        continue
                    reservation = None
                    try:
                        lease = controller.begin_launch(current.work_id)
                        project_id, release_name, roadmap_index = self._candidate_project(current)
                        reservation = self.governor.reserve_slot(
                            provider=provider,
                            repo_id=current.repo_id,
                            queue_root=controller.queue_root,
                            work_id=current.work_id,
                            attempt_id=lease.attempt_id,
                            lane=lease.lane_name,
                            project_attribution=project_id,
                            release_name=release_name,
                        )
                        self.governor.launch(reservation, command=self.command_builder(current))
                    except Exception as exc:  # noqa: BLE001
                        if reservation is not None:
                            self.governor.release_pre_spawn_failure(reservation.slot_id)
                        controller.revert_launch(current.work_id, error=str(exc))
                        raise
                    AdmissionStore(self.home.root).write_receipt(
                        admission_id=f"admission-{lease.work_id}",
                        repo_id=current.repo_id,
                        spec_id=current.spec_id,
                        project_id=project_id,
                        release_name=release_name,
                        roadmap_index=roadmap_index,
                        work_id=current.work_id,
                        attempt_id=lease.attempt_id,
                        membership=Membership(project_id=project_id, release_name=release_name, roadmap_index=roadmap_index),
                    )
                    launches.append(
                        DrainLaunch(
                            provider=provider,
                            project_id=project_id,
                            repo_id=current.repo_id,
                            work_id=current.work_id,
                            attempt_id=lease.attempt_id,
                            slot_id=reservation.slot_id,
                        )
                    )
        return launches
