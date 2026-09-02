#!/usr/bin/env python3
"""Minimal central-daemon watchdog. This does not start Mission Control."""

from __future__ import annotations

import argparse
import time
from pathlib import Path

from _builder_project_model.central_supervisor import CentralSupervisor, acquire_supervisor_lock


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="central-daemon-watchdog")
    parser.add_argument("--home", default=".builder-home")
    parser.add_argument("--interval", type=float, default=5.0)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    lock = acquire_supervisor_lock(Path(args.home))
    supervisor = CentralSupervisor(Path(args.home))
    try:
        while True:
            failed = False
            try:
                decision = supervisor.ensure_once()
                print(f"{decision.action}: {decision.detail}", flush=True)
                failed = decision.action == "refused"
            except Exception as exc:  # noqa: BLE001 - watchdog must survive transient snapshot errors
                failed = True
                print(f"error: snapshot check failed, retrying next interval: {exc}", flush=True)
            if args.once:
                return 2 if failed else 0
            time.sleep(args.interval)
    finally:
        lock.release()


if __name__ == "__main__":
    raise SystemExit(main())
