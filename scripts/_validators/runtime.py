"""Minimal runtime-directory helpers for the standalone validator package."""

from __future__ import annotations

import re
from pathlib import Path


RUNTIME_DIR_NAMES = (".builder", ".specpilot")

ARCHIVE_DIR_NAME = "archive"
# `/isanna-archive` writes `archive/YYYY-MM-DD-<spec-id>`; older entries in the tree carry no
# date prefix. Anchored on both ends so `2026-07-29-demo-two` cannot satisfy a lookup of `demo`.
_ARCHIVED_SPEC_RE = "^(?:[0-9]{4}-[0-9]{2}-[0-9]{2}-)?%s$"


def runtime_dir(project_root: Path) -> Path:
    """Return the canonical .builder runtime directory."""
    return Path(project_root) / RUNTIME_DIR_NAMES[0]


def resolve_spec_dir(specs_root: Path, spec_id: str) -> Path | None:
    """The directory holding `spec_id`: the live spec, else its archived form, else None.

    Archiving MOVES `specs/<id>/` to `specs/archive/YYYY-MM-DD-<id>/`. Release membership used
    to look only at `specs/<id>`, so archiving a release-referenced spec instantly turned it
    into a dangling ref -- meaning a spec whose own `next_action` said "Run /isanna-archive
    <name>" could not have that action performed without breaking `isanna release lint`. Two
    documented workflows contradicting each other; this resolver is what reconciles them.

    Live wins over archived: an archived copy must never shadow live work of the same id. Among
    several archived copies the newest date prefix wins, so the answer is deterministic rather
    than filesystem-order dependent.
    """
    specs_root = Path(specs_root)
    live = specs_root / spec_id
    if live.is_dir():
        return live
    archive_root = specs_root / ARCHIVE_DIR_NAME
    if not archive_root.is_dir():
        return None
    pattern = re.compile(_ARCHIVED_SPEC_RE % re.escape(spec_id))
    try:
        matches = [p for p in archive_root.iterdir() if p.is_dir() and pattern.match(p.name)]
    except OSError:
        return None
    if not matches:
        return None
    # Reverse-sorted by name: the ISO date prefix sorts chronologically, so newest first. A
    # bare `archive/<id>` (no prefix) sorts last and is only used when nothing dated exists.
    return sorted(matches, key=lambda p: p.name, reverse=True)[0]
