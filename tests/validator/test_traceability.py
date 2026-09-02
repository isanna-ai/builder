from __future__ import annotations

import os
from pathlib import Path

from scripts._validators.traceability import validate_traceability


def _errors_enforced(data: dict, name: str, spec: Path) -> str:
    """Coverage checks are warn-staged by default; force BUILDER_TRACE_COVERAGE=enforce
    so a coverage gap surfaces as a hard error. Shim-safe env set/restore (no monkeypatch)."""
    prior = os.environ.get("BUILDER_TRACE_COVERAGE")
    os.environ["BUILDER_TRACE_COVERAGE"] = "enforce"
    try:
        return "\n".join(validate_traceability(data, name, spec))
    finally:
        if prior is None:
            os.environ.pop("BUILDER_TRACE_COVERAGE", None)
        else:
            os.environ["BUILDER_TRACE_COVERAGE"] = prior


REQUIREMENTS = """artifact: requirements
title: Demo requirements
spec: demo
requirements:
  - id: R1
    title: First requirement
    user_story: As a maintainer, I want the first thing so that it is traced.
    acceptance:
      - WHEN the first thing happens, the system SHALL record it.
  - id: R2
    title: Second requirement
    user_story: As a maintainer, I want the second thing so that it is traced.
    acceptance:
      - WHEN the second thing happens, the system SHALL record it.
"""


DESIGN = """artifact: design
title: Demo design
spec: demo
responsibility_allocation:
  - id: D9
    surface: validator
    keep: existing behavior
    change: add coverage checks; on unreadable input SHALL error
    why: R1
core_changes:
  - id: D1
    title: First change
    summary: Implements R1; on invalid input returns an error.
    requirements: [R1]
  - id: D2
    title: Second change
    summary: Implements R2; on invalid input returns an error.
    requirements: [R2]
telemetry_strategy:
  - Record adoption later.
verification_strategy:
  - command: python3 scripts/validate-spec.py demo --strict
"""


TASKS = """artifact: tasks
title: Demo tasks
spec: demo
tasks:
  - id: T1
    title: Build R1
    repo: builder
    files: [a.py]
    steps:
      - text: Do it.
    verify:
      - command: python3 -m pytest -q
    done_when: Done.
    tdd:
      mode: required
    depends_on: []
    parallel_with: []
  - id: T2
    title: Build R2
    repo: builder
    files: [b.py]
    steps:
      - text: Do it.
    verify:
      - command: python3 -m pytest -q
    done_when: Done.
    tdd:
      mode: required
    depends_on: []
    parallel_with: []
"""


# A design.yaml that is READABLE (parses cleanly) but declares NO design ids, and a
# requirements.yaml that is readable but declares NO requirement ids. These exercise
# the enforce-airtightness gap: a link that references an id absent from an
# empty-but-readable source must surface as an unknown reference (not silently pass).
EMPTY_DESIGN = """artifact: design
title: Demo design
spec: demo
responsibility_allocation: []
core_changes: []
telemetry_strategy:
  - Record adoption later.
verification_strategy:
  - command: python3 scripts/validate-spec.py demo --strict
"""


EMPTY_REQUIREMENTS = """artifact: requirements
title: Demo requirements
spec: demo
requirements: []
"""


# A LEGACY design.yaml: readable and NON-empty, but every design id is FREE-FORM (none
# match the canonical ^D[0-9]+$ convention). A spec whose design carries no canonical
# D-id has NOT adopted the new methodology, so it is grandfathered — its coverage gaps
# stay advisory (stderr) even under enforce.
LEGACY_DESIGN = """artifact: design
title: Legacy demo design
spec: demo
core_changes:
  - id: billing-role-migration
    title: Legacy change
    summary: Implements R1 with a free-form id.
    requirements: [R1]
  - id: billing-role-backfill
    title: Legacy change two
    summary: Implements R2 with a free-form id.
    requirements: [R2]
telemetry_strategy:
  - Record adoption later.
verification_strategy:
  - command: python3 scripts/validate-spec.py demo --strict
"""


