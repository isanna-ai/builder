#!/usr/bin/env python3
"""List Builder specs — active and archived.

Scans ``<root>/.builder/specs/`` and reports each spec with a phase inferred
from the canonical artifacts present on disk, so it needs no YAML dependency and
runs on Python 3.8+ stdlib alone. Archived specs live under
``.builder/specs/archive/``.

Backs the ``/isanna-list`` CLI utility (see ``/isanna-help``):

    python3 scripts/list-specs.py [--root .] [--json]
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from _dispatch_runtime.paths import runtime_dir

# Declared status ladder, lowest -> highest. Mirrors the status-enum in
# standards/builder-contract.md; `archived` is terminal and deliberately last.
_STATUS_LADDER = [
    "specifying", "specified", "spec-reviewed", "designed", "reviewed", "planned",
    "implementing", "implemented", "adversarially-reviewed", "verifying", "verified",
    "verified_with_tasks", "syncing", "synced", "archived",
]
_STATUS_RANK = {s: i for i, s in enumerate(_STATUS_LADDER)}

# A spec declaring a status at or beyond `gate` must have at least ONE of `files` on disk.
#
# Only TWO artifacts are gated, and the omissions are deliberate. `review-log.yaml` is not
# gated because plenty of legitimate specs never take a review phase. `design.yaml` is not
# gated because the contract's own state machine allows `specified -> spec-reviewed ->
# planned`, a route that skips design entirely -- an earlier draft of this check gated it
# and lit up a dozen specs in one repo plus most of builder's own, every one of which had simply
# taken the supported route. A check that fires on correct work is worse than no check: it
# gets read once, dismissed, and then ignored when it finally finds something real.
_ARTIFACT_GATES = [
    ("specified", ("requirements.yaml", "system-model.yaml")),
    ("planned", ("tasks.yaml",)),
]

# Read without a YAML parser: this module is stdlib-only so it runs on any host, including
# ones where the project's dependencies are not installed.
_STATUS_RE = re.compile(r"^status:\s*[\"']?([A-Za-z0-9_-]+)", re.MULTILINE)


@dataclass
class DriftFinding:
    """One spec whose DECLARED status over-claims what is actually on disk."""

    spec: str
    declared: str
    detail: str


def read_declared_status(spec_dir: Path) -> str | None:
    """The `status:` value from spec.yaml, or None when there is no readable one."""
    path = spec_dir / "spec.yaml"
    try:
        if not path.is_file():
            return None
        match = _STATUS_RE.search(path.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return None
    return match.group(1) if match else None


def collect_drift(specs_root: Path) -> list[DriftFinding]:
    """Specs whose declaration claims MORE than the artifacts on disk support.

    This is the over-claim direction only. A spec whose artifacts run ahead of its declared
    status is under-claiming: untidy, but it never causes anyone to skip work, and reporting
    it would bury the findings that matter.

    Note what this cannot see. It compares a declaration against ARTIFACTS, so it catches the
    stub spec and the status that outruns its files -- it does NOT catch a spec marked
    `planned` whose deliverable is already shipped, because every artifact is present in that
    case. That is `isanna verify --spec`'s job. The two alarms are complementary, and neither
    subsumes the other.
    """
    findings: list[DriftFinding] = []
    if not specs_root.is_dir():
        return findings
    for entry in sorted(specs_root.iterdir(), key=lambda p: p.name):
        if not entry.is_dir() or entry.name == "archive":
            continue
        declared = read_declared_status(entry)
        if declared is None:
            findings.append(DriftFinding(entry.name, "-", "no readable `status` in spec.yaml"))
            continue
        if declared not in _STATUS_RANK:
            findings.append(DriftFinding(
                entry.name, declared,
                f"unknown status `{declared}` (not in the contract status-enum)"))
            continue
        if declared == "archived":
            # Archiving legitimately strips a spec to a tombstone.
            continue
        rank = _STATUS_RANK[declared]
        missing = [
            " or ".join(files)
            for gate, files in _ARTIFACT_GATES
            if rank >= _STATUS_RANK[gate] and not any((entry / f).exists() for f in files)
        ]
        if missing:
            findings.append(DriftFinding(
                entry.name, declared,
                f"declares `{declared}` but is missing {', '.join(missing)}"))
    return findings

# Phase inference: the highest marker whose artifact is present wins.
# Ordered lowest → highest phase.
PHASE_MARKERS = [
    ("specified", ("system-model.yaml", "requirements.yaml")),
    ("designed", ("design.yaml",)),
    ("reviewed", ("review-log.yaml",)),
    ("planned", ("tasks.yaml",)),
    ("verified", ("verification.yaml", "verify.yaml")),
]


def infer_phase(spec_dir: Path) -> str:
    """Best-effort phase from the artifacts on disk (no YAML parsing)."""
    phase = "draft"
    for label, files in PHASE_MARKERS:
        if any((spec_dir / f).exists() for f in files):
            phase = label
    # An evidence directory with entries means implementation is underway/done.
    evidence = spec_dir / "evidence"
    if evidence.is_dir() and any(evidence.iterdir()) and phase in ("planned", "reviewed", "designed", "specified"):
        phase = "implementing"
    return phase


def collect(specs_root: Path):
    active, archived = [], []
    for entry in sorted(specs_root.iterdir(), key=lambda p: p.name):
        if not entry.is_dir():
            continue
        if entry.name == "archive":
            for a in sorted(entry.iterdir(), key=lambda p: p.name):
                if a.is_dir():
                    archived.append((a.name, infer_phase(a)))
            continue
        active.append((entry.name, infer_phase(entry)))
    return active, archived


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="List Builder specs (active and archived).")
    ap.add_argument("--root", default=".", help="Project root containing an active runtime directory (default: .)")
    ap.add_argument("--json", action="store_true", help="Emit JSON instead of a table")
    ap.add_argument(
        "--strict", action="store_true",
        help="Exit non-zero when any spec's declared status over-claims the artifacts on disk",
    )
    args = ap.parse_args(argv)

    specs_root = runtime_dir(Path(args.root)) / "specs"
    if not specs_root.is_dir():
        print(
            f"No specs directory under the active runtime directory for {args.root!r}. "
            "Run /isanna-setup, then /isanna-1-specify <description> to start one.",
            file=sys.stderr,
        )
        # --strict is a DRIFT gate (`make lint`, CI). An absent corpus has no spec that can
        # over-claim its artifacts, so there is no finding to report and the gate passes.
        # Failing here instead made `make lint` red on every fresh clone -- including the
        # public export, which drops `.builder/` -- for having no work rather than bad work.
        # The bare listing keeps exit 1: a human asking "what specs do I have?" in a repo with
        # none is asking a question the answer above resolves, and the non-zero exit is what
        # makes that visible in a pipeline.
        return 0 if args.strict else 1

    active, archived = collect(specs_root)
    drift = collect_drift(specs_root)

    if args.json:
        import json

        print(json.dumps(
            {
                "active": [{"spec": n, "phase": p} for n, p in active],
                "archived": [{"spec": n, "phase": p} for n, p in archived],
                "drift": [
                    {"spec": f.spec, "declared": f.declared, "detail": f.detail} for f in drift
                ],
            },
            indent=2,
        ))
        return 1 if (drift and args.strict) else 0

    if not active and not archived:
        print("No specs yet. Start one with /isanna-1-specify <description>.")
        return 0

    width = max([len(n) for n, _ in active + archived] + [4])
    if active:
        print("ACTIVE")
        for name, phase in active:
            print(f"  {name.ljust(width)}  {phase}")
    if archived:
        if active:
            print()
        print("ARCHIVED")
        for name, phase in archived:
            print(f"  {name.ljust(width)}  {phase}")
    print(f"\n{len(active)} active, {len(archived)} archived")

    if drift:
        print("\nDRIFT  declared status over-claims what is on disk")
        for f in drift:
            print(f"  {f.spec.ljust(width)}  {f.detail}")
        print(f"\n{len(drift)} spec(s) with declared/artifact drift."
              f"{'' if args.strict else '  (advisory; re-run with --strict to gate on it)'}")
        if args.strict:
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
