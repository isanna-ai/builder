"""Runtime-skip honesty check for guarding tests.

behaviors.py's AST drift-check can only see a guarding test's *source* -- a skip decorator,
or a commented-out def. It cannot see a runtime ``raise SkipTest`` (or any other conditional
skip) buried inside the test body, so a guarding test that quietly skips every single time the
real gate runs still counts as a live guard to that check. This closes that hole using the
outcomes the pytest shim itself records (see pytest/__main__.py, ``PYTEST_SHIM_OUTCOMES``).

The unit of honesty is the BEHAVIOR, not the individual guarding_test: a behavior can legitimately
list more than one guard, and one of them may conditionally (and correctly) skip in a given
checkout -- e.g. an invariant that only applies pre-cutover, tested against a fixture that no
longer exists post-cutover. That sibling guard skipping is not drift. A behavior only has NO live
guard -- and only then fails here -- when every single guarding_test it lists either skipped or
never ran at all during the actual gate run. Stdlib-only, deterministic.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from _validators.behaviors import _normalize_ref
from _validators.common import parse_yaml_like_file


def check_guard_outcomes(root: Path, outcomes_path: Path) -> list[str]:
    """Return human-readable findings; empty means every behavior has at least one live guard."""
    root = Path(root)
    data, errors = parse_yaml_like_file(root / "docs" / "system-behaviors.yaml")
    if errors:
        return [f"system-behaviors.yaml: {e}" for e in errors]
    behaviors = data.get("behaviors") if isinstance(data, dict) else None
    if not isinstance(behaviors, list):
        return ["system-behaviors.yaml: no behaviors defined"]

    try:
        outcomes = json.loads(Path(outcomes_path).read_text(encoding="utf-8"))
    except OSError as exc:
        return [f"guard outcomes file not found: {outcomes_path} ({exc})"]
    except json.JSONDecodeError as exc:
        return [f"guard outcomes file is not valid JSON: {outcomes_path} ({exc})"]

    ran = set(outcomes.get("ran") or [])
    skipped = set(outcomes.get("skipped") or [])

    findings: list[str] = []
    for behavior in behaviors:
        if not isinstance(behavior, dict):
            continue
        bid = str(behavior.get("id") or "?")
        guarding_tests = behavior.get("guarding_tests")
        if not isinstance(guarding_tests, list) or not guarding_tests:
            continue
        refs = [_normalize_ref(raw) for raw in guarding_tests]
        refs = [ref for ref in refs if ref and "::" in ref]
        if not refs:
            continue

        live = [ref for ref in refs if ref in ran and ref not in skipped]
        if live:
            continue  # at least one guard actually ran -- the behavior is live-guarded

        skipped_refs = [ref for ref in refs if ref in skipped]
        absent_refs = [ref for ref in refs if ref not in ran and ref not in skipped]
        findings.append(
            f"{bid}: NO guarding_test ran during the gate (skipped: {skipped_refs}, "
            f"absent: {absent_refs}) -- behavior has no live guard"
        )
    return findings


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: check_guard_outcomes.py <repo-root> <outcomes-json-path>", file=sys.stderr)
        return 2
    findings = check_guard_outcomes(Path(argv[0]), Path(argv[1]))
    if findings:
        print("guarding-test runtime-skip check FAILED:")
        for finding in findings:
            print(f"  - {finding}")
        return 1
    print("guarding-test runtime-skip check: every behavior has at least one live guard")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