def _full_coverage_data() -> dict:
    return {
        "artifact": "traceability",
        "spec": "demo",
        "requirement_links": [
            {"requirement_id": "R1", "design_ids": ["D1"]},
            {"requirement_id": "R2", "design_ids": ["D2"]},
        ],
        "design_links": [
            {"design_id": "D1", "task_ids": ["T1"]},
            {"design_id": "D2", "task_ids": ["T2"]},
            # D9 is the responsibility_allocation design id; it must also decompose into
            # a task for the fixture to be genuinely full-coverage under enforce.
            {"design_id": "D9", "task_ids": ["T1"]},
        ],
        "task_links": [
            {"task_id": "T1", "files": [{"path": "a.py", "relevance": "primary"}], "evidence_ids": ["E1"]},
            {"task_id": "T2", "files": [{"path": "b.py", "relevance": "primary"}], "evidence_ids": ["E2"]},
        ],
    }


def _make_spec(tmp_path: Path, *, requirements_text: str = REQUIREMENTS, design_text: str = DESIGN) -> Path:
    spec = tmp_path / ".builder" / "specs" / "demo"
    spec.mkdir(parents=True)
    (spec / "requirements.yaml").write_text(requirements_text, encoding="utf-8")
    (spec / "design.yaml").write_text(design_text, encoding="utf-8")
    (spec / "tasks.yaml").write_text(TASKS, encoding="utf-8")
    evidence = spec / "evidence"
    evidence.mkdir()
    (evidence / "task-1.yaml").write_text("entries:\n  - id: E1\n", encoding="utf-8")
    (evidence / "task-2.yaml").write_text("entries:\n  - id: E2\n", encoding="utf-8")
    return spec


def test_full_coverage_passes(tmp_path: Path) -> None:
    spec = _make_spec(tmp_path)
    assert validate_traceability(_full_coverage_data(), "traceability.yaml", spec) == []


def test_requirement_without_design_link_errors(tmp_path: Path) -> None:
    spec = _make_spec(tmp_path)
    data = _full_coverage_data()
    # Drop the R2 requirement link entirely.
    data["requirement_links"] = [entry for entry in data["requirement_links"] if entry["requirement_id"] != "R2"]
    errors = _errors_enforced(data, "traceability.yaml", spec)
    assert "requirement `R2`" in errors
    assert "not traced to any design" in errors


def test_empty_design_ids_errors(tmp_path: Path) -> None:
    spec = _make_spec(tmp_path)
    data = _full_coverage_data()
    data["requirement_links"][1]["design_ids"] = []
    errors = _errors_enforced(data, "traceability.yaml", spec)
    assert "design_ids is empty" in errors
    assert "R2" in errors


def test_design_without_task_link_errors(tmp_path: Path) -> None:
    spec = _make_spec(tmp_path)
    data = _full_coverage_data()
    # Drop the D2 design link entirely.
    data["design_links"] = [entry for entry in data["design_links"] if entry["design_id"] != "D2"]
    errors = _errors_enforced(data, "traceability.yaml", spec)
    assert "design `D2`" in errors
    assert "not decomposed into any task" in errors


def test_empty_task_ids_errors(tmp_path: Path) -> None:
    spec = _make_spec(tmp_path)
    data = _full_coverage_data()
    data["design_links"][1]["task_ids"] = []
    errors = _errors_enforced(data, "traceability.yaml", spec)
    assert "task_ids is empty" in errors
    assert "D2" in errors


def test_unparseable_requirements_is_error_not_silent_pass(tmp_path: Path) -> None:
    # A top-level sequence is not a mapping, so requirements.yaml fails to parse.
    spec = _make_spec(tmp_path, requirements_text="- not\n- a\n- mapping\n")
    errors = _errors_enforced(_full_coverage_data(), "traceability.yaml", spec)
    assert "requirement coverage cannot be verified" in errors


def test_core_change_referencing_unknown_requirement_errors(tmp_path: Path) -> None:
    bad_design = DESIGN.replace("requirements: [R2]", "requirements: [R2, R9]")
    spec = _make_spec(tmp_path, design_text=bad_design)
    errors = _errors_enforced(_full_coverage_data(), "traceability.yaml", spec)
    assert "references unknown requirement `R9`" in errors


