#!/usr/bin/env python3
"""Builder external validator for spec artifacts."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from typing import Optional

from _validators import list_checks, run_checks
from _validators.common import ValidationContext
from _validators.runtime import runtime_dir


def resolve_spec_dir(root: Path, spec_arg: str) -> Path:
    candidate = Path(spec_arg)
    if candidate.is_absolute() and candidate.is_dir():
        return candidate
    if candidate.is_dir():
        return candidate.resolve()
    return (runtime_dir(root) / "specs" / spec_arg).resolve()


def resolve_contract_path(root: Path, explicit: Optional[str]) -> Optional[Path]:
    if explicit:
        path = Path(explicit).resolve()
        return path if path.is_file() else None

    candidates = [
        root / "standards" / "builder-contract.md",
        runtime_dir(root) / "standards" / "builder-contract.md",
        root / "builder" / "standards" / "builder-contract.md",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate a Builder spec directory.")
    parser.add_argument(
        "spec",
        nargs="?",
        help="Feature name (resolved under the active runtime specs directory) or direct path to a spec directory",
    )
    parser.add_argument(
        "--root",
        default=".",
        help="Workspace root (default: cwd). Used to resolve a spec under the active runtime directory.",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Enable strict validation: enforce status enum and reject unknown fields.",
    )
    parser.add_argument(
        "--list-checks",
        action="store_true",
        help="Print the artifact types validated and exit.",
    )
    parser.add_argument(
        "--contract",
        default=None,
        help="Explicit path to builder-contract.md.",
    )
    args = parser.parse_args()

    if args.list_checks:
        for check_name in list_checks():
            print(check_name)
        return 0

    if not args.spec:
        print("error: spec argument is required (unless --list-checks is used)", file=sys.stderr)
        return 2

    root = Path(args.root).resolve()
    spec_dir = resolve_spec_dir(root, args.spec)
    if not spec_dir.is_dir():
        print(f"ERROR  spec directory not found: {spec_dir}", file=sys.stderr)
        return 2

    context = ValidationContext(
        spec_dir=spec_dir,
        strict=args.strict,
        contract_path=resolve_contract_path(root, args.contract),
    )

    print(f"Validating {spec_dir}")
    total_checks = 0
    total_errors = 0
    for result in run_checks(context):
        if result.skipped:
            print(f"SKIP   {result.skip_message}")
            continue
        for error in result.errors:
            print(f"ERROR  {error}")
            total_errors += 1
        total_checks += result.total_checks
        if result.summary:
            print(f"OK     {result.summary}")

    print()
    if total_errors:
        print(f"FAIL   {total_errors} error(s) across {total_checks} item(s)")
        return 1
    print(f"PASS   0 errors across {total_checks} item(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
