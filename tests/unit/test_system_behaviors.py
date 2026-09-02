"""The behavioral SSOT (docs/system-behaviors.yaml) cannot lie.

This IS the drift check, run in the gate: every documented behavior must name a guarding test that
exists and is actually run by `make gate`. If someone documents a behavior with no live gated test,
or renames/deletes a guarding test out from under the SSOT, this test goes red.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from _validators.behaviors import _normalize_ref, check_behavior_drift  # noqa: E402
from _yaml import yaml  # noqa: E402


def _load_behaviors_yaml():
    return yaml.safe_load((ROOT / "docs" / "system-behaviors.yaml").read_text(encoding="utf-8"))


def test_the_behavioral_ssot_has_no_drift():
    findings = check_behavior_drift(ROOT)
    assert findings == [], "behavioral SSOT drift:\n" + "\n".join(f"  - {f}" for f in findings)


def test_authoring_cutover_mutation_audit_provenance_is_current():
    text = (ROOT / "docs" / "system-behaviors.yaml").read_text(encoding="utf-8")
    assert "authoring/cutover" in text
    assert "re-audited 2026-07-24" in text


def test_the_2026_07_27_backfill_scopes_the_mutation_audit_claim():
    # The 8 entries added after that date are verified by the AST
    # drift-check + reading what each cited test asserts -- NOT by a mutation audit. The header
    # must say so, or its mutation-audit claim silently over-covers these entries.
    text = (ROOT / "docs" / "system-behaviors.yaml").read_text(encoding="utf-8")
    assert "PROVENANCE SCOPE (2026-07-27)" in text
    assert "NOT mutation-audited" in text


def test_the_2026_07_27_backfilled_behaviors_are_present_and_undrifted():
    # The drift check itself (AST-verified after the slice-1 fix) proves each named guarding_test
    # is live + gated; this test is the honesty anchor that the 8 ids actually landed in the SSOT.
    data = _load_behaviors_yaml()
    ids = {b["id"] for b in data["behaviors"] if isinstance(b, dict)}
    expected = {
        "harness-idea-capture",
        "backlog-tending",
        "phase-persona-routing",
        "cross-model-review-staffing",
        "graduated-gate-approval",
        "driver-liveness-watchdog",
        "sync-bootstrap-readmission",
        "dispatch-pause-continue-gc",
    }
    missing = expected - ids
    assert not missing, f"backfilled behavior ids missing from SSOT: {missing}"


def test_the_honest_gaps_list_names_the_unlanded_targets():
    data = _load_behaviors_yaml()
    gap_ids = {g["id"] for g in data.get("gaps", []) if isinstance(g, dict)}
    expected = {
        "idea-capture",
        "persona-runner-profiles",
        "gate-lane-policy",
        "driver-loop",
        "sync-admission-evidence",
        "idea-to-backlog",
        "spec-design-review-separation",
        "intent-to-fulfilled-unattended",
        "sync-readmission-live-clone-proof",
    }
    missing = expected - gap_ids
    assert not missing, f"expected gap ids missing: {missing}"
    behavior_ids = {b["id"] for b in data["behaviors"] if isinstance(b, dict)}
    assert not (gap_ids & behavior_ids), "a gaps id must never collide with a documented behavior id"


def test_the_drift_check_catches_a_documented_behavior_whose_test_is_missing(tmp_path):
    # A synthetic repo: a real test file with a REAL function, but the SSOT points at a name that
    # isn't there. The checker must REPORT it — proof the guard itself catches drift, not just passes.
    (tmp_path / "tests" / "unit").mkdir(parents=True)
    (tmp_path / "tests" / "unit" / "test_x.py").write_text("def test_real():\n    pass\n", encoding="utf-8")
    (tmp_path / "Makefile").write_text("gate:\n\tpytest tests/unit/test_x.py -q\n", encoding="utf-8")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "system-behaviors.yaml").write_text(
        "schema: system-behaviors/v1\n"
        "behaviors:\n"
        "  - id: bogus\n"
        "    area: x\n"
        "    behavior: b\n"
        "    invariant: i\n"
        "    breaks_when: w\n"
        "    guarding_tests:\n"
        "      - tests/unit/test_x.py::test_does_not_exist\n",
        encoding="utf-8",
    )
    findings = check_behavior_drift(tmp_path)
    assert any("test_does_not_exist" in f for f in findings), findings


def test_the_drift_check_flags_a_gaps_id_that_collides_with_a_behavior_id(tmp_path):
    # `gaps` is the honest-untested list; a gap sharing an id with a documented (verified)
    # behavior would let a claim quietly ride on the wrong id's meaning. Must be flagged.
    (tmp_path / "tests" / "unit").mkdir(parents=True)
    (tmp_path / "tests" / "unit" / "test_x.py").write_text("def test_real():\n    pass\n", encoding="utf-8")
    (tmp_path / "Makefile").write_text("gate:\n\tpytest tests/unit/test_x.py -q\n", encoding="utf-8")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "system-behaviors.yaml").write_text(
        "schema: system-behaviors/v1\n"
        "behaviors:\n"
        "  - id: shared-id\n"
        "    area: x\n"
        "    behavior: b\n"
        "    invariant: i\n"
        "    breaks_when: w\n"
        "    guarding_tests:\n"
        "      - tests/unit/test_x.py::test_real\n"
        "gaps:\n"
        "  - id: shared-id\n"
        "    area: x\n"
        "    gap: g\n"
        "    why_untested: w\n",
        encoding="utf-8",
    )
    findings = check_behavior_drift(tmp_path)
    assert any("collides" in f for f in findings), findings


def test_the_drift_check_flags_a_test_the_gate_never_runs(tmp_path):
    # The test exists, but its file is not in the gate — a behavior 'guarded' by an ungated test is
    # still unverified in CI, and the checker must say so.
    (tmp_path / "tests" / "unit").mkdir(parents=True)
    (tmp_path / "tests" / "unit" / "test_y.py").write_text("def test_here():\n    pass\n", encoding="utf-8")
    (tmp_path / "Makefile").write_text("gate:\n\tpytest tests/unit/test_other.py -q\n", encoding="utf-8")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "system-behaviors.yaml").write_text(
        "schema: system-behaviors/v1\n"
        "behaviors:\n"
        "  - id: ungated\n"
        "    area: x\n"
        "    behavior: b\n"
        "    invariant: i\n"
        "    breaks_when: w\n"
        "    guarding_tests:\n"
        "      - tests/unit/test_y.py::test_here\n",
        encoding="utf-8",
    )
    findings = check_behavior_drift(tmp_path)
    assert any("not run by" in f for f in findings), findings


def _write_synthetic_repo(tmp_path: Path, test_file_body: str, bid: str, test_name: str) -> None:
    (tmp_path / "tests" / "unit").mkdir(parents=True)
    (tmp_path / "tests" / "unit" / "test_x.py").write_text(test_file_body, encoding="utf-8")
    (tmp_path / "Makefile").write_text("gate:\n\tpytest tests/unit/test_x.py -q\n", encoding="utf-8")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "system-behaviors.yaml").write_text(
        "schema: system-behaviors/v1\n"
        "behaviors:\n"
        f"  - id: {bid}\n"
        "    area: x\n"
        "    behavior: b\n"
        "    invariant: i\n"
        "    breaks_when: w\n"
        "    guarding_tests:\n"
        f"      - tests/unit/test_x.py::{test_name}\n",
        encoding="utf-8",
    )


def test_the_drift_check_flags_a_commented_out_guarding_test(tmp_path):
    # A substring scan would be fooled by a commented-out `def test_x():` still sitting in the
    # file — the AST-based check must not count text that was never parsed as a function.
    _write_synthetic_repo(
        tmp_path,
        "# def test_x():\n#     pass\n",
        "commented",
        "test_x",
    )
    findings = check_behavior_drift(tmp_path)
    assert any("not defined" in f for f in findings), findings


def test_the_drift_check_flags_a_skip_decorated_guarding_test(tmp_path):
    # A test that exists but is decorated with @unittest.skip / @pytest.mark.skip never actually
    # runs — citing it as a guard is a false anchor, and the checker must say so.
    # (built via a marker string, not a literal "@pytest..." line, so the source text here never
    # contains an "n@pytest"-shaped run that the pre-publish scrub's email-pattern scan flags)
    decorator_line = "@" + "pytest.mark.skip(reason='wip')"
    body = "import pytest\n\n\n" + decorator_line + "\ndef test_skipped():\n    pass\n"
    _write_synthetic_repo(
        tmp_path,
        body,
        "skipped",
        "test_skipped",
    )
    findings = check_behavior_drift(tmp_path)
    assert any("skip/xfail-marked" in f for f in findings), findings


def test_the_drift_check_passes_a_plain_live_guarding_test(tmp_path):
    # The control case: a real, undecorated, module-level test in a gated file must produce no
    # findings at all — proof the AST check isn't just more paranoid than useful.
    _write_synthetic_repo(
        tmp_path,
        "def test_plain():\n    pass\n",
        "plain",
        "test_plain",
    )
    findings = check_behavior_drift(tmp_path)
    assert findings == [], findings


# ---------------------------------------------------------------------------------------------
# TypeScript/JavaScript guard resolution -- many repos adopting this are TS-majority; these
# prove the new mode without touching a single Python
# outcome above. Each helper below builds a synthetic TS repo the same way _write_synthetic_repo
# does for Python: a Makefile `gate:` recipe that runs the test file wholesale via vitest, plus a
# minimal system-behaviors.yaml pointing guarding_tests at a title in that file.
# ---------------------------------------------------------------------------------------------


def _write_synthetic_ts_repo(tmp_path: Path, test_file_body: str, bid: str, ref: str) -> None:
    (tmp_path / "src").mkdir(parents=True)
    (tmp_path / "src" / "thing.test.ts").write_text(test_file_body, encoding="utf-8")
    # `vitest run` with no path args -> wholesale coverage of every .ts under the repo.
    (tmp_path / "Makefile").write_text("gate:\n\tnpx vitest run\n", encoding="utf-8")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "system-behaviors.yaml").write_text(
        "schema: system-behaviors/v1\n"
        "behaviors:\n"
        f"  - id: {bid}\n"
        "    area: x\n"
        "    behavior: b\n"
        "    invariant: i\n"
        "    breaks_when: w\n"
        "    guarding_tests:\n"
        f"      - {ref}\n",
        encoding="utf-8",
    )


def test_the_drift_check_passes_a_live_ts_guarding_test(tmp_path):
    # A live, un-skipped `it(...)` in a gate-covered .test.ts file must produce no findings.
    _write_synthetic_ts_repo(
        tmp_path,
        "import { it, expect } from 'vitest'\n"
        "it('adds one plus one', () => { expect(1 + 1).toBe(2) })\n",
        "ts-live",
        "src/thing.test.ts::adds one plus one",
    )
    findings = check_behavior_drift(tmp_path)
    assert findings == [], findings


def _assert_ts_skip_finding(tmp_path: Path, body: str) -> None:
    # Each skip/xfail spelling the spec names must produce the SAME class of finding the
    # Python path reports for a skip/xfail-decorated test.
    _write_synthetic_ts_repo(tmp_path, body, "ts-skip", "src/thing.test.ts::adds one plus one")
    findings = check_behavior_drift(tmp_path)
    assert any("skip/xfail-marked" in f for f in findings), findings


def test_the_drift_check_flags_it_skip(tmp_path):
    _assert_ts_skip_finding(
        tmp_path,
        "import { it, expect } from 'vitest'\n"
        "it.skip('adds one plus one', () => { expect(1 + 1).toBe(2) })\n",
    )


def test_the_drift_check_flags_test_todo(tmp_path):
    _assert_ts_skip_finding(
        tmp_path,
        "import { test, expect } from 'vitest'\n"
        "test.todo('adds one plus one')\n",
    )


def test_the_drift_check_flags_xit(tmp_path):
    _assert_ts_skip_finding(
        tmp_path,
        "import { xit, expect } from 'vitest'\n"
        "xit('adds one plus one', () => { expect(1 + 1).toBe(2) })\n",
    )


def test_the_drift_check_flags_enclosing_describe_skip(tmp_path):
    _assert_ts_skip_finding(
        tmp_path,
        "import { describe, it, expect } from 'vitest'\n"
        "describe.skip('a suite', () => {\n"
        "  it('adds one plus one', () => { expect(1 + 1).toBe(2) })\n"
        "})\n",
    )


def test_the_drift_check_flags_a_dot_only_ts_test(tmp_path):
    # `.only` isn't a skip -- the referenced test itself still runs -- but it silently disables
    # every OTHER test in the file, which is its own drift the checker must name distinctly.
    _write_synthetic_ts_repo(
        tmp_path,
        "import { it, expect } from 'vitest'\n"
        "it.only('adds one plus one', () => { expect(1 + 1).toBe(2) })\n",
        "ts-only",
        "src/thing.test.ts::adds one plus one",
    )
    findings = check_behavior_drift(tmp_path)
    assert any(".only-marked" in f for f in findings), findings


def test_the_drift_check_flags_a_missing_ts_title(tmp_path):
    # The file and function-family exist, but no `it`/`test` call carries this exact title.
    _write_synthetic_ts_repo(
        tmp_path,
        "import { it, expect } from 'vitest'\n"
        "it('a totally different title', () => { expect(1 + 1).toBe(2) })\n",
        "ts-missing",
        "src/thing.test.ts::adds one plus one",
    )
    findings = check_behavior_drift(tmp_path)
    assert any("not defined in src/thing.test.ts" in f for f in findings), findings


def test_the_drift_check_flags_a_ts_guard_the_gate_never_runs(tmp_path):
    # Same file, but the Makefile gate only runs a DIFFERENT test file -- a live, correctly-named
    # TS test that the gate never executes is still an unverified claim.
    (tmp_path / "src").mkdir(parents=True)
    (tmp_path / "src" / "thing.test.ts").write_text(
        "import { it, expect } from 'vitest'\n"
        "it('adds one plus one', () => { expect(1 + 1).toBe(2) })\n",
        encoding="utf-8",
    )
    (tmp_path / "Makefile").write_text(
        "gate:\n\tnpx vitest run src/other.test.ts\n", encoding="utf-8"
    )
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "system-behaviors.yaml").write_text(
        "schema: system-behaviors/v1\n"
        "behaviors:\n"
        "  - id: ts-ungated\n"
        "    area: x\n"
        "    behavior: b\n"
        "    invariant: i\n"
        "    breaks_when: w\n"
        "    guarding_tests:\n"
        "      - src/thing.test.ts::adds one plus one\n",
        encoding="utf-8",
    )
    findings = check_behavior_drift(tmp_path)
    assert any("not run by" in f for f in findings), findings


def test_gate_with_explicit_ts_paths_covers_only_those_paths(tmp_path):
    # `vitest run <explicit path>` is NOT wholesale -- a sibling TS file the gate never names
    # must still be reported uncovered, proving explicit-path gates stay narrow.
    (tmp_path / "src").mkdir(parents=True)
    (tmp_path / "src" / "thing.test.ts").write_text(
        "import { it, expect } from 'vitest'\n"
        "it('adds one plus one', () => { expect(1 + 1).toBe(2) })\n",
        encoding="utf-8",
    )
    (tmp_path / "src" / "sibling.test.ts").write_text(
        "import { it, expect } from 'vitest'\n"
        "it('subtracts one', () => { expect(2 - 1).toBe(1) })\n",
        encoding="utf-8",
    )
    (tmp_path / "Makefile").write_text(
        "gate:\n\tnpx vitest run src/thing.test.ts\n", encoding="utf-8"
    )
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "system-behaviors.yaml").write_text(
        "schema: system-behaviors/v1\n"
        "behaviors:\n"
        "  - id: ts-covered\n"
        "    area: x\n"
        "    behavior: b\n"
        "    invariant: i\n"
        "    breaks_when: w\n"
        "    guarding_tests:\n"
        "      - src/thing.test.ts::adds one plus one\n"
        "  - id: ts-not-covered\n"
        "    area: x\n"
        "    behavior: b\n"
        "    invariant: i\n"
        "    breaks_when: w\n"
        "    guarding_tests:\n"
        "      - src/sibling.test.ts::subtracts one\n",
        encoding="utf-8",
    )
    findings = check_behavior_drift(tmp_path)
    assert not any("thing.test.ts is not run by" in f for f in findings), findings
    assert any("sibling.test.ts is not run by" in f for f in findings), findings


def test_pnpm_test_resolves_via_package_json_to_wholesale_vitest_coverage(tmp_path):
    # `pnpm test` on its own names nothing -- the checker must resolve ONE level of indirection
    # through package.json's scripts.test ("vitest run", no path args) to see the wholesale gate.
    (tmp_path / "src").mkdir(parents=True)
    (tmp_path / "src" / "thing.test.ts").write_text(
        "import { it, expect } from 'vitest'\n"
        "it('adds one plus one', () => { expect(1 + 1).toBe(2) })\n",
        encoding="utf-8",
    )
    (tmp_path / "Makefile").write_text("gate:\n\tpnpm test\n", encoding="utf-8")
    (tmp_path / "package.json").write_text(
        '{"scripts": {"test": "vitest run"}}\n', encoding="utf-8"
    )
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "system-behaviors.yaml").write_text(
        "schema: system-behaviors/v1\n"
        "behaviors:\n"
        "  - id: ts-pnpm\n"
        "    area: x\n"
        "    behavior: b\n"
        "    invariant: i\n"
        "    breaks_when: w\n"
        "    guarding_tests:\n"
        "      - src/thing.test.ts::adds one plus one\n",
        encoding="utf-8",
    )
    findings = check_behavior_drift(tmp_path)
    assert findings == [], findings


def test_missing_makefile_falls_back_to_setup_decisions_test_command(tmp_path):
    # No Makefile at all -- the checker must read .builder/setup-decisions.yaml's
    # commands.default.test as the repo's own definition of the armed gate, not raise.
    (tmp_path / "src").mkdir(parents=True)
    (tmp_path / "src" / "thing.test.ts").write_text(
        "import { it, expect } from 'vitest'\n"
        "it('adds one plus one', () => { expect(1 + 1).toBe(2) })\n",
        encoding="utf-8",
    )
    (tmp_path / ".builder").mkdir()
    (tmp_path / ".builder" / "setup-decisions.yaml").write_text(
        "commands:\n  default:\n    test: \"npx vitest run\"\n",
        encoding="utf-8",
    )
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "system-behaviors.yaml").write_text(
        "schema: system-behaviors/v1\n"
        "behaviors:\n"
        "  - id: ts-fallback\n"
        "    area: x\n"
        "    behavior: b\n"
        "    invariant: i\n"
        "    breaks_when: w\n"
        "    guarding_tests:\n"
        "      - src/thing.test.ts::adds one plus one\n",
        encoding="utf-8",
    )
    findings = check_behavior_drift(tmp_path)
    assert findings == [], findings


def test_missing_makefile_and_missing_setup_decisions_reports_uncovered_not_a_crash(tmp_path):
    # No Makefile, no setup-decisions.yaml -- must not raise; every behavior is honestly
    # reported as uncovered rather than the check aborting with a traceback.
    (tmp_path / "src").mkdir(parents=True)
    (tmp_path / "src" / "thing.test.ts").write_text(
        "import { it, expect } from 'vitest'\n"
        "it('adds one plus one', () => { expect(1 + 1).toBe(2) })\n",
        encoding="utf-8",
    )
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "system-behaviors.yaml").write_text(
        "schema: system-behaviors/v1\n"
        "behaviors:\n"
        "  - id: ts-nogate\n"
        "    area: x\n"
        "    behavior: b\n"
        "    invariant: i\n"
        "    breaks_when: w\n"
        "    guarding_tests:\n"
        "      - src/thing.test.ts::adds one plus one\n",
        encoding="utf-8",
    )
    findings = check_behavior_drift(tmp_path)
    assert any("not run by" in f for f in findings), findings


def test_python_guard_fixture_is_byte_identical_after_the_ts_mode_lands(tmp_path):
    # Regression anchor: an existing Python-guard fixture (skip-decorated) must yield the exact
    # same finding text as before the TS mode was added -- the new suffix dispatch must be a
    # no-op for .py refs.
    decorator_line = "@" + "pytest.mark.skip(reason='wip')"
    body = "import pytest\n\n\n" + decorator_line + "\ndef test_skipped():\n    pass\n"
    _write_synthetic_repo(tmp_path, body, "regression-skipped", "test_skipped")
    findings = check_behavior_drift(tmp_path)
    assert findings == [
        "regression-skipped: test 'test_skipped' is skip/xfail-marked — not a live guard"
    ], findings


def test_normalize_ref_restores_the_separator_yaml_consumed():
    """A ref reaches us pre-parsed by one of two producers that consumed different separators.

    (a) The lossy YAML shim turns 'PATH::name' into {PATH: ':name'} -- val keeps the colon.
    (b) Real PyYAML reading an UNQUOTED ref whose test title contains ': ' builds a mapping:
        'p.test.ts::single-low: $12' -> {'p.test.ts::single-low': '$12'}, consuming the ': '.

    Case (b) is the normal case for TypeScript guards -- vitest titles routinely contain colons.
    Reconstructing it without the space silently corrupts the title, and the guard is then reported
    "not defined" even though it is right there, which sends the author hunting a phantom.
    """
    assert _normalize_ref("a/b.py::test_x") == "a/b.py::test_x"
    assert _normalize_ref({"a/b.py": ":test_x"}) == "a/b.py::test_x"
    assert (
        _normalize_ref({"p.test.ts::single-low": "$12 -> 1 credit"})
        == "p.test.ts::single-low: $12 -> 1 credit"
    )


def _ssot_with_anchors(tmp_path: Path, anchors_block: str) -> Path:
    """A minimal repo whose one behavior is guarded by a real python test, plus `anchors`."""
    root = tmp_path
    (root / "tests" / "gate").mkdir(parents=True)
    (root / "tests" / "gate" / "test_g.py").write_text("def test_guard():\n    pass\n", encoding="utf-8")
    (root / "Makefile").write_text("gate:\n\tpython3 -m pytest tests/gate -q\n", encoding="utf-8")
    (root / "src").mkdir()
    (root / "src" / "bands.ts").write_text("export const X = { priceCents: 1200 }\n", encoding="utf-8")
    (root / "docs").mkdir()
    (root / "docs" / "system-behaviors.yaml").write_text(
        "schema: system-behaviors/v1\n"
        "behaviors:\n"
        "  - id: b1\n"
        "    area: pricing\n"
        "    behavior: b\n"
        "    invariant: i\n"
        "    guarding_tests:\n"
        "      - tests/gate/test_g.py::test_guard\n"
        "    breaks_when: w\n"
        + anchors_block,
        encoding="utf-8",
    )
    return root


def test_valid_anchor_produces_no_finding(tmp_path: Path) -> None:
    root = _ssot_with_anchors(
        tmp_path,
        "    anchors:\n      - path: src/bands.ts\n        contains: \"priceCents: 1200\"\n",
    )
    assert check_behavior_drift(root) == []


def test_anchor_whose_literal_moved_is_reported(tmp_path: Path) -> None:
    """The point of an anchor: it goes red when the value it pins changes."""
    root = _ssot_with_anchors(
        tmp_path,
        "    anchors:\n      - path: src/bands.ts\n        contains: \"priceCents: 1250\"\n",
    )
    findings = check_behavior_drift(root)
    assert any("anchor no longer present" in f for f in findings), findings


def test_anchor_pointing_at_a_missing_file_is_reported(tmp_path: Path) -> None:
    root = _ssot_with_anchors(
        tmp_path,
        "    anchors:\n      - path: src/gone.ts\n        contains: \"x\"\n",
    )
    assert any("anchor file not found" in f for f in check_behavior_drift(root))


def test_anchor_missing_contains_is_reported(tmp_path: Path) -> None:
    root = _ssot_with_anchors(tmp_path, "    anchors:\n      - path: src/bands.ts\n")
    assert any("needs both `path` and a non-empty `contains`" in f for f in check_behavior_drift(root))


def test_behaviors_without_anchors_are_unaffected(tmp_path: Path) -> None:
    """`anchors` is optional -- a repo that never adopted it must behave exactly as before."""
    root = _ssot_with_anchors(tmp_path, "")
    assert check_behavior_drift(root) == []