def test_coverage_gap_is_warn_by_default_not_blocking(tmp_path: Path) -> None:
    # Staging contract: under the default (BUILDER_TRACE_COVERAGE unset/warn) a
    # coverage gap must NOT block — it is advisory only. Guard the env is unset.
    os.environ.pop("BUILDER_TRACE_COVERAGE", None)
    spec = _make_spec(tmp_path)
    data = _full_coverage_data()
    data["requirement_links"] = [e for e in data["requirement_links"] if e["requirement_id"] != "R2"]
    # R2 is now uncovered, but under warn this yields no hard error.
    assert validate_traceability(data, "traceability.yaml", spec) == []


def test_empty_but_readable_design_unknown_ref_grandfathered_for_legacy(tmp_path: Path) -> None:
    # design.yaml is READABLE but declares NO design ids (a LEGACY spec — no canonical D-id).
    # A link to an absent id is an empty-but-readable coverage gap, GRANDFATHERED to advisory
    # only, even under enforce — so flipping enforce does not break legacy specs.
    spec = _make_spec(tmp_path, design_text=EMPTY_DESIGN)
    data = _full_coverage_data()
    enforced = _errors_enforced(data, "traceability.yaml", spec)
    assert "references unknown design `D1`" not in enforced  # grandfathered -> advisory
    os.environ.pop("BUILDER_TRACE_COVERAGE", None)
    assert validate_traceability(data, "traceability.yaml", spec) == []


def test_legacy_freeform_design_coverage_grandfathered_under_enforce(tmp_path: Path) -> None:
    # A LEGACY spec whose design uses FREE-FORM ids (no canonical ^D[0-9]+$) is grandfathered:
    # a real trace-coverage gap (an uncovered requirement) stays advisory even under enforce,
    # so TRACE_COVERAGE=enforce can be flipped without breaking the existing corpus.
    legacy_design = (
        DESIGN.replace("- id: D1", "- id: alpha_change")
        .replace("- id: D2", "- id: beta_change")
        .replace("- id: D9", "- id: alpha_alloc")
    )
    spec = _make_spec(tmp_path, design_text=legacy_design)
    data = {
        "artifact": "traceability",
        "spec": "demo",
        "requirement_links": [{"requirement_id": "R1", "design_ids": ["alpha_change"]}],  # R2 uncovered
        "design_links": [
            {"design_id": "alpha_change", "task_ids": ["T1"]},
            {"design_id": "beta_change", "task_ids": ["T2"]},
            {"design_id": "alpha_alloc", "task_ids": ["T1"]},
        ],
        "task_links": [
            {"task_id": "T1", "files": [{"path": "a.py", "relevance": "primary"}], "evidence_ids": ["E1"]},
            {"task_id": "T2", "files": [{"path": "b.py", "relevance": "primary"}], "evidence_ids": ["E2"]},
        ],
    }
    enforced = _errors_enforced(data, "traceability.yaml", spec)
    assert "requirement `R2`" not in enforced  # uncovered but grandfathered -> advisory only


def test_empty_but_readable_requirements_unknown_ref_errors_under_enforce_only(tmp_path: Path) -> None:
    # requirements.yaml is READABLE but declares NO requirement ids. A requirement_links
    # entry referencing R1 is an unknown reference: enforce -> hard error, warn -> advisory.
    spec = _make_spec(tmp_path, requirements_text=EMPTY_REQUIREMENTS)
    data = _full_coverage_data()
    enforced = _errors_enforced(data, "traceability.yaml", spec)
    assert "references unknown requirement `R1`" in enforced
    os.environ.pop("BUILDER_TRACE_COVERAGE", None)
    assert validate_traceability(data, "traceability.yaml", spec) == []


def test_task_without_evidence_errors_under_enforce_only(tmp_path: Path) -> None:
    # A task_link with empty evidence_ids is a coverage gap: enforce -> hard error,
    # warn (default) -> advisory only (keeps the branch non-breaking).
    spec = _make_spec(tmp_path)
    data = _full_coverage_data()
    data["task_links"][0]["evidence_ids"] = []  # T1 now carries no evidence.
    enforced = _errors_enforced(data, "traceability.yaml", spec)
    assert "task `T1` has no evidence" in enforced
    os.environ.pop("BUILDER_TRACE_COVERAGE", None)
    assert validate_traceability(data, "traceability.yaml", spec) == []


def test_full_coverage_passes_under_enforce(tmp_path: Path) -> None:
    # Regression: the canonical full-coverage fixture stays clean even under enforce —
    # every requirement, design (incl. the responsibility_allocation D9), task, and
    # task->evidence link is satisfied, so there are no hard errors.
    spec = _make_spec(tmp_path)
    assert _errors_enforced(_full_coverage_data(), "traceability.yaml", spec) == ""


