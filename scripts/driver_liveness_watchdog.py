#!/usr/bin/env python3
"""LLM-free driver liveness watchdog.

A dumb, scheduled check — independent of the driver it watches — that reads
the driver's own heartbeat file and pages the owner when the driver is
stalled (heartbeat gone stale) or dead (recorded pid no longer alive). This
module never imports `builder-driver.py`, never calls an LLM to decide
anything or to format the page, and never routes the page through the
driver: the escalation channel must not depend on the thing that just
failed. Paging goes through `_dispatch_runtime.notifier` — plain file/HTTP
I/O, the same LLM-free channel the dispatcher already uses for
blocked_human/spec_failed alerts.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_datetime(value: str) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:  # exists, owned by another user
        return True
    except OSError:
        return False


@dataclass(frozen=True)
class LivenessVerdict:
    alive: bool
    reason: str


def read_heartbeat(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def check_liveness(heartbeat_path: Path, *, stale_after_seconds: float, now: datetime | None = None) -> LivenessVerdict:
    """Pure liveness check driven ENTIRELY by the driver's own heartbeat file
    — no call into the driver process, no LLM. A missing/corrupt/stale
    heartbeat, or a recorded pid that is no longer running, means the driver
    is stalled or dead."""
    now = now or _utc_now()
    heartbeat = read_heartbeat(heartbeat_path)
    if heartbeat is None:
        return LivenessVerdict(False, "heartbeat file missing or unreadable")
    at = heartbeat.get("at")
    if not at:
        return LivenessVerdict(False, "heartbeat has no timestamp")
    try:
        age = (now - _parse_datetime(str(at))).total_seconds()
    except ValueError:
        return LivenessVerdict(False, "heartbeat timestamp is unparseable")
    if age > stale_after_seconds:
        return LivenessVerdict(False, f"heartbeat stale ({int(age)}s > {int(stale_after_seconds)}s)")
    pid = heartbeat.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return LivenessVerdict(False, "heartbeat has no valid driver pid")
    if not _pid_alive(pid):
        return LivenessVerdict(False, f"heartbeat pid {pid} is not running")
    return LivenessVerdict(True, "alive")


def page_owner(notifier: Any, verdict: LivenessVerdict, heartbeat_path: Path) -> None:
    """Page over the LLM-free notifier channel. Never routes through the
    driver — `notifier` is a plain file/HTTP sink, never the driver process
    or an LLM call."""
    notifier.notify("driver_stalled", {
        "spec_id": "driver",
        "phase": "liveness",
        "reason": verdict.reason,
        "work_id": "driver-liveness",
        "heartbeat_path": str(heartbeat_path),
    })


def ensure_once(
    heartbeat_path: Path,
    notifier: Any,
    *,
    stale_after_seconds: float,
    now: datetime | None = None,
) -> LivenessVerdict:
    verdict = check_liveness(heartbeat_path, stale_after_seconds=stale_after_seconds, now=now)
    if not verdict.alive:
        page_owner(notifier, verdict, heartbeat_path)
    return verdict


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="driver-liveness-watchdog")
    parser.add_argument("--project-dir", default=".")
    parser.add_argument("--heartbeat", default=None)
    parser.add_argument("--interval", type=float, default=30.0)
    parser.add_argument("--stale-after", type=float, default=120.0)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)

    project_dir = Path(args.project_dir).resolve()
    from _dispatch_runtime.paths import runtime_dir

    heartbeat_path = Path(args.heartbeat) if args.heartbeat else (runtime_dir(project_dir) / "driver-heartbeat.json")

    from _dispatch_runtime.config import load_dispatch_config
    from _dispatch_runtime.notifier import build_notifier
    from _dispatch_runtime.queue_store import QueueStore

    try:
        config = load_dispatch_config(runtime_dir(project_dir) / "dispatch.yaml")
        pipeline = config.pipeline
        queue_root = QueueStore(config.queue_store_path).root
    except Exception:  # noqa: BLE001 - the watchdog must still page even with a broken config
        pipeline = {}
        queue_root = runtime_dir(project_dir) / "dispatch-queue"
    notifier = build_notifier(pipeline, queue_root)

    while True:
        verdict = ensure_once(heartbeat_path, notifier, stale_after_seconds=args.stale_after)
        print(f"{'alive' if verdict.alive else 'STALLED'}: {verdict.reason}", flush=True)
        if args.once:
            return 0 if verdict.alive else 2
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
