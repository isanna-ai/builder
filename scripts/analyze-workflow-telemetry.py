#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from _telemetry.aggregate import write_telemetry_report


def main() -> int:
    parser = argparse.ArgumentParser(description="Aggregate Builder workflow telemetry into telemetry-report.yaml.")
    parser.add_argument("--root", default=".", help="Workspace root containing active runtime telemetry events (default: cwd)")
    args = parser.parse_args()

    workspace_root = Path(args.root).resolve()
    report_path = write_telemetry_report(workspace_root)
    print(report_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
