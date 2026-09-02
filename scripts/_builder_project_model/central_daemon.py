"""Opt-in central-home daemon built around the existing governor and drainer."""

from __future__ import annotations

import hashlib
import json
import os
import signal
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable
from uuid import uuid4

from _dispatch_runtime.scheduler import SchedulerBusyError
from _dispatch_runtime.state_model import WorkItemState

from .central_scheduler import HomeLockHandle, acquire_home_lock
from .draining import FederatedDrainer
from .governor import BuilderGovernor, current_executable, current_pid_start_ticks
from .home import BuilderHome, lint_loaded_home, load_builder_home
from .live_command import live_command_builder
from .live_runtime import CENTRAL_OWNER_PREFIX, live_activation
from .session_store import SessionStore, _atomic_unlink, _atomic_write_json


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _next_sleep(poll_seconds: float, idle_ticks: int, launches: list, sessions: list) -> tuple[float, int]:
    """perf(central): a fully-idle central daemon (no launches, no live sessions)
    otherwise busy-polls at poll_seconds forever. Stretch the sleep
    geometrically up to 30s while idle; any launch or live session resets to
    poll_seconds so a newly-enqueued item is never held past its normal
    poll cadence once work actually starts flowing. Pure/no wall-clock so it's
    unit-testable directly; CentralDaemon.run() owns the idle_ticks counter.
    Returns (sleep_seconds, next_idle_ticks).
    """
    if launches or sessions:
        return poll_seconds, 0
    sleep_seconds = min(30.0, poll_seconds * (2 ** idle_ticks))
    return sleep_seconds, idle_ticks + 1


def _expired(stamp: object) -> bool:
    if not stamp:
        return False
    try:
        parsed = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed <= datetime.now(timezone.utc)


