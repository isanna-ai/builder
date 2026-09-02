#!/usr/bin/env python3
"""check-mirror-drift.py — Classify drift between a Builder install root and canonical source.

Compares each installed asset's current SHA-256 against:
  (a) the SHA-256 recorded in install-state.json at install time
  (b) the current canonical file's SHA-256

Classification:
  out-of-date-mirror      installed == recorded digest BUT canonical has changed
  unsupported-divergence  installed ≠ recorded AND installed ≠ canonical
  unverifiable            recorded digest AND canonical digest both unavailable
  supported-extension     file matches --allow-extension pattern (zero drift)

Exit codes:
  0 = no drift (or only supported-extensions)
  1 = out-of-date-mirror, unsupported-divergence, or unverifiable found
  2 = usage / I/O error (install-state.json missing, etc.)

Zero runtime deps beyond Python 3.8+ stdlib.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import sys
from pathlib import Path

from _dispatch_runtime.paths import runtime_dir
from typing import Optional


def sha256_file(path: Path) -> Optional[str]:
    """Return hex SHA-256 of a file, or None if the file can't be read."""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def load_install_state(install_root: Path) -> tuple[Optional[dict], Optional[str]]:
    """Return (data, error). error is set if the file is missing or invalid."""
    state_path = runtime_dir(install_root) / "install-state.json"
    if not state_path.exists():
        return None, f"{state_path}: not found; run install.sh first"
    try:
        return json.loads(state_path.read_text(encoding="utf-8")), None
    except (json.JSONDecodeError, OSError) as exc:
        return None, f"cannot parse install-state.json: {exc}"


def classify(
    rel_path: str,
    install_root: Path,
    canonical_root: Path,
    recorded_digest: str,
    allow_patterns: list[str],
    source: Optional[str] = None,
) -> tuple[str, str]:
    """Return (classification, hint).

    classification is one of:
      clean, out-of-date-mirror, unsupported-divergence, unverifiable,
      supported-extension, missing-installed
    """
    installed_path = install_root / rel_path
    # Prefer the canonical-relative "source" path install-state.json recorded
    # at install time — it is correct for every AI layout (copilot, claude,
    # codex) without needing to special-case installed path shapes. Fall back
    # to path-shape inference for legacy install-state.json entries recorded
    # before "source" was tracked, or entries missing it for any reason.
    #   .github/prompts/  → prompts/ in canonical
    #   .github/skills/   → skills/ in canonical
    #   .builder/scripts/ → scripts/ in canonical
    #   .builder/standards/ → standards/ in canonical
    #   .builder/templates/ → templates/ in canonical
    #   .builder/<name>.md (flat standards copy) → standards/<name>.md in canonical
    #   .builder/constitution.md → templates/constitution.md
    canonical_path = _resolve_canonical_source(source, canonical_root) or _resolve_canonical(
        rel_path, canonical_root
    )

    if not installed_path.exists():
        return "missing-installed", "file not present in install root"

    # Check allowlist AFTER the exists check: an allowed-to-differ file must
    # still exist. Checking the allowlist first let a matching pattern hide
    # an absent file as supported-extension instead of missing-installed.
    for pattern in allow_patterns:
        if fnmatch.fnmatch(rel_path, pattern) or fnmatch.fnmatch(
            Path(rel_path).name, pattern
        ):
            return "supported-extension", f"matches allow pattern '{pattern}'"

    installed_digest = sha256_file(installed_path)
    canonical_digest = sha256_file(canonical_path) if (canonical_path and canonical_path.exists()) else None

    if installed_digest is None:
        return "unsupported-divergence", "cannot read installed file"

    # If recorded digest was "unavailable" (python3 was absent at install time),
    # we fall back to comparing only with the canonical.
    if recorded_digest == "unavailable":
        if canonical_digest is None:
            # Neither the recorded install-time digest nor the canonical
            # digest is available — integrity is genuinely unverifiable, not
            # clean. Fail closed: counts as drift so it isn't silently missed.
            return "unverifiable", "re-run install.sh with python3 available to record a real digest"
        if installed_digest != canonical_digest:
            return "unsupported-divergence", (
                f"installed differs from canonical "
                f"(recorded digest was unavailable at install time)"
            )
        return "clean", "matches canonical"

    if installed_digest == canonical_digest and installed_digest == recorded_digest:
        return "clean", "all digests match"

    if installed_digest == recorded_digest and canonical_digest != recorded_digest:
        # Installed matches what was installed; canonical has since changed
        return "out-of-date-mirror", (
            f"installed matches recorded digest but canonical has changed "
            f"(canonical: {canonical_digest[:12] if canonical_digest else 'missing'})"
        )

    if installed_digest == canonical_digest and installed_digest != recorded_digest:
        # Someone re-installed (upgraded) this file; it now matches canonical
        return "clean", "matches canonical (re-installed since recorded)"

    if installed_digest != recorded_digest and installed_digest != canonical_digest:
        return "unsupported-divergence", (
            f"installed matches neither recorded "
            f"({recorded_digest[:12]}) nor canonical "
            f"({canonical_digest[:12] if canonical_digest else 'missing'})"
        )

    return "clean", "all digests match"


