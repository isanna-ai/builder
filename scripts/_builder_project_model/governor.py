from __future__ import annotations

import os
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from _dispatch_runtime.lane_common import _kill_group, _pgid_group_alive, _proc_identity, _reap_group

from .home import BuilderHome
from .launcher_shim import command_digest, spawn_launcher, wait_for_slot_identity
from .session_store import SessionStore

GLOBAL_COOLDOWN_CLASSES = {"subscription", "quota", "rate-limit"}


def governor_enabled(home: BuilderHome | None) -> bool:
    return bool(home is not None and home.policy.governor_enabled)


def current_pid_start_ticks(pid: int | None = None) -> int:
    proc = pid or os.getpid()
    ident = _proc_identity(proc)
    if ident is None:
        raise RuntimeError(f"unable to read process identity for pid {proc}")
    return int(ident[0])


def current_executable(pid: int | None = None) -> str:
    """The cmdline[0] of a process — the identity token the live-cutover operator
    matches against. The central daemon must record this so its own daemon record is
    identity-verifiable the same way session/legacy records are."""
    proc = pid or os.getpid()
    ident = _proc_identity(proc)
    if ident is None:
        raise RuntimeError(f"unable to read process identity for pid {proc}")
    argv = ident[1].split()
    if not argv:
        raise RuntimeError(f"empty cmdline for pid {proc}")
    return argv[0]


def _pid_matches(pid: int | None, start_ticks: int | None) -> bool:
    if pid is None or start_ticks is None:
        return False
    live = _proc_identity(pid)
    if live is None:
        return False
    return int(live[0]) == int(start_ticks)


def _identity_matches(record: dict[str, Any]) -> bool:
    pgid = record.get("pgid")
    if not isinstance(pgid, int) or pgid <= 1:
        return False
    if not _pgid_group_alive(pgid):
        return False
    live = _proc_identity(pgid)
    if live is None:
        return False
    start_ticks, cmdline = live
    if str(record.get("pgid_leader_start_ticks") or "") != str(start_ticks):
        return False
    executable = str(record.get("executable") or "")
    if not executable:
        return False
    argv = cmdline.split()
    if not argv or argv[0] != executable:
        return False
    digest = record.get("command_digest")
    return digest == command_digest(argv)


@dataclass(frozen=True)
class GovernorReservation:
    slot_id: str
    provider: str
    record: dict[str, Any]


@dataclass(frozen=True)
class ReconcileAction:
    slot_id: str
    action: str
    detail: str