def snapshot_digest(home: BuilderHome) -> str:
    paths = [home.builder_path, home.manifest.repositories, home.manifest.policy]
    for project in home.projects:
        paths.append(project.manifest_path)
        paths.extend(release.manifest_path for release in project.releases)
    digest = hashlib.sha256()
    for path in sorted({Path(path).resolve() for path in paths}, key=str):
        digest.update(str(path.relative_to(home.root)).encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return "sha256:" + digest.hexdigest()


def load_valid_snapshot(home_root: Path) -> tuple[BuilderHome, str]:
    home = load_builder_home(home_root)
    findings = lint_loaded_home(home)
    if findings:
        raise ValueError("; ".join(findings))
    if not live_activation(home):
        raise RuntimeError(
            "central daemon inactive: requires Builder Home, governor.enabled=true, "
            "and non-empty governor.drain_repos"
        )
    return home, snapshot_digest(home)


class CentralDaemon:
    def __init__(
        self,
        home_root: Path,
        *,
        poll_seconds: float | None = None,
        shutdown_timeout: float = 2100.0,
        governor_factory: Callable[..., BuilderGovernor] = BuilderGovernor,
        drainer_factory: Callable[..., FederatedDrainer] = FederatedDrainer,
        crash_hook=None,
    ):
        self.home_root = Path(home_root).resolve()
        self.home, self.config_digest = load_valid_snapshot(self.home_root)
        self.poll_seconds = float(poll_seconds or self.home.policy.scheduler["poll_seconds"])
        self.shutdown_timeout = shutdown_timeout
        self.instance_id = str(uuid4())
        # Existing lock recovery extracts the final dash-delimited token as pid.
        self.owner_id = f"{CENTRAL_OWNER_PREFIX}{self.instance_id}-dispatch-{os.getpid()}"
        self.governor = governor_factory(self.home, daemon_instance_id=self.instance_id)
        self.drainer = drainer_factory(
            self.home,
            owner_id=self.owner_id,
            governor=self.governor,
            command_builder=live_command_builder,
        )
        self.store = SessionStore(self.home.root)
        self.lock: HomeLockHandle | None = None
        self.stop_requested = False
        self.findings: list[str] = []
        self.crash_hook = crash_hook
        self._started = False
        self._idle_ticks = 0

    @property
    def daemon_path(self) -> Path:
        return self.store.paths.daemon_path

    def _crash(self, phase: str) -> None:
        if self.crash_hook is not None:
            self.crash_hook(phase, self)

    def _daemon_payload(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "pid": os.getpid(),
            "pid_start_ticks": current_pid_start_ticks(),
            "executable": current_executable(),
            "daemon_instance_id": self.instance_id,
            "started_at": getattr(self, "started_at", _utc_now()),
            "heartbeat_at": _utc_now(),
            "config_digest": self.config_digest,
            "drain_repos": list(self.home.policy.drain_repos),
            "findings": list(dict.fromkeys(self.findings + self.drainer.findings)),
        }

    def heartbeat(self) -> None:
        _atomic_write_json(self.daemon_path, self._daemon_payload())

    def _finalize_marker(self, record: dict[str, object]) -> Path:
        return self.home.root / "state" / "finalize" / f"{record['slot_id']}.json"

    def _stage_finalization(self, record: dict[str, object]) -> None:
        _atomic_write_json(self._finalize_marker(record), dict(record))

    def _recover_finalizations(self) -> None:
        pending = self.home.root / "state" / "finalize"
        if not pending.is_dir():
            return
        for path in sorted(pending.glob("*.json")):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
                if self.store.paths.session_path(str(record["slot_id"])).exists():
                    continue
                self._finalize_interrupted(record, "recovered-after-reap")
                _atomic_unlink(path)
            except Exception as exc:  # noqa: BLE001
                self.findings.append(f"finalization recovery refused for {path.name}: {exc}")

    def start(self) -> None:
        if self._started:
            return
        self.lock = acquire_home_lock(self.home.root, owner_id=self.owner_id)
        try:
            self.started_at = _utc_now()
            self.heartbeat()
            prior = {str(row["slot_id"]): row for row in self.store.list_sessions()}
            for action in self.governor.reconcile_startup():
                if action.action == "closed" and action.slot_id in prior:
                    self._finalize_interrupted(prior[action.slot_id], action.detail)
                if action.action in {"quarantine", "dual-owner-refused", "reaping"}:
                    self.findings.append(f"session {action.slot_id}: {action.action}: {action.detail}")
            self._recover_finalizations()
            self.findings.extend(self.drainer.start())
            self._started = True
            self.heartbeat()
        except BaseException:
            if self.lock is not None:
                self.lock.release()
                self.lock = None
            raise

    def _reload_snapshot(self) -> None:
        self._crash("during_snapshot_reload")
        try:
            candidate, digest = load_valid_snapshot(self.home_root)
        except Exception as exc:  # noqa: BLE001
            finding = f"snapshot reload refused; keeping last good snapshot: {exc}"
            if finding not in self.findings:
                self.findings.append(finding)
            return
        if digest == self.config_digest:
            return
        old_scope = {
            repo_id: self.home.repo_roots_by_id.get(repo_id)
            for repo_id in self.home.policy.drain_repos
        }
        new_scope = {
            repo_id: candidate.repo_roots_by_id.get(repo_id)
            for repo_id in candidate.policy.drain_repos
        }
        # Cap/cooldown changes can be adopted without releasing ownership. A
        # repo-scope change while a group is in flight is held until the fleet is
        # empty, avoiding a lock-release window around a still-running attempt.
        if old_scope == new_scope:
            self.home = candidate
            self.config_digest = digest
            self.governor.home = candidate
            self.drainer.home = candidate
            return
        if self.store.list_sessions():
            finding = "snapshot reload deferred: repo ownership scope changed while sessions are in flight"
            if finding not in self.findings:
                self.findings.append(finding)
            return
        # Swap only after the whole candidate parsed/linted and no group can be
        # exposed by the lock handoff.
        self.drainer.stop()
        self.home = candidate
        self.config_digest = digest
        self.governor.home = candidate
        self.drainer = FederatedDrainer(
            candidate,
            owner_id=self.owner_id,
            governor=self.governor,
            command_builder=live_command_builder,
        )
        self.findings.extend(self.drainer.start())

    def _expire_cooldowns(self) -> None:
        for provider in self.home.policy.providers:
            row = self.store.read_provider(provider)
            if _expired(row.get("cooldown_until")):
                self.store.write_provider(
                    provider,
                    cooldown_until=None,
                    reason_class=None,
                    source_repo_id=None,
                    source_attempt_id=None,
                )

    def _propagate_provider_cooldown(self, record: dict[str, object]) -> None:
        """Promote only this central attempt's repo-local rate limit to provider scope."""

        try:
            from _dispatch_runtime.queue_store import QueueStore

            queue = QueueStore(Path(str(record["queue_root"])))
            snapshot = queue.reconstruct()
            attempt_id = str(record.get("attempt_id") or "")
            attempt = snapshot.attempts.get(attempt_id)
            if attempt is None or attempt.work_id != str(record.get("work_id") or ""):
                return
            if attempt.metadata.get("decision") != "rate-limit-cooldown":
                return
            lane = snapshot.lanes.get(str(record.get("lane") or ""))
            cooldown_until = None if lane is None else lane.cooldown_until
            if not cooldown_until:
                self.findings.append(
                    f"provider cooldown refused for attempt {attempt_id}: "
                    "matching repo lane cooldown is missing"
                )
                return
            candidate_deadline = datetime.fromisoformat(str(cooldown_until).replace("Z", "+00:00"))
            provider = str(record.get("provider") or "")
            current = self.store.read_provider(provider)
            current_until = current.get("cooldown_until")
            if current_until:
                try:
                    current_deadline = datetime.fromisoformat(str(current_until).replace("Z", "+00:00"))
                except ValueError:
                    self.findings.append(
                        f"provider cooldown refused for {provider}: existing global cooldown is invalid"
                    )
                    return
                if current_deadline >= candidate_deadline:
                    return
            self.governor.open_provider_cooldown(
                provider,
                reason_class="rate-limit",
                source_repo_id=str(record.get("repo_id") or "") or None,
                source_attempt_id=attempt_id,
                cooldown_until=str(cooldown_until),
            )
        except Exception as exc:  # noqa: BLE001
            self.findings.append(
                f"unable to propagate provider cooldown for session {record.get('slot_id')}: {exc}"
            )

    def _finalize_interrupted(self, record: dict[str, object], detail: str) -> None:
        self._propagate_provider_cooldown(record)
        try:
            from _dispatch_runtime.queue_store import QueueStore

            queue = QueueStore(Path(str(record["queue_root"])))
            item = queue.get_item(str(record["work_id"]))
            if item is None or item.state != WorkItemState.RUNNING:
                return
            if str(item.lease.get("attempt_id") or "") != str(record.get("attempt_id") or ""):
                return
            # Only "closed" actions reach here (quarantine/dual-owner stay as
            # findings without touching the item) - a proven-dead session group
            # is safe to requeue with backoff rather than stranding the spec as
            # a silent terminal FAILED. Bounded by max_attempts so a
            # persistently-crashing attempt still dead-ends eventually.
            if item.attempt < item.max_attempts:
                backoff_seconds = min(900, 60 * 2 ** max(0, item.attempt - 1))
                item.state = WorkItemState.QUEUED
                item.lease = {}
                item.scheduled_after = (
                    (datetime.now(timezone.utc) + timedelta(seconds=backoff_seconds))
                    .isoformat()
                    .replace("+00:00", "Z")
                )
                item.task_ref["last_error"] = f"central attempt interrupted; requeued: {detail}"
                disposition = "requeued"
            else:
                item.state = WorkItemState.FAILED
                item.lease = {}
                item.scheduled_after = None
                item.task_ref["last_error"] = f"central attempt interrupted; retry budget exhausted: {detail}"
                disposition = "failed"
            queue.save_item(item)
            # Always record a finding - previously a FAILED finalization was
            # completely silent, and a requeue must be just as visible.
            self.findings.append(f"session {record.get('slot_id')}: interrupted ({detail}); {disposition}")
            self._notify_interrupted(record, item, detail=detail, disposition=disposition)
        except Exception as exc:  # noqa: BLE001
            self.findings.append(f"unable to finalize interrupted session {record.get('slot_id')}: {exc}")

    def _notify_interrupted(self, record: dict[str, object], item, *, detail: str, disposition: str) -> None:
        # Notifier misconfiguration must never block finalization - own try/except.
        try:
            from _dispatch_runtime.notifier import build_notifier

            from .eligibility import load_repo_dispatch_config

            repo_id = str(record.get("repo_id") or "")
            repo_root = self.home.repo_roots_by_id.get(repo_id)
            if repo_root is None:
                return
            config = load_repo_dispatch_config(repo_root)
            notifier = build_notifier(config.pipeline, Path(str(record["queue_root"])))
            notifier.notify(
                "attempt-interrupted",
                {
                    "work_id": item.id,
                    "spec_id": item.task_ref.get("spec_id"),
                    "repo_id": repo_id,
                    "detail": detail,
                    "disposition": disposition,
                },
            )
        except Exception as exc:  # noqa: BLE001
            self.findings.append(
                f"unable to notify attempt-interrupted for session {record.get('slot_id')}: {exc}"
            )

    def reap_completed(self, *, terminate: bool = False) -> None:
        for record in list(self.store.list_sessions()):
            slot_id = str(record["slot_id"])
            proc = self.governor._processes.get(slot_id)
            if proc is not None and proc.poll() is None and not terminate:
                continue
            self._stage_finalization(record)
            self._crash("during_reap")
            action = self.governor.reap_session(slot_id, terminate=terminate)
            if action.action == "closed":
                self._crash("during_finalize")
                self._finalize_interrupted(record, action.detail)
                _atomic_unlink(self._finalize_marker(record))
            elif action.action in {"quarantine", "reaping"}:
                self.findings.append(f"session {slot_id}: {action.action}: {action.detail}")

    def tick(self) -> list[object]:
        if not self._started:
            self.start()
        self._reload_snapshot()
        self._expire_cooldowns()
        self.reap_completed()
        drained = (self.home.root / "state" / "drain").exists()
        launches = [] if self.stop_requested or drained else self.drainer.drain_once()
        self.heartbeat()
        return launches

    def request_stop(self, *_args) -> None:
        self.stop_requested = True

    def shutdown(self) -> None:
        self.stop_requested = True
        deadline = time.monotonic() + self.shutdown_timeout
        while self.store.list_sessions() and time.monotonic() < deadline:
            self.reap_completed()
            if self.store.list_sessions():
                time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
        # Timeout never authorizes an unsafe kill. Leave identity-bearing records
        # for startup reconciliation and surface them loudly.
        if self.store.list_sessions():
            self.findings.append("shutdown timed out with owned groups still live")
        self.drainer.stop()
        if self.daemon_path.exists():
            try:
                payload = json.loads(self.daemon_path.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                payload = {}
            if payload.get("daemon_instance_id") == self.instance_id:
                _atomic_unlink(self.daemon_path)
        if self.lock is not None:
            self.lock.release()
            self.lock = None
        self._started = False

    def run(self, *, once: bool = False) -> None:
        self.start()
        old_term = signal.signal(signal.SIGTERM, self.request_stop)
        old_int = signal.signal(signal.SIGINT, self.request_stop)
        try:
            while not self.stop_requested:
                launches = self.tick()
                if once:
                    break
                sleep_seconds, self._idle_ticks = _next_sleep(
                    self.poll_seconds, self._idle_ticks, launches, self.store.list_sessions()
                )
                time.sleep(sleep_seconds)
        finally:
            signal.signal(signal.SIGTERM, old_term)
            signal.signal(signal.SIGINT, old_int)
            self.shutdown()
