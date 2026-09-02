#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from datetime import datetime, timezone

from _telemetry.record import record_workflow_event

VALID_OUTCOMES = {"completed", "partial", "rejected", "approved", "rollback"}
VALID_REASONS = {
    "phase_progress", "phase_rework", "validator_failure", "human_redirect",
    "exploration", "archival", "telemetry_analysis", "fallback_planned",
    "fallback_drift", "fallback_starvation", "fallback_verify_overflow",
    "profile_chain_exhausted", "packet_fit_drift"
}

def main() -> int:
    parser = argparse.ArgumentParser(description="Persist a Builder workflow-event YAML file into the canonical telemetry event store.")
    parser.add_argument("input", nargs="?", help="Path to a workflow-event YAML file (optional if flags provided)")
    parser.add_argument("--root", default=".", help="Workspace root that owns active runtime telemetry (default: cwd)")
    
    # New flags
    parser.add_argument("--phase", help="Workflow phase")
    parser.add_argument("--outcome-category", help="Workflow outcome category")
    parser.add_argument("--reason-category", help="Reason for specific outcome")
    parser.add_argument("--spec", help="Spec name")
    parser.add_argument("--used-model", help="Used model")
    parser.add_argument("--execution-path", help="Execution path")
    parser.add_argument("--next-command", help="Next command")
    parser.add_argument("--artifacts-read", nargs="*", help="Artifacts read")
    parser.add_argument("--artifacts-written", nargs="*", help="Artifacts written")

    args = parser.parse_args()

    # Legacy mode: positional argument provided
    if args.input:
        input_path = Path(args.input).resolve()
        if not input_path.is_file():
            print(f"ERROR  input file not found: {input_path}", file=sys.stderr)
            return 2
        try:
            output_path = record_workflow_event(input_path, Path(args.root).resolve())
        except ValueError as exc:
            print(f"ERROR  {exc}", file=sys.stderr)
            return 1
        print(output_path)
        return 0

    # New flag mode
    if not all([args.phase, args.spec]):
        parser.print_help()
        return 2
    if args.outcome_category and args.outcome_category not in VALID_OUTCOMES:
        print(f"ERROR  unknown outcome_category `{args.outcome_category}`", file=sys.stderr)
        return 1
    if args.reason_category and args.reason_category not in VALID_REASONS:
        print(f"ERROR  unknown reason_category `{args.reason_category}`", file=sys.stderr)
        return 1

    now = datetime.now(timezone.utc)
    event = {
        "artifact": "workflow-event",
        "event_id": f"EVT-{now.strftime('%Y%m%dT%H%M%SZ')}-{args.phase}",
        "recorded_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "command": f"record-workflow-event.py --phase {args.phase}",
        "mode": "lifecycle",
        "phase": args.phase,
        "spec": args.spec,
        "used_model": args.used_model or "unknown",
        "thinking_effort": "unknown",
        "capture_source": "unavailable",
        "reason_category": args.reason_category or "phase_progress",
        "intent_summary": f"{args.phase} {args.outcome_category or 'completed'}",
        "execution_path": args.execution_path or "normal_phase",
        "artifacts_read": args.artifacts_read or [],
        "artifacts_written": args.artifacts_written or [],
        "validation_refs": [],
        "outcome_category": args.outcome_category or "completed",
        "next_command": args.next_command or "none",
        "redaction": {"sanitized": True, "fields": []},
    }

    try:
        output_path = record_workflow_event(event, Path(args.root).resolve())
    except ValueError as exc:
        print(f"ERROR  {exc}", file=sys.stderr)
        return 1
    print(output_path)
    return 0

if __name__ == "__main__":
    sys.exit(main())