# --- acceptance coverage (structured must-criteria proven by verify[].proves) -------

STRUCTURED_REQUIREMENTS = """artifact: requirements
title: Demo requirements
spec: demo
requirements:
  - id: R1
    title: First requirement
    user_story: As a maintainer, I want the first thing so that it is traced.
    acceptance:
      - id: AC-R1-1
        statement: WHEN the first thing happens, the system SHALL record it.
        observable_at: exit code of the check
        oracle:
          type: automated_test
          expected: the record exists
        priority: must
"""


TASKS_WITH_PROVES = """artifact: tasks
title: Demo tasks
spec: demo
tasks:
  - id: T1
    title: Build R1
    repo: builder
    files: [a.py]
    steps:
      - text: Do it.
    verify:
      - command: python3 -m pytest -q
        proves: [AC-R1-1]
    done_when: Done.
    tdd:
      mode: required
    depends_on: []
    parallel_with: []
"""


TASKS_WITHOUT_PROVES = """artifact: tasks
title: Demo tasks
spec: demo
tasks:
  - id: T1
    title: Build R1
    repo: builder
    files: [a.py]
    steps:
      - text: Do it.
    verify:
      - command: python3 -m pytest -q
    done_when: Done.
    tdd:
      mode: required
    depends_on: []
    parallel_with: []
"""


def _make_accept_spec(tmp_path: Path, *, requirements_text: str, tasks_text: str) -> Path:
    # Minimal spec dir exercising ONLY the acceptance-coverage path (requirements.yaml +
    # tasks.yaml). Other coverage gaps (no design.yaml etc.) are irrelevant here; the
    # assertions target the acceptance-criterion message substrings specifically.
    spec = tmp_path / ".builder" / "specs" / "demo"
    spec.mkdir(parents=True)
    (spec / "requirements.yaml").write_text(requirements_text, encoding="utf-8")
    (spec / "tasks.yaml").write_text(tasks_text, encoding="utf-8")
    return spec


def test_must_acceptance_uncovered_by_proves_errors_under_enforce_only(tmp_path: Path) -> None:
    # A structured `priority: must` criterion with no covering verify[].proves is a
    # coverage gap: hard error under enforce, advisory-only (no hard error) under warn.
    spec = _make_accept_spec(
        tmp_path, requirements_text=STRUCTURED_REQUIREMENTS, tasks_text=TASKS_WITHOUT_PROVES
    )
    data = {"artifact": "traceability", "spec": "demo"}
    enforced = _errors_enforced(data, "traceability.yaml", spec)
    assert "acceptance criterion `AC-R1-1` (priority: must) is not proven" in enforced
    # Default (warn) stays non-breaking: the acceptance gap never becomes a hard error.
    os.environ.pop("BUILDER_TRACE_COVERAGE", None)
    warned = "\n".join(validate_traceability(data, "traceability.yaml", spec))
    assert "AC-R1-1" not in warned


def test_must_acceptance_covered_by_proves_passes_under_enforce(tmp_path: Path) -> None:
    # When a task's verify[].proves references the must criterion, there is no gap.
    spec = _make_accept_spec(
        tmp_path, requirements_text=STRUCTURED_REQUIREMENTS, tasks_text=TASKS_WITH_PROVES
    )
    data = {"artifact": "traceability", "spec": "demo"}
    enforced = _errors_enforced(data, "traceability.yaml", spec)
    assert "AC-R1-1" not in enforced


def test_string_only_acceptance_has_no_acceptance_coverage_gap(tmp_path: Path) -> None:
    # Legacy string-form acceptance exposes NO must-priority ids, so the acceptance
    # coverage check is skipped entirely — even under enforce. This is the guarantee
    # that the entire existing (string-only) corpus is non-breaking.
    spec = _make_accept_spec(
        tmp_path, requirements_text=REQUIREMENTS, tasks_text=TASKS_WITHOUT_PROVES
    )
    data = {"artifact": "traceability", "spec": "demo"}
    enforced = _errors_enforced(data, "traceability.yaml", spec)
    assert "is not proven by any task" not in enforced
    assert "acceptance criterion" not in enforced
