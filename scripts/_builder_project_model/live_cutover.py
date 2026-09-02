"""Fail-closed cutover operator over live Builder Home state."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

from _dispatch_runtime.lane_common import _pgid_group_alive, _proc_identity, _reap_group

from .central_scheduler import inspect_home_lock
from .cutover import CutoverError, StepResult
from .governor import _identity_matches
from .home import load_builder_home
from .live_runtime import live_activation
from .session_store import SessionStore, _atomic_write_json


class LiveCutoverOperator:
    """The synthetic step interface, backed by real locks and identity records."""

    def __init__(
        self,
        home_root: Path,
        *,
        starter: Callable[[list[str]], Any] | None = None,
        signaler: Callable[[int, int], None] = os.kill,
        reaper: Callable[[int], None] = _reap_group,
    ):
        self.home = load_builder_home(Path(home_root).resolve())
        if not live_activation(self.home):
            raise CutoverError("live-operator-inactive")
        self.store = SessionStore(self.home.root)
        self._starter = starter or self._spawn
        self._signaler = signaler
        self._reaper = reaper
        self._transient_state: dict[str, Any] | None = None

    @property
    def legacy_dir(self) -> Path:
        return self.home.root / "state" / "legacy"

    @property
    def rollback_path(self) -> Path:
        return self.home.root / "state" / "cutover-rollback.json"

    @property
    def watchdog_path(self) -> Path:
        return self.home.root / "state" / "supervisor.json"

    def _spawn(self, argv: list[str]):
        # start_new_session (setsid) + detached stdio so the central daemon SURVIVES the
        # cutover CLI process exiting. Without it the daemon was a child of the short-lived
        # `isanna home cutover` process and died the moment that returned, leaving a stale
        # home lock and an unowned repo — caught only in a live cutover, never by a fixture.
        return subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
            start_new_session=True,
        )

    def load(self) -> dict[str, Any]:
        return {"central_planned": self._daemon_record(require=False) is not None}

    def _records(self) -> list[tuple[Path, dict[str, Any]]]:
        if not self.legacy_dir.is_dir():
            return []
        rows = []
        for path in sorted(self.legacy_dir.glob("*.json")):
            try:
                row = json.loads(path.read_text(encoding="utf-8"))
            except Exception as exc:  # noqa: BLE001
                raise CutoverError(f"invalid-legacy-metadata:{path.name}:{exc}") from exc
            if not isinstance(row, dict):
                raise CutoverError(f"invalid-legacy-metadata:{path.name}")
            rows.append((path, row))
        return rows

    def _sessions(self) -> list[dict[str, Any]]:
        if not self.store.paths.sessions_dir.is_dir():
            return []
        return self.store.list_sessions()

    def _pid_state(self, row: dict[str, Any], label: str) -> str:
        pid = row.get("pid")
        ticks = row.get("pid_start_ticks")
        executable = row.get("executable")
        if not isinstance(pid, int) or pid <= 1 or not isinstance(ticks, int) or not executable:
            raise CutoverError(f"missing-identity-metadata:{label}")
        ident = _proc_identity(pid)
        if ident is None:
            return "gone"
        live_ticks, cmdline = ident
        if int(live_ticks) != ticks or not cmdline.split() or cmdline.split()[0] != executable:
            raise CutoverError(f"identity-mismatch:{label}")
        return "live"

    def _daemon_record(self, *, require: bool) -> dict[str, Any] | None:
        path = self.store.paths.daemon_path
        if not path.exists():
            if require:
                raise CutoverError("central-daemon-not-recorded")
            return None
        try:
            row = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            raise CutoverError(f"invalid-central-daemon-metadata:{exc}") from exc
        state = self._pid_state(row, "central-daemon")
        if require and state != "live":
            raise CutoverError("central-daemon-not-live")
        row["_identity_state"] = state
        return row

    def _result(self, step: str, dry_run: bool, changed: bool, *details: str) -> StepResult:
        return StepResult(step, dry_run, changed and not dry_run, tuple(details))

    def stop_legacy(self, *, dry_run: bool) -> StepResult:
        live = []
        for _path, row in self._records():
            label = str(row.get("repo_id") or "legacy")
            if self._pid_state(row, label) == "live":
                live.append((label, int(row["pid"])))
        if not dry_run:
            for _label, pid in live:
                self._signaler(pid, signal.SIGTERM)
        details = [f"stop:legacy:{label}" for label, _ in live] or ["legacy-absent"]
        return self._result("stop_legacy", dry_run, bool(live), *details)

    def prove_legacy_gone(self, *, dry_run: bool) -> StepResult:
        details = []
        for _path, row in self._records():
            label = str(row.get("repo_id") or "legacy")
            if self._pid_state(row, label) == "live":
                if not (dry_run and self._transient_state is not None):
                    raise CutoverError(f"legacy-owner-still-live:{label}")
                details.append(f"would-prove-gone:legacy:{label}")
            else:
                details.append(f"gone:legacy:{label}")
        return self._result("prove_legacy_gone", dry_run, False, *(details or ["legacy-absent"]))

    def reconcile_legacy_pgids(self, *, dry_run: bool) -> StepResult:
        details = []
        for path, row in self._records():
            pgid = row.get("pgid")
            if pgid is None:
                continue
            if not isinstance(pgid, int) or pgid <= 1:
                raise CutoverError(f"unsafe-pgid:{path.name}")
            required = ("pgid_leader_start_ticks", "executable", "command_digest")
            if any(row.get(key) in (None, "") for key in required):
                raise CutoverError(f"missing-identity-metadata:{path.name}")
            if _pgid_group_alive(pgid):
                if not _identity_matches(row):
                    raise CutoverError(f"identity-mismatch:{path.name}")
                details.append(f"reap:{pgid}")
                if not dry_run:
                    self._reaper(pgid)
        return self._result("reconcile_legacy_pgids", dry_run, bool(details), *(details or ["no-live-legacy-pgids"]))

    def start_central(self, *, dry_run: bool) -> StepResult:
        for _path, row in self._records():
            if self._pid_state(row, str(row.get("repo_id") or "legacy")) == "live" and not (
                dry_run and self._transient_state is not None
            ):
                raise CutoverError("dual-owner-refused:legacy-live")
        lock = inspect_home_lock(self.home.root)
        if lock.state == "locked-live":
            raise CutoverError("central-daemon-already-live")
        argv = [sys.executable, str(Path(__file__).resolve().parents[1] / "isanna.py"), "central", "run", "--home", str(self.home.root)]
        if dry_run:
            if self._transient_state is not None:
                self._transient_state["central_planned"] = True
        else:
            proc = self._starter(argv)
            _atomic_write_json(self.rollback_path, {
                "schema_version": 1,
                "central_pid": getattr(proc, "pid", None),
                "legacy_records": [row for _path, row in self._records()],
            })
        return self._result("start_central", dry_run, True, "start:central-daemon")

    def acquire_repo_locks(self, *, dry_run: bool) -> StepResult:
        if dry_run and self._transient_state is not None and self._transient_state.get("central_planned"):
            return self._result("acquire_repo_locks", dry_run, False, *(f"would-acquire:{r}" for r in self.home.policy.drain_repos))
        self._daemon_record(require=True)
        details = []
        for repo_id in self.home.policy.drain_repos:
            root = self.home.repo_roots_by_id[repo_id]
            from .eligibility import inspect_scheduler_lock
            from _dispatch_runtime.paths import runtime_dir

            lock = inspect_scheduler_lock(runtime_dir(root) / "dispatch-queue" / "queue" / ".scheduler.lock")
            if lock.state != "locked-live" or not str(lock.owner or "").startswith("central-"):
                raise CutoverError(f"repo-lock-not-central:{repo_id}:{lock.state}")
            details.append(f"owned:{repo_id}")
        return self._result("acquire_repo_locks", dry_run, False, *details)

    def reconcile_repo_runtime(self, *, dry_run: bool) -> StepResult:
        usage: dict[str, int] = {}
        for row in self._sessions():
            if row["state"] in {"starting", "active", "reaping"}:
                usage[row["provider"]] = usage.get(row["provider"], 0) + 1
        details = []
        for provider, count in sorted(usage.items()):
            cap = self.home.policy.providers[provider].max_sessions
            if count > cap:
                raise CutoverError(f"provider-cap-exceeded:{provider}:{count}>{cap}")
            details.append(f"capacity:{provider}:{count}/{cap}")
        return self._result("reconcile_repo_runtime", dry_run, False, *(details or ["capacity:empty"]))

    def replace_watchdogs(self, *, dry_run: bool) -> StepResult:
        if not dry_run:
            _atomic_write_json(self.watchdog_path, {"schema_version": 1, "mode": "central-only", "enabled": True})
        return self._result("replace_watchdogs", dry_run, True, "watchdogs:central-only")

    def stop_new_central_launches(self, *, dry_run: bool) -> StepResult:
        path = self.home.root / "state" / "drain"
        if not dry_run:
            path.write_text("rollback\n", encoding="utf-8")
        return self._result("stop_new_central_launches", dry_run, not path.exists(), "central-launches-disabled")

    def reconcile_central_groups(self, *, dry_run: bool) -> StepResult:
        details = []
        for row in self._sessions():
            if not _identity_matches(row):
                raise CutoverError(f"identity-mismatch:session:{row['slot_id']}")
            details.append(f"reconcile-central:{row['slot_id']}")
            if not dry_run:
                self._reaper(int(row["pgid"]))
        return self._result("reconcile_central_groups", dry_run, bool(details), *(details or ["no-central-groups"]))

    def stop_central(self, *, dry_run: bool) -> StepResult:
        row = self._daemon_record(require=False)
        if row is None or row.get("_identity_state") == "gone":
            return self._result("stop_central", dry_run, False, "central-already-stopped")
        if not dry_run:
            self._signaler(int(row["pid"]), signal.SIGTERM)
        return self._result("stop_central", dry_run, True, "central-stopped")

    def release_repo_locks(self, *, dry_run: bool) -> StepResult:
        lock = inspect_home_lock(self.home.root)
        if lock.state == "locked-live" and not dry_run:
            raise CutoverError("central-owner-still-live")
        return self._result("release_repo_locks", dry_run, False, "locks-release-with-daemon")

    def restore_legacy_watchdogs(self, *, dry_run: bool) -> StepResult:
        return self._result("restore_legacy_watchdogs", dry_run, False, "rollback-package-preserved")

    def restore_legacy_daemons(self, *, dry_run: bool) -> StepResult:
        records = self._records()
        details = [f"restore:{p.stem}" for p, _ in records] or ["legacy-idle"]
        return self._result("restore_legacy_daemons", dry_run, bool(records), *details)

    def select_legacy_discovery(self, *, dry_run: bool) -> StepResult:
        path = self.home.root / "state" / "discovery.json"
        if not dry_run:
            _atomic_write_json(path, {"schema_version": 1, "mode": "legacy"})
        return self._result("select_legacy_discovery", dry_run, True, "discovery:legacy")
