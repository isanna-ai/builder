"""Single-process watchdog for the opt-in central daemon."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from _dispatch_runtime.lane_common import _proc_identity

from .central_daemon import load_valid_snapshot
from _dispatch_runtime.scheduler import SchedulerBusyError, _owner_pid, _pid_alive


@dataclass(frozen=True)
class SupervisorDecision:
    action: str
    detail: str


@dataclass(frozen=True)
class SupervisorLock:
    path: Path
    owner: str

    def release(self) -> None:
        if self.path.exists() and self.path.read_text(encoding="utf-8").strip() == self.owner:
            self.path.unlink()


def acquire_supervisor_lock(home_root: Path) -> SupervisorLock:
    path = Path(home_root).resolve() / "state" / "supervisor.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    owner = f"central-supervisor-{os.getpid()}"
    for _ in range(2):
        try:
            with path.open("x", encoding="utf-8") as stream:
                stream.write(owner + "\n")
            return SupervisorLock(path, owner)
        except FileExistsError as exc:
            recorded = path.read_text(encoding="utf-8").strip()
            pid = _owner_pid(recorded)
            if pid is not None and not _pid_alive(pid):
                path.unlink()
                continue
            raise SchedulerBusyError(f"central supervisor already owned: {recorded or '?'}") from exc
    raise SchedulerBusyError("central supervisor lock recovery failed")


def daemon_identity_state(record: dict[str, Any]) -> str:
    pid = record.get("pid")
    ticks = record.get("pid_start_ticks")
    if not isinstance(pid, int) or pid <= 1 or not isinstance(ticks, int):
        return "unsafe-metadata"
    live = _proc_identity(pid)
    if live is None:
        return "proven-gone"
    return "live-match" if int(live[0]) == ticks else "identity-mismatch"


class CentralSupervisor:
    def __init__(self, home_root: Path, *, launcher: Callable[[list[str]], Any] | None = None):
        self.home_root = Path(home_root).resolve()
        self.launcher = launcher or self._launch

    @property
    def daemon_path(self) -> Path:
        return self.home_root / "state" / "daemon.json"

    def _launch(self, argv: list[str]):
        # Same detach fix as LiveCutoverOperator._spawn: a watchdog-restarted daemon must run
        # in its own session so it survives the watchdog dying (the watchdog re-adopts it via
        # daemon.json identity on next launch), not as a child that dies with its parent.
        return subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
            start_new_session=True,
        )

    def _argv(self) -> list[str]:
        return [
            sys.executable,
            str(Path(__file__).resolve().parents[1] / "isanna.py"),
            "central",
            "run",
            "--home",
            str(self.home_root),
        ]

    def ensure_once(self) -> SupervisorDecision:
        _home, current_digest = load_valid_snapshot(self.home_root)
        if not self.daemon_path.exists():
            self.launcher(self._argv())
            return SupervisorDecision("started", "no recorded daemon; initial start")
        try:
            record = json.loads(self.daemon_path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            return SupervisorDecision("refused", f"invalid daemon metadata: {exc}")
        if not isinstance(record, dict):
            return SupervisorDecision("refused", "invalid daemon metadata: expected object")
        state = daemon_identity_state(record)
        if state == "live-match":
            detail = "recorded daemon is live"
            if record.get("config_digest") != current_digest:
                detail += "; config digest differs (daemon must reload; watchdog will not overlap it)"
            return SupervisorDecision("refused-live", detail)
        if state != "proven-gone":
            return SupervisorDecision("refused", f"fail-closed: {state}")
        self.launcher(self._argv())
        return SupervisorDecision("restarted", "recorded pid-start identity is proven gone")
