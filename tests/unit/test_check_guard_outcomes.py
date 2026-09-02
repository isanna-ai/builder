"""A guarding test that skips at runtime still counts as a live guard to the AST drift-check
(behaviors.py) -- it can only see decorators and commented-out defs, not a `raise SkipTest`
buried in a test body. check_guard_outcomes.py closes that hole using the outcomes the pytest
shim records (PYTEST_SHIM_OUTCOMES).

The unit of honesty is the BEHAVIOR, not the individual guarding_test: a behavior may legitimately
list more than one guard, and one sibling may conditionally (and correctly) skip in a given
checkout. Only a behavior where EVERY listed guard skipped or never ran has no live guard, and
only that must fail.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from _validators.check_guard_outcomes import check_guard_outcomes  # noqa: E402


def _write_synthetic_behaviors(tmp_path: Path, bid: str, test_names: list[str]) -> None:
    (tmp_path / "docs").mkdir(parents=True, exist_ok=True)
    guarding_tests = "\n".join(f"      - tests/unit/test_x.py::{name}" for name in test_names)
    (tmp_path / "docs" / "system-behaviors.yaml").write_text(
        "schema: system-behaviors/v1\n"
        "behaviors:\n"
        f"  - id: {bid}\n"
        "    area: x\n"
        "    behavior: b\n"
        "    invariant: i\n"
        "    breaks_when: w\n"
        "    guarding_tests:\n"
        f"{guarding_tests}\n",
        encoding="utf-8",
    )


def _write_outcomes(tmp_path: Path, ran: list[str], skipped: list[str]) -> Path:
    outcomes_path = tmp_path / "outcomes.json"
    outcomes_path.write_text(json.dumps({"ran": ran, "skipped": skipped}), encoding="utf-8")
    return outcomes_path


def test_flags_a_behavior_whose_only_guard_skipped_at_runtime(tmp_path):
    _write_synthetic_behaviors(tmp_path, "runtime-skip", ["test_x"])
    node_id = "tests/unit/test_x.py::test_x"
    outcomes_path = _write_outcomes(tmp_path, ran=[node_id], skipped=[node_id])
    findings = check_guard_outcomes(tmp_path, outcomes_path)
    assert any("NO guarding_test ran" in f for f in findings), findings


def test_flags_a_behavior_whose_only_guard_never_ran(tmp_path):
    _write_synthetic_behaviors(tmp_path, "never-ran", ["test_x"])
    # A different node id ran; the documented guard is simply absent from `ran`.
    outcomes_path = _write_outcomes(tmp_path, ran=["tests/unit/test_other.py::test_other"], skipped=[])
    findings = check_guard_outcomes(tmp_path, outcomes_path)
    assert any("NO guarding_test ran" in f for f in findings), findings


def test_passes_when_one_of_two_guards_skips_but_the_other_ran(tmp_path):
    # The real-world case this refinement exists for: a behavior with a sibling guard that
    # conditionally (and legitimately) skips in this checkout, while its other guard runs.
    # The behavior IS live-guarded -- this must NOT be flagged.
    _write_synthetic_behaviors(tmp_path, "sibling-skip", ["test_skips", "test_runs"])
    skipped_id = "tests/unit/test_x.py::test_skips"
    ran_id = "tests/unit/test_x.py::test_runs"
    outcomes_path = _write_outcomes(tmp_path, ran=[skipped_id, ran_id], skipped=[skipped_id])
    findings = check_guard_outcomes(tmp_path, outcomes_path)
    assert findings == [], findings


def test_passes_when_all_guards_ran(tmp_path):
    _write_synthetic_behaviors(tmp_path, "clean", ["test_x"])
    node_id = "tests/unit/test_x.py::test_x"
    outcomes_path = _write_outcomes(tmp_path, ran=[node_id], skipped=[])
    findings = check_guard_outcomes(tmp_path, outcomes_path)
    assert findings == [], findings
