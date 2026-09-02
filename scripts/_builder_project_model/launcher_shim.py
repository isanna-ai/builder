from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from _dispatch_runtime.lane_common import _proc_identity

from .session_store import SessionStore


def command_digest(command: list[str]) -> str:
    payload = json.dumps(command, separators=(",", ":"), ensure_ascii=True)
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def launcher_state_payload(*, slot_id: str, phase: str, pid: int, pid_start_ticks: int | None) -> dict[str, object]:
    return {
        "schema_version": 1,
        "slot_id": slot_id,
        "phase": phase,
        "pid": pid,
        "pid_start_ticks": pid_start_ticks,
    }


def _write_launcher_started(store: SessionStore, slot_id: str) -> None:
    ident = _proc_identity(os.getpid())
    ticks = None if ident is None else int(ident[0])
    store.write_launcher_state(
        slot_id,
        launcher_state_payload(slot_id=slot_id, phase="started", pid=os.getpid(), pid_start_ticks=ticks),
    )


def launcher_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="launcher_shim")
    parser.add_argument("--home-root", required=True)
    parser.add_argument("--slot-id", required=True)
    parser.add_argument("--crash-phase", choices=("before-pgid-write", "after-pgid-write"), default=None)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    ns = parser.parse_args(argv)
    if not ns.command:
        raise SystemExit("launcher_shim requires a provider command")
    if ns.command[0] == "--":
        command = ns.command[1:]
    else:
        command = ns.command
    if not command:
        raise SystemExit("launcher_shim requires a provider command")

    store = SessionStore(Path(ns.home_root))
    _write_launcher_started(store, ns.slot_id)
    os.setsid()
    if ns.crash_phase == "before-pgid-write":
        return 91
    ident = _proc_identity(os.getpid())
    if ident is None:
        return 92
    store.update_state(
        ns.slot_id,
        state="active",
        previous_state="starting",
        pgid=os.getpid(),
        pgid_leader_start_ticks=int(ident[0]),
        executable=str(command[0]),
        command_digest=command_digest(command),
    )
    store.write_launcher_state(
        ns.slot_id,
        launcher_state_payload(slot_id=ns.slot_id, phase="pgid-recorded", pid=os.getpid(), pid_start_ticks=int(ident[0])),
    )
    if ns.crash_phase == "after-pgid-write":
        return 93
    os.execvp(command[0], command)
    return 0


def spawn_launcher(
    *,
    home_root: Path,
    slot_id: str,
    command: list[str],
    crash_phase: str | None = None,
) -> subprocess.Popen[str]:
    env = os.environ.copy()
    package_root = str(Path(__file__).resolve().parents[1])
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = package_root if not existing else f"{package_root}{os.pathsep}{existing}"
    argv = [
        sys.executable,
        "-m",
        "_builder_project_model.launcher_shim",
        "--home-root",
        str(Path(home_root).resolve()),
        "--slot-id",
        slot_id,
    ]
    if crash_phase:
        argv.extend(["--crash-phase", crash_phase])
    argv.append("--")
    argv.extend(command)
    return subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, text=True, env=env)


def wait_for_slot_identity(
    store: SessionStore,
    slot_id: str,
    *,
    timeout_seconds: float = 5.0,
) -> dict[str, object]:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            record = store.load_session(slot_id)
        except FileNotFoundError:
            time.sleep(0.05)
            continue
        if record.get("pgid") is not None:
            return record
        time.sleep(0.05)
    raise TimeoutError(f"timed out waiting for slot identity: {slot_id}")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(launcher_main())
