#!/usr/bin/env python3
"""Safely move one stopped repository from ``.specpilot`` to ``.builder``."""

from __future__ import annotations

import argparse
import errno
import os
from pathlib import Path

from _dispatch_runtime.lane_common import _live_pgids_dir, _pgid_group_alive
from _dispatch_runtime.paths import runtime_dir
from _dispatch_runtime.scheduler import _owner_pid, _pid_alive


def _live_lock(specpilot: Path) -> bool:
    """Read the scheduler lock only; stale lockfiles deliberately do not block a move."""
    lock = specpilot / "dispatch-queue" / "queue" / ".scheduler.lock"
    if not lock.exists():
        return False
    try:
        owner = lock.read_text(encoding="utf-8").strip()
    except OSError:
        # A lock we cannot safely inspect is not evidence that the daemon is down.
        return True
    pid = _owner_pid(owner)
    return pid is not None and _pid_alive(pid)


def _live_pgids(specpilot: Path) -> list[int]:
    """Return registered, still-live process groups without sweeping or editing the registry."""
    pgids_dir = _live_pgids_dir(specpilot / "dispatch-queue")
    if not pgids_dir.is_dir():
        return []
    try:
        entries = list(pgids_dir.iterdir())
    except OSError:
        # We cannot demonstrate that no work is in flight, so fail closed.
        return [-1]
    live: list[int] = []
    for entry in entries:
        try:
            pgid = int(entry.name)
        except ValueError:
            continue
        if _pgid_group_alive(pgid):
            live.append(pgid)
    return live


def _runtime_is_live(specpilot: Path) -> str | None:
    if _live_lock(specpilot):
        return "dispatcher is running; stop the daemon first"
    if _live_pgids(specpilot):
        return "work is in flight"
    return None


def _refuse(reason: str) -> int:
    print(f"isanna migrate: {reason}")
    return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="isanna migrate", description="Atomically rename one stopped runtime directory.")
    parser.add_argument("--dir", action="store_true", required=True, help="move this repository's runtime directory")
    parser.add_argument("--target", default=".", help="repository to migrate (default: current directory)")
    parser.add_argument("--dry-run", action="store_true", help="report guards and planned move without writing")
    parser.add_argument("--force", action="store_true", help="accepted for stale-lock preflights; never overrides live work")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    target = Path(args.target).expanduser().resolve()
    if not target.is_dir():
        return _refuse(f"not a directory: {target}")

    specpilot = target / ".specpilot"
    builder = target / ".builder"
    # is_symlink precedes exists so a dangling link is also refused, never followed.
    if specpilot.is_symlink():
        return _refuse(".specpilot is a symlink; refusing to follow or move it")
    if not specpilot.exists():
        return _refuse("nothing to migrate")
    if not specpilot.is_dir():
        return _refuse(".specpilot is not a directory")
    if builder.exists() or builder.is_symlink():
        return _refuse("already migrated, or a .builder already present")

    reason = _runtime_is_live(specpilot)
    if reason:
        return _refuse(reason)

    if args.dry_run:
        print(f"WOULD MOVE {specpilot} -> {builder}")
        print("guards passed: no live scheduler lock or registered process group")
        return 0

    # A fresh read immediately before rename narrows the window without ever touching the queue.
    reason = _runtime_is_live(specpilot)
    if reason:
        return _refuse(reason)
    try:
        os.rename(specpilot, builder)
    except OSError as exc:
        if exc.errno == errno.EXDEV:
            return _refuse("cannot atomically move runtime directory across filesystems; refusing copy")
        return _refuse(f"atomic rename failed: {exc}")

    print(f"Moved {specpilot} -> {builder}")
    print("The resolver now uses .builder/ automatically; no in-repo edits are needed.")
    print("WARNING: external watchdog/daemon launchers and git tracking are the operator's job.")
    print("If .specpilot/ was git-tracked, run git add -A to record the rename.")
    print("Any external dispatch process targets the repository path, not the runtime dir; confirm its state separately.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
