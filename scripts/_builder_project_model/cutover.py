from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

FORWARD_STEPS = (
    "stop_legacy",
    "prove_legacy_gone",
    "reconcile_legacy_pgids",
    "start_central",
    "acquire_repo_locks",
    "reconcile_repo_runtime",
    "replace_watchdogs",
)

ROLLBACK_STEPS = (
    "stop_new_central_launches",
    "reconcile_central_groups",
    "stop_central",
    "release_repo_locks",
    "restore_legacy_watchdogs",
    "restore_legacy_daemons",
    "select_legacy_discovery",
)


class CutoverError(RuntimeError):
    pass


@dataclass(frozen=True)
class StepResult:
    step: str
    dry_run: bool
    changed: bool
    details: tuple[str, ...]


@dataclass(frozen=True)
class LaunchRequest:
    session_id: str
    repo_id: str
    provider: str


def load_cutover_state(path: Path) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise CutoverError(f"{path}: expected object state")
    return data


def write_cutover_state(path: Path, state: dict[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _bool(value: Any, *, field: str) -> bool:
    if not isinstance(value, bool):
        raise CutoverError(f"missing-or-invalid {field}")
    return value


def _string(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise CutoverError(f"missing-or-invalid {field}")
    return value


def _int(value: Any, *, field: str) -> int:
    if not isinstance(value, int):
        raise CutoverError(f"missing-or-invalid {field}")
    return value


def _legacy_targets(state: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    repos = state.get("legacy_daemons", {})
    if not isinstance(repos, dict):
        raise CutoverError("missing-or-invalid legacy_daemons")
    targets = [(f"legacy:{repo_id}", row) for repo_id, row in sorted(repos.items())]
    group_runner = state.get("group_runner")
    if group_runner is not None:
        if not isinstance(group_runner, dict):
            raise CutoverError("missing-or-invalid group_runner")
        targets.append(("group-runner", group_runner))
    return targets


def _require_identity_ok(row: dict[str, Any], *, label: str) -> None:
    if not _bool(row.get("identity_ok"), field=f"{label}.identity_ok"):
        raise CutoverError(f"identity-mismatch:{label}")


def _validate_pgid_record(row: dict[str, Any], *, label: str) -> None:
    _require_identity_ok(row, label=label)
    pgid = _int(row.get("pgid"), field=f"{label}.pgid")
    if pgid <= 1:
        raise CutoverError(f"unsafe-pgid:{label}")
    _int(row.get("pgid_leader_start_ticks"), field=f"{label}.pgid_leader_start_ticks")
    _string(row.get("executable"), field=f"{label}.executable")
    _string(row.get("command_digest"), field=f"{label}.command_digest")


class SyntheticCutoverOperator:
    def __init__(self, state_path: Path):
        self.state_path = Path(state_path).resolve()
        self._transient_state: dict[str, Any] | None = None

    def load(self) -> dict[str, Any]:
        return load_cutover_state(self.state_path)

    def save(self, state: dict[str, Any]) -> None:
        write_cutover_state(self.state_path, state)

    def _mutate(self, *, dry_run: bool, mutator) -> StepResult:
        state = self._transient_state if self._transient_state is not None else self.load()
        result = mutator(state)
        if dry_run and self._transient_state is not None:
            self._transient_state = state
        elif not dry_run and result.changed:
            self.save(state)
        return result

    def _apply_now(self, dry_run: bool) -> bool:
        return not dry_run or self._transient_state is not None

    def stop_legacy(self, *, dry_run: bool) -> StepResult:
        def mutator(state: dict[str, Any]) -> StepResult:
            apply_now = self._apply_now(dry_run)
            details: list[str] = []
            changed = False
            for label, row in _legacy_targets(state):
                _require_identity_ok(row, label=label)
                if _bool(row.get("alive"), field=f"{label}.alive"):
                    details.append(f"stop:{label}")
                    changed = True
                    if apply_now:
                        row["alive"] = False
            return StepResult("stop_legacy", dry_run, changed and not dry_run, tuple(details or ["legacy-already-stopped"]))

        return self._mutate(dry_run=dry_run, mutator=mutator)

    def prove_legacy_gone(self, *, dry_run: bool) -> StepResult:
        def mutator(state: dict[str, Any]) -> StepResult:
            details: list[str] = []
            for label, row in _legacy_targets(state):
                _require_identity_ok(row, label=label)
                if _bool(row.get("alive"), field=f"{label}.alive"):
                    raise CutoverError(f"legacy-owner-still-live:{label}")
                details.append(f"gone:{label}")
            return StepResult("prove_legacy_gone", dry_run, False, tuple(details))

        return self._mutate(dry_run=dry_run, mutator=mutator)

    def reconcile_legacy_pgids(self, *, dry_run: bool) -> StepResult:
        def mutator(state: dict[str, Any]) -> StepResult:
            apply_now = self._apply_now(dry_run)
            rows = state.get("legacy_pgids", [])
            if not isinstance(rows, list):
                raise CutoverError("missing-or-invalid legacy_pgids")
            details: list[str] = []
            changed = False
            for index, row in enumerate(rows):
                if not isinstance(row, dict):
                    raise CutoverError("missing-or-invalid legacy_pgids entry")
                label = f"legacy-pgid:{index}"
                _validate_pgid_record(row, label=label)
                if _bool(row.get("alive"), field=f"{label}.alive"):
                    details.append(f"reconcile:{label}")
                    changed = True
                    if apply_now:
                        row["alive"] = False
                        row["reconciled"] = True
            return StepResult("reconcile_legacy_pgids", dry_run, changed and not dry_run, tuple(details or ["no-live-legacy-pgids"]))

        return self._mutate(dry_run=dry_run, mutator=mutator)

    def start_central(self, *, dry_run: bool) -> StepResult:
        def mutator(state: dict[str, Any]) -> StepResult:
            apply_now = self._apply_now(dry_run)
            for label, row in _legacy_targets(state):
                if _bool(row.get("alive"), field=f"{label}.alive"):
                    raise CutoverError(f"dual-owner-refused:{label}")
            central = state.get("central")
            if not isinstance(central, dict):
                raise CutoverError("missing-or-invalid central")
            if _bool(central.get("daemon_alive"), field="central.daemon_alive"):
                raise CutoverError("central-daemon-already-live")
            if apply_now:
                central["daemon_alive"] = True
                central["launch_enabled"] = True
            return StepResult("start_central", dry_run, not dry_run, ("start:central-daemon",))

        return self._mutate(dry_run=dry_run, mutator=mutator)

    def acquire_repo_locks(self, *, dry_run: bool) -> StepResult:
        def mutator(state: dict[str, Any]) -> StepResult:
            apply_now = self._apply_now(dry_run)
            repo_locks = state.get("repo_locks", {})
            if not isinstance(repo_locks, dict):
                raise CutoverError("missing-or-invalid repo_locks")
            details: list[str] = []
            changed = False
            for repo_id in sorted(repo_locks):
                owner = repo_locks[repo_id]
                if owner in {"legacy", "other"}:
                    raise CutoverError(f"dual-owner-refused:repo-lock:{repo_id}:{owner}")
                if owner == "central":
                    details.append(f"already-owned:{repo_id}")
                    continue
                details.append(f"acquire:{repo_id}")
                changed = True
                if apply_now:
                    repo_locks[repo_id] = "central"
            return StepResult("acquire_repo_locks", dry_run, changed and not dry_run, tuple(details))

        return self._mutate(dry_run=dry_run, mutator=mutator)

    def reconcile_repo_runtime(self, *, dry_run: bool) -> StepResult:
        def mutator(state: dict[str, Any]) -> StepResult:
            central = state.get("central")
            if not isinstance(central, dict) or not _bool(central.get("daemon_alive"), field="central.daemon_alive"):
                raise CutoverError("central-daemon-not-live")
            repo_locks = state.get("repo_locks", {})
            sessions = state.get("sessions", [])
            providers = state.get("providers", {})
            if not isinstance(repo_locks, dict) or not isinstance(sessions, list) or not isinstance(providers, dict):
                raise CutoverError("missing cutover runtime state")
            usage: dict[str, int] = {}
            details: list[str] = []
            for session in sessions:
                if not isinstance(session, dict):
                    raise CutoverError("missing-or-invalid session entry")
                owner = _string(session.get("owner"), field="session.owner")
                repo_id = _string(session.get("repo_id"), field="session.repo_id")
                provider = _string(session.get("provider"), field="session.provider")
                consuming = _bool(session.get("consuming"), field="session.consuming")
                if owner == "legacy" and consuming and repo_locks.get(repo_id) == "central":
                    raise CutoverError(f"dual-owner-refused:session:{repo_id}:{provider}")
                if consuming and session.get("state") in {"starting", "active", "reaping"}:
                    usage[provider] = usage.get(provider, 0) + 1
            for provider, count in sorted(usage.items()):
                provider_row = providers.get(provider)
                if not isinstance(provider_row, dict):
                    raise CutoverError(f"missing provider config for {provider}")
                cap = _int(provider_row.get("max_sessions"), field=f"providers.{provider}.max_sessions")
                if count > cap:
                    raise CutoverError(f"provider-cap-exceeded:{provider}:{count}>{cap}")
                details.append(f"capacity:{provider}:{count}/{cap}")
            return StepResult("reconcile_repo_runtime", dry_run, False, tuple(details or ["capacity:empty"]))

        return self._mutate(dry_run=dry_run, mutator=mutator)

    def replace_watchdogs(self, *, dry_run: bool) -> StepResult:
        def mutator(state: dict[str, Any]) -> StepResult:
            apply_now = self._apply_now(dry_run)
            active = state.get("active_watchdogs", [])
            if not isinstance(active, list):
                raise CutoverError("missing-or-invalid active_watchdogs")
            changed = active != ["central"]
            if apply_now and changed:
                if not state.get("rollback_legacy_watchdogs"):
                    state["rollback_legacy_watchdogs"] = list(active)
                state["active_watchdogs"] = ["central"]
                central = state.get("central")
                if not isinstance(central, dict):
                    raise CutoverError("missing-or-invalid central")
                central["watchdog_alive"] = True
                state["discovery_mode"] = "home"
            return StepResult("replace_watchdogs", dry_run, changed and not dry_run, ("watchdogs:central",))

        return self._mutate(dry_run=dry_run, mutator=mutator)

    def stop_new_central_launches(self, *, dry_run: bool) -> StepResult:
        def mutator(state: dict[str, Any]) -> StepResult:
            apply_now = self._apply_now(dry_run)
            central = state.get("central")
            if not isinstance(central, dict):
                raise CutoverError("missing-or-invalid central")
            changed = _bool(central.get("launch_enabled"), field="central.launch_enabled")
            if apply_now and changed:
                central["launch_enabled"] = False
            return StepResult("stop_new_central_launches", dry_run, changed and not dry_run, ("central-launches-disabled",))

        return self._mutate(dry_run=dry_run, mutator=mutator)

    def reconcile_central_groups(self, *, dry_run: bool) -> StepResult:
        def mutator(state: dict[str, Any]) -> StepResult:
            apply_now = self._apply_now(dry_run)
            sessions = state.get("sessions", [])
            if not isinstance(sessions, list):
                raise CutoverError("missing-or-invalid sessions")
            details: list[str] = []
            changed = False
            for session in sessions:
                if not isinstance(session, dict):
                    raise CutoverError("missing-or-invalid session entry")
                if session.get("owner") != "central":
                    continue
                _require_identity_ok(session, label=f"session:{session.get('session_id')}")
                if _bool(session.get("consuming"), field="session.consuming"):
                    details.append(f"reconcile-central:{session.get('session_id')}")
                    changed = True
                    if apply_now:
                        session["consuming"] = False
                        session["state"] = "closed"
            return StepResult("reconcile_central_groups", dry_run, changed and not dry_run, tuple(details or ["no-central-groups"]))

        return self._mutate(dry_run=dry_run, mutator=mutator)

    def stop_central(self, *, dry_run: bool) -> StepResult:
        def mutator(state: dict[str, Any]) -> StepResult:
            apply_now = self._apply_now(dry_run)
            central = state.get("central")
            if not isinstance(central, dict):
                raise CutoverError("missing-or-invalid central")
            changed = _bool(central.get("daemon_alive"), field="central.daemon_alive") or _bool(
                central.get("watchdog_alive"), field="central.watchdog_alive"
            )
            if apply_now:
                central["daemon_alive"] = False
                central["watchdog_alive"] = False
            return StepResult("stop_central", dry_run, changed and not dry_run, ("central-stopped",))

        return self._mutate(dry_run=dry_run, mutator=mutator)

    def release_repo_locks(self, *, dry_run: bool) -> StepResult:
        def mutator(state: dict[str, Any]) -> StepResult:
            apply_now = self._apply_now(dry_run)
            repo_locks = state.get("repo_locks", {})
            if not isinstance(repo_locks, dict):
                raise CutoverError("missing-or-invalid repo_locks")
            details: list[str] = []
            changed = False
            for repo_id, owner in sorted(repo_locks.items()):
                if owner == "central":
                    details.append(f"release:{repo_id}")
                    changed = True
                    if apply_now:
                        repo_locks[repo_id] = None
            return StepResult("release_repo_locks", dry_run, changed and not dry_run, tuple(details or ["no-central-locks"]))

        return self._mutate(dry_run=dry_run, mutator=mutator)

    def restore_legacy_watchdogs(self, *, dry_run: bool) -> StepResult:
        def mutator(state: dict[str, Any]) -> StepResult:
            apply_now = self._apply_now(dry_run)
            backups = state.get("rollback_legacy_watchdogs", [])
            if not isinstance(backups, list):
                raise CutoverError("missing-or-invalid rollback_legacy_watchdogs")
            changed = bool(backups)
            if apply_now:
                state["active_watchdogs"] = list(backups)
            return StepResult("restore_legacy_watchdogs", dry_run, changed and not dry_run, tuple(backups or ["no-legacy-watchdogs-recorded"]))

        return self._mutate(dry_run=dry_run, mutator=mutator)

    def restore_legacy_daemons(self, *, dry_run: bool) -> StepResult:
        def mutator(state: dict[str, Any]) -> StepResult:
            apply_now = self._apply_now(dry_run)
            details: list[str] = []
            changed = False
            for label, row in _legacy_targets(state):
                restore = _bool(row.get("restore_on_rollback"), field=f"{label}.restore_on_rollback")
                if restore and not _bool(row.get("alive"), field=f"{label}.alive"):
                    details.append(f"restore:{label}")
                    changed = True
                    if apply_now:
                        row["alive"] = True
            return StepResult("restore_legacy_daemons", dry_run, changed and not dry_run, tuple(details or ["no-legacy-daemons-restored"]))

        return self._mutate(dry_run=dry_run, mutator=mutator)

    def select_legacy_discovery(self, *, dry_run: bool) -> StepResult:
        def mutator(state: dict[str, Any]) -> StepResult:
            apply_now = self._apply_now(dry_run)
            changed = state.get("discovery_mode") != "legacy"
            if apply_now:
                state["discovery_mode"] = "legacy"
            return StepResult("select_legacy_discovery", dry_run, changed and not dry_run, ("discovery:legacy",))

        return self._mutate(dry_run=dry_run, mutator=mutator)

    def admit_central_launches(self, requests: list[LaunchRequest], *, dry_run: bool) -> StepResult:
        def mutator(state: dict[str, Any]) -> StepResult:
            apply_now = self._apply_now(dry_run)
            central = state.get("central")
            repo_locks = state.get("repo_locks", {})
            providers = state.get("providers", {})
            sessions = state.get("sessions", [])
            if not isinstance(central, dict) or not isinstance(repo_locks, dict) or not isinstance(providers, dict) or not isinstance(sessions, list):
                raise CutoverError("missing cutover runtime state")
            if not _bool(central.get("daemon_alive"), field="central.daemon_alive"):
                raise CutoverError("central-daemon-not-live")
            if not _bool(central.get("launch_enabled"), field="central.launch_enabled"):
                raise CutoverError("central-launches-disabled")
            usage: dict[str, int] = {}
            for session in sessions:
                if isinstance(session, dict) and session.get("owner") == "central" and session.get("consuming") and session.get("state") in {"starting", "active", "reaping"}:
                    provider = _string(session.get("provider"), field="session.provider")
                    usage[provider] = usage.get(provider, 0) + 1
            details: list[str] = []
            changed = False
            for request in requests:
                if repo_locks.get(request.repo_id) != "central":
                    raise CutoverError(f"repo-not-owned-by-central:{request.repo_id}")
                provider_row = providers.get(request.provider)
                if not isinstance(provider_row, dict):
                    raise CutoverError(f"missing provider config for {request.provider}")
                cap = _int(provider_row.get("max_sessions"), field=f"providers.{request.provider}.max_sessions")
                used = usage.get(request.provider, 0)
                if used >= cap:
                    details.append(f"refused-cap:{request.provider}:{request.session_id}")
                    continue
                usage[request.provider] = used + 1
                details.append(f"admitted:{request.provider}:{request.session_id}")
                changed = True
                if apply_now:
                    sessions.append(
                        {
                            "session_id": request.session_id,
                            "repo_id": request.repo_id,
                            "provider": request.provider,
                            "owner": "central",
                            "state": "active",
                            "consuming": True,
                            "identity_ok": True,
                        }
                    )
            return StepResult("admit_central_launches", dry_run, changed and not dry_run, tuple(details))

        return self._mutate(dry_run=dry_run, mutator=mutator)


class CutoverOrchestrator:
    def __init__(self, operator: SyntheticCutoverOperator, *, dry_run: bool = True, confirmations: set[str] | None = None):
        self.operator = operator
        self.dry_run = dry_run
        self.confirmations = confirmations or set()

    def _require_confirmation(self, step: str) -> None:
        if not self.dry_run and step not in self.confirmations:
            raise CutoverError(f"missing-confirmation:{step}")

    def _run_named_steps(self, names: tuple[str, ...]) -> list[StepResult]:
        results: list[StepResult] = []
        if self.dry_run:
            self.operator._transient_state = self.operator.load()
        try:
            for name in names:
                self._require_confirmation(name)
                results.append(getattr(self.operator, name)(dry_run=self.dry_run))
            return results
        finally:
            self.operator._transient_state = None

    def run_forward(self, *, steps: tuple[str, ...] | None = None) -> list[StepResult]:
        return self._run_named_steps(FORWARD_STEPS if steps is None else steps)

    def run_rollback(self, *, steps: tuple[str, ...] | None = None) -> list[StepResult]:
        return self._run_named_steps(ROLLBACK_STEPS if steps is None else steps)


def render_cutover_results(results: list[StepResult]) -> str:
    lines: list[str] = []
    for result in results:
        mode = "dry-run" if result.dry_run else "applied"
        changed = "changed" if result.changed else "no-change"
        lines.append(f"{result.step}: {mode} {changed}")
        for detail in result.details:
            lines.append(f"  - {detail}")
    return "\n".join(lines) + ("\n" if lines else "")