def _resolve_canonical_source(source: Optional[str], canonical_root: Path) -> Optional[Path]:
    """Resolve canonical path from install-state.json's recorded 'source' field.

    install.sh's add_asset records the canonical-relative source path for every
    asset at install time, regardless of AI layout. Path-shape inference in
    _resolve_canonical only understands copilot (.github/*) and .builder/*
    installed shapes — claude (.claude/commands/isanna-foo.md) and absolute
    codex keys fall through it as canonical:missing, a false drift. Prefer
    this when the recorded source is a non-empty relative path.
    """
    if not source:
        return None
    if Path(source).is_absolute():
        return None
    return canonical_root / source


def _resolve_canonical(rel_path: str, canonical_root: Path) -> Optional[Path]:
    """Map an installed relative path back to its canonical source file."""
    p = Path(rel_path)
    parts = p.parts

    if len(parts) >= 3 and parts[0] == ".github" and parts[1] == "prompts":
        return canonical_root / "prompts" / parts[2]

    if len(parts) >= 4 and parts[0] == ".github" and parts[1] == "skills":
        # e.g. .github/skills/planning/SKILL.md → skills/planning/SKILL.md
        return canonical_root / "skills" / Path(*parts[2:])

    if len(parts) >= 3 and parts[0] == ".builder" and parts[1] in ("scripts", "schemas", "tests", "skills"):
        return canonical_root / parts[1] / Path(*parts[2:])

    if len(parts) >= 3 and parts[0] == ".builder" and parts[1] == "standards":
        return canonical_root / "standards" / parts[2]

    if len(parts) >= 3 and parts[0] == ".builder" and parts[1] == "templates":
        if parts[2] == "constitution.md":
            return canonical_root / "templates" / "constitution.md"
        return canonical_root / "templates" / parts[2]

    if len(parts) == 2 and parts[0] == ".builder":
        filename = parts[1]
        if filename == "constitution.md":
            return canonical_root / "templates" / "constitution.md"
        # Flat standards copy
        return canonical_root / "standards" / filename

    return None


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Check for drift between a Builder install root and canonical source."
    )
    ap.add_argument("--canonical", required=True, help="Path to canonical Builder root.")
    ap.add_argument("--install-root", required=True, help="Path to the installed target root.")
    ap.add_argument(
        "--allow-extension",
        action="append",
        default=[],
        metavar="PATTERN",
        dest="allow_patterns",
        help="Glob pattern for files that are allowed to differ (supported extensions).",
    )
    args = ap.parse_args()

    canonical_root = Path(args.canonical).resolve()
    install_root = Path(args.install_root).resolve()

    if not canonical_root.is_dir():
        print(f"error: canonical root not found: {canonical_root}", file=sys.stderr)
        return 2

    if not install_root.is_dir():
        print(f"error: install root not found: {install_root}", file=sys.stderr)
        return 2

    state, err = load_install_state(install_root)
    if err:
        print(err, file=sys.stderr)
        return 2

    assets: dict = state.get("assets", {}) if isinstance(state, dict) else {}
    if not assets:
        print("warning: install-state.json has no 'assets' entries", file=sys.stderr)
        return 0

    drift_found = False
    for rel_path, info in assets.items():
        recorded_digest = info.get("sha256", "unavailable") if isinstance(info, dict) else "unavailable"
        source = info.get("source") if isinstance(info, dict) else None
        classification, hint = classify(
            rel_path,
            install_root,
            canonical_root,
            recorded_digest,
            args.allow_patterns,
            source,
        )
        if classification in ("out-of-date-mirror", "unsupported-divergence", "unverifiable", "missing-installed"):
            print(f"{rel_path}  {classification}  {hint}")
            drift_found = True
        elif classification == "supported-extension":
            print(f"{rel_path}  supported-extension  {hint}")

    return 1 if drift_found else 0


if __name__ == "__main__":
    sys.exit(main())