class BuilderGovernor:
    def __init__(self, home: BuilderHome, *, daemon_instance_id: str | None = None, owner_pid: int | None = None):
        self.home = home
        self.store = SessionStore(home.root)
        self.daemon_instance_id = daemon_instance_id or str(uuid4())
        self.owner_pid = owner_pid or os.getpid()
        self.owner_pid_start_ticks = current_pid_start_ticks(self.owner_pid)
        self._processes: dict[str, Any] = {}

    def reserve_slot(
        self,
        *,
        provider: str,
        repo_id: str,
        queue_root: Path,
        work_id: str,
        attempt_id: str,
        lane: str,
        project_attribution: str,
        release_name: str | None,
        crash_hook=None,
    ) -> GovernorReservation:
        max_sessions = self.home.policy.providers[provider].max_sessions
        record = self.store.reserve_slot(
            provider=provider,
            max_sessions=max_sessions,
            daemon_instance_id=self.daemon_instance_id,
            owner_pid=self.owner_pid,
            owner_pid_start_ticks=self.owner_pid_start_ticks,
            repo_id=repo_id,
            queue_root=queue_root,
            work_id=work_id,
            attempt_id=attempt_id,
            lane=lane,
            project_attribution=project_attribution,
            release_name=release_name,
            crash_hook=crash_hook,
        )
        return GovernorReservation(slot_id=record["slot_id"], provider=provider, record=record)

    def launch(
        self,
        reservation: GovernorReservation,
        *,
        command: list[str],
        crash_phase: str | None = None,
        timeout_seconds: float = 5.0,
    ):
        proc = spawn_launcher(
            home_root=self.home.root,
            slot_id=reservation.slot_id,
            command=command,
            crash_phase=crash_phase,
        )
        try:
            wait_for_slot_identity(self.store, reservation.slot_id, timeout_seconds=timeout_seconds)
        except TimeoutError:
            # No pgid was recorded within the identity deadline. That's expected when the
            # launcher is dying before it ever writes one (e.g. a pre-pgid crash) rather than
            # truly hung -- but an instantaneous proc.poll() sample right at the deadline races
            # the launcher's own exit: under load, interpreter startup for the launcher subprocess
            # can still be finishing at the exact moment the deadline fires, so poll() observes
            # "still running" a few milliseconds before it exits on its own. Wait for the real
            # condition (process termination) with its own bound instead of trusting one sample:
            # only a launcher that is still alive after this second bounded wait is actually hung.
            # The grace period has its own floor (independent of a caller's possibly-tiny
            # identity timeout) so a slow-to-schedule-but-already-exiting launcher under load
            # gets a real chance to be observed dead rather than merely doubling a sub-second bound.
            grace_seconds = max(timeout_seconds, 2.0)
            try:
                proc.wait(timeout=grace_seconds)
            except subprocess.TimeoutExpired:
                raise TimeoutError(
                    f"launcher for slot {reservation.slot_id} neither recorded an identity "
                    f"within {timeout_seconds}s nor exited within a further {grace_seconds}s"
                ) from None
        self._processes[reservation.slot_id] = proc
        return proc

    def release_pre_spawn_failure(self, slot_id: str) -> None:
        self.store.close_session(slot_id)

    def begin_reaping(self, slot_id: str) -> dict[str, Any]:
        return self.store.update_state(slot_id, state="reaping", previous_state="active")

    def reap_session(self, slot_id: str, *, terminate: bool = True) -> ReconcileAction:
        record = self.store.load_session(slot_id)
        proc = self._processes.get(slot_id)
        pgid = record.get("pgid")
        if not isinstance(pgid, int) or pgid <= 1:
            return ReconcileAction(slot_id, "quarantine", "missing-or-unsafe-pgid")
        if proc is not None and proc.poll() is not None:
            proc.wait()
        if not _pgid_group_alive(pgid):
            self._processes.pop(slot_id, None)
            self.store.close_session(slot_id)
            return ReconcileAction(slot_id, "closed", "group-already-gone")
        if not _identity_matches(record):
            if record.get("state") == "reaping":
                return ReconcileAction(slot_id, "reaping", "group-still-alive")
            return ReconcileAction(slot_id, "quarantine", "identity-mismatch")
        if terminate and _pgid_group_alive(pgid):
            self.store.update_state(slot_id, state="reaping", previous_state=record["state"])
            _reap_group(pgid)
            if proc is not None:
                proc.wait()
        if _pgid_group_alive(pgid):
            return ReconcileAction(slot_id, "reaping", "group-still-alive")
        self._processes.pop(slot_id, None)
        self.store.close_session(slot_id)
        return ReconcileAction(slot_id, "closed", "group-reaped")

    def open_provider_cooldown(
        self,
        provider: str,
        *,
        reason_class: str,
        source_repo_id: str | None,
        source_attempt_id: str | None,
        cooldown_until: str | None,
    ) -> bool:
        if reason_class not in GLOBAL_COOLDOWN_CLASSES:
            return False
        self.store.write_provider(
            provider,
            cooldown_until=cooldown_until,
            reason_class=reason_class,
            source_repo_id=source_repo_id,
            source_attempt_id=source_attempt_id,
        )
        return True

    def reconcile_startup(self) -> list[ReconcileAction]:
        actions: list[ReconcileAction] = []
        for record in self.store.list_sessions():
            slot_id = str(record["slot_id"])
            try:
                queue_root = Path(str(record["queue_root"]))
                if not queue_root.is_absolute():
                    raise ValueError("queue_root must be absolute")
            except Exception as exc:  # noqa: BLE001
                actions.append(ReconcileAction(slot_id, "quarantine", f"invalid-session:{exc}"))
                continue

            if _pid_matches(int(record.get("owner_pid") or 0), int(record.get("owner_pid_start_ticks") or 0)):
                actions.append(ReconcileAction(slot_id, "dual-owner-refused", "recorded-daemon-instance-live"))
                continue

            if record["state"] == "starting" and record.get("pgid") is None:
                launcher = self.store.read_launcher_state(slot_id)
                if not launcher:
                    actions.append(ReconcileAction(slot_id, "quarantine", "starting-without-launcher-proof"))
                    continue
                launcher_live = _pid_matches(int(launcher.get("pid") or 0), int(launcher.get("pid_start_ticks") or 0))
                if launcher_live:
                    actions.append(ReconcileAction(slot_id, "quarantine", "launcher-still-live"))
                    continue
                if launcher.get("phase") == "started":
                    self.store.close_session(slot_id)
                    actions.append(ReconcileAction(slot_id, "closed", "starting-no-pgid-proven-no-child"))
                else:
                    actions.append(ReconcileAction(slot_id, "quarantine", "starting-without-pgid-unproven"))
                continue

            if record.get("pgid") is not None:
                pgid = int(record["pgid"])
                if not _pgid_group_alive(pgid):
                    self.store.close_session(slot_id)
                    actions.append(ReconcileAction(slot_id, "closed", "recorded-group-already-gone"))
                    continue
                if not _identity_matches(record):
                    actions.append(ReconcileAction(slot_id, "quarantine", "pgid-identity-mismatch"))
                    continue
                action = self.reap_session(slot_id, terminate=True)
                actions.append(action)
                continue

            actions.append(ReconcileAction(slot_id, "quarantine", "unhandled-session-shape"))
        return actions
