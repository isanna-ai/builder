from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from scripts._validators import CHECKS


ROOT = Path(__file__).resolve().parents[2]
NEW_CHECKS = {"prompt_budget", "packet_fit", "runner_ready", "anchors", "intent"}
MANIFEST_VALIDATORS = {
    "script _validators/anchors.py",
    "script _validators/dependencies.py",
    "script _validators/intent.py",
    "script _validators/packet_fit.py",
    "script _validators/prompt_budget.py",
    "script _validators/runner_ready.py",
}


def test_list_checks_contains_new_checks() -> None:
    result = subprocess.run([sys.executable, "scripts/validate-spec.py", "--list-checks"], cwd=ROOT, text=True, capture_output=True)
    assert result.returncode == 0
    listed = set(result.stdout.splitlines())
    assert NEW_CHECKS <= listed


def test_checks_registered_with_expected_length() -> None:
    assert len(CHECKS) >= 19


def test_new_checks_are_callable() -> None:
    checks = dict(CHECKS)
    for name in NEW_CHECKS:
        assert callable(checks[name])


def test_asset_manifest_lists_registered_validator_scripts() -> None:
    manifest_lines = set((ROOT / "asset-manifest.txt").read_text(encoding="utf-8").splitlines())
    assert MANIFEST_VALIDATORS <= manifest_lines
