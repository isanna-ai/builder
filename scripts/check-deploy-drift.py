#!/usr/bin/env python3
"""Fail loudly when the LIVE dispatch runtime differs from the canonical one.

python3 scripts/check-deploy-drift.py [--live PATH] [--canonical PATH] [--json]

Why this exists
---------------
This compares an explicitly supplied live checkout with the checkout containing this script. A
deployment can otherwise run a stale dispatcher while local tests pass against newer source.

That is the same failure class this project exists to kill: a result that looks verified and
touches nothing real. The remedy is not vigilance -- it is a check that fails.

What it compares
----------------
The CONTENT of `scripts/_dispatch_runtime/*.py` in both trees (sha256 over sorted (name, bytes)).
Content, not git refs: the live worktree can legitimately sit on its own branch with its own
`.builder/dispatch.yaml`, and a git-ref check would either false-alarm on that or miss a
hand-edited file. What matters is whether the RUNTIME that actually gets imported is the one
that was written.

Exit codes: 0 = in sync · 1 = DRIFT · 2 = operational error (a tree is missing/unreadable).
Wire it into CI, and run it before believing any dispatcher change is live.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

RUNTIME_SUBDIR = Path("scripts") / "_dispatch_runtime"
DEFAULT_CANONICAL = Path(__file__).resolve().parents[1]


def _runtime_files(tree: Path) -> dict[str, bytes]:
    root = tree / RUNTIME_SUBDIR
    if not root.is_dir():
        raise SystemExit(f"[deploy-drift] not a dispatch runtime: {root}")
    out: dict[str, bytes] = {}
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts or "tests" in path.parts:
            continue  # bytecode is derived; tests do not ship into the running lane
        out[str(path.relative_to(root))] = path.read_bytes()
    return out


def _digest(files: dict[str, bytes]) -> str:
    h = hashlib.sha256()
    for name in sorted(files):
        h.update(name.encode("utf-8"))
        h.update(hashlib.sha256(files[name]).digest())
    return h.hexdigest()


def compare(live: Path, canonical: Path) -> dict:
    lf, cf = _runtime_files(live), _runtime_files(canonical)
    only_live = sorted(set(lf) - set(cf))
    only_canonical = sorted(set(cf) - set(lf))
    differing = sorted(n for n in (set(lf) & set(cf)) if lf[n] != cf[n])
    return {
        "live": str(live),
        "canonical": str(canonical),
        "live_digest": _digest(lf),
        "canonical_digest": _digest(cf),
        "in_sync": not (only_live or only_canonical or differing),
        "missing_from_live": only_canonical,   # shipped but NOT deployed -- the dangerous class
        "only_in_live": only_live,
        "content_differs": differing,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Fail when the live dispatch runtime is stale.")
    ap.add_argument("--live", type=Path,
                    help="tree the runners actually import (required)")
    ap.add_argument("--canonical", type=Path, default=DEFAULT_CANONICAL,
                    help=f"tree the code is written in (default: {DEFAULT_CANONICAL})")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    if args.live is None:
        ap.error("--live is required; select the deployed checkout explicitly")

    try:
        report = compare(args.live, args.canonical)
    except SystemExit as exc:
        print(str(exc), file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(report, indent=2))
        return 0 if report["in_sync"] else 1

    if report["in_sync"]:
        print(f"deploy: IN SYNC  ({report['live_digest'][:12]})")
        print(f"  live      {report['live']}")
        print(f"  canonical {report['canonical']}")
        return 0

    print("deploy: *** DRIFT — THE FLEET IS NOT RUNNING THE CODE YOU WROTE ***")
    print(f"  live      {report['live']}      {report['live_digest'][:12]}")
    print(f"  canonical {report['canonical']}  {report['canonical_digest'][:12]}")
    if report["missing_from_live"]:
        print("\n  SHIPPED BUT NOT DEPLOYED (present canonically, absent live):")
        for n in report["missing_from_live"]:
            print(f"    - {n}")
    if report["content_differs"]:
        print("\n  DEPLOYED BUT STALE (content differs):")
        for n in report["content_differs"]:
            print(f"    ~ {n}")
    if report["only_in_live"]:
        print("\n  LIVE-ONLY (present live, absent canonically — a hand-edit or an old file):")
        for n in report["only_in_live"]:
            print(f"    + {n}")
    print("\n  Deploy with:  git -C <live> merge --ff-only <canonical-branch>")
    print("  The dispatcher spawns fresh per cycle, so no restart is needed.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
