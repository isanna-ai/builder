"""Tests for scripts/backfill-traceability.py.

The script filename contains a dash, so it is loaded by path via importlib rather
than imported as a module. Covered:
  - free-form design ids -> D1/D2/... and traceability design_ids/design_id rewritten
  - a design already using D-ids -> idempotent no-op (no D-id churn, no diff)
  - the gap report lists an uncovered requirement / untasked design / unevidenced task
  - dry-run (the default) writes nothing; only --apply writes
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "backfill-traceability.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("backfill_traceability", _SCRIPT_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # Register before exec: dataclasses (Py>=3.14) resolve annotations via
    # sys.modules[cls.__module__]; a path-loaded module must be discoverable there.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


bt = _load_module()


# --- fixtures ---------------------------------------------------------------- #

DESIGN_FREEFORM = """artifact: design
title: Demo design
spec: demo
responsibility_allocation:
  - surface: validator surface
    keep: existing behavior
    change: add coverage checks
    why: R1
core_changes:
  - id: alpha
    title: First change
    summary: >-
      A folded summary
      spanning two lines.
    requirements: [R1]
  - id: beta
    title: Second change
    summary: Plain one-line summary.
    requirements: [R2]
telemetry_strategy:
  - Record adoption later.
verification_strategy:
  - command: python3 scripts/validate-spec.py demo
"""

TRACE_FREEFORM = """artifact: traceability
spec: demo
requirement_links:
  - requirement_id: R1
    design_ids: [alpha]
  - requirement_id: R2
    design_ids: [beta]
design_links:
  - design_id: alpha
    task_ids: [T1]
  - design_id: beta
    task_ids: [T2]
task_links:
  - task_id: T1
    files:
      - path: a.py
        relevance: primary
    evidence_ids: [E1]
"""

REQUIREMENTS = """artifact: requirements
title: Demo
spec: demo
requirements:
  - id: R1
    title: First
    user_story: As a maintainer, I want the first thing.
    acceptance:
      - WHEN a thing happens, the system SHALL record it.
  - id: R2
    title: Second
    user_story: As a maintainer, I want the second thing.
    acceptance:
      - WHEN another thing happens, the system SHALL record it.
"""

TASKS = """artifact: tasks
title: Demo
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


def _make_spec(tmp_path: Path, *, design: str, trace: str | None,
               requirements: str = REQUIREMENTS, tasks: str = TASKS) -> Path:
    spec = tmp_path / ".builder" / "specs" / "demo"
    spec.mkdir(parents=True)
    (spec / "design.yaml").write_text(design, encoding="utf-8")
    if trace is not None:
        (spec / "traceability.yaml").write_text(trace, encoding="utf-8")
    (spec / "requirements.yaml").write_text(requirements, encoding="utf-8")
    (spec / "tasks.yaml").write_text(tasks, encoding="utf-8")
    return spec


# --- tests ------------------------------------------------------------------- #

def test_freeform_ids_get_d_ids_and_traceability_is_rewritten(tmp_path: Path) -> None:
    spec = _make_spec(tmp_path, design=DESIGN_FREEFORM, trace=TRACE_FREEFORM)
    result = bt.process_spec(spec, apply=True)

    # core_changes are numbered first (document order), then responsibility_allocation.
    assert result.design.rename_map["alpha"] == "D1"
    assert result.design.rename_map["beta"] == "D2"
    # The responsibility_allocation entry (no prior id) gets the next number, D3.
    assert result.design.final_ids == {"D1", "D2", "D3"}
    added = [m for m in result.design.mapping if m[3] == "added"]
    assert added and added[0][0] == "D3"

    design_out = (spec / "design.yaml").read_text(encoding="utf-8")
    assert "id: D1" in design_out and "id: D2" in design_out and "id: D3" in design_out
    assert "id: alpha" not in design_out and "id: beta" not in design_out
    # The folded block scalar and its content survive the surgical edit.
    assert "summary: >-" in design_out
    assert "A folded summary" in design_out

    trace_out = (spec / "traceability.yaml").read_text(encoding="utf-8")
    assert "design_ids: [D1]" in trace_out
    assert "design_ids: [D2]" in trace_out
    assert "design_id: D1" in trace_out
    assert "design_id: D2" in trace_out
    assert "alpha" not in trace_out and "beta" not in trace_out
    assert result.wrote == ["design.yaml", "traceability.yaml"]


def test_already_d_ids_is_idempotent(tmp_path: Path) -> None:
    design = """artifact: design
title: Demo design
spec: demo
core_changes:
  - id: D1
    title: First change
    summary: Plain summary.
    requirements: [R1]
responsibility_allocation:
  - id: D2
    surface: validator surface
    keep: existing behavior
    change: add coverage checks
    why: R1
telemetry_strategy:
  - Record adoption later.
verification_strategy:
  - command: python3 scripts/validate-spec.py demo
"""
    trace = """artifact: traceability
spec: demo
requirement_links:
  - requirement_id: R1
    design_ids: [D1, D2]
design_links:
  - design_id: D1
    task_ids: [T1]
  - design_id: D2
    task_ids: [T1]
task_links:
  - task_id: T1
    files:
      - path: a.py
        relevance: primary
    evidence_ids: [E1]
"""
    spec = _make_spec(tmp_path, design=design, trace=trace)
    design_before = (spec / "design.yaml").read_text(encoding="utf-8")
    trace_before = (spec / "traceability.yaml").read_text(encoding="utf-8")

    result = bt.process_spec(spec, apply=True)

    assert result.design.changed is False
    assert result.trace.changed is False
    assert result.wrote == []
    # Existing D-ids are kept as-is (no churn).
    assert all(m[3] == "kept" for m in result.design.mapping)
    # Bytes are unchanged.
    assert (spec / "design.yaml").read_text(encoding="utf-8") == design_before
    assert (spec / "traceability.yaml").read_text(encoding="utf-8") == trace_before


def test_apply_then_reapply_is_a_no_op(tmp_path: Path) -> None:
    spec = _make_spec(tmp_path, design=DESIGN_FREEFORM, trace=TRACE_FREEFORM)
    bt.process_spec(spec, apply=True)
    design_after = (spec / "design.yaml").read_text(encoding="utf-8")
    trace_after = (spec / "traceability.yaml").read_text(encoding="utf-8")

    result2 = bt.process_spec(spec, apply=True)
    assert result2.design.changed is False
    assert result2.trace.changed is False
    assert result2.wrote == []
    assert (spec / "design.yaml").read_text(encoding="utf-8") == design_after
    assert (spec / "traceability.yaml").read_text(encoding="utf-8") == trace_after


def test_gap_report_lists_uncovered_requirement_untasked_design_and_unevidenced_task(tmp_path: Path) -> None:
    # R2 has no requirement_links entry (uncovered), beta/D2 has no design_links entry
    # (untasked), and T1 carries empty evidence_ids (unevidenced).
    trace = """artifact: traceability
spec: demo
requirement_links:
  - requirement_id: R1
    design_ids: [alpha]
design_links:
  - design_id: alpha
    task_ids: [T1]
task_links:
  - task_id: T1
    files:
      - path: a.py
        relevance: primary
    evidence_ids: []
"""
    spec = _make_spec(tmp_path, design=DESIGN_FREEFORM, trace=trace)
    result = bt.process_spec(spec, apply=False)
    joined = "\n".join(result.gaps)

    assert "requirement `R2` has no requirement_links entry" in joined
    assert "design `D2` has no design_links entry" in joined  # beta -> D2, untasked
    assert "`T1`" in joined and "evidence" in joined


def test_dry_run_writes_nothing(tmp_path: Path) -> None:
    spec = _make_spec(tmp_path, design=DESIGN_FREEFORM, trace=TRACE_FREEFORM)
    design_before = (spec / "design.yaml").read_text(encoding="utf-8")
    trace_before = (spec / "traceability.yaml").read_text(encoding="utf-8")

    result = bt.process_spec(spec, apply=False)

    # It WOULD change (dry-run detected work) but wrote nothing.
    assert result.design.changed is True
    assert result.trace.changed is True
    assert result.wrote == []
    assert (spec / "design.yaml").read_text(encoding="utf-8") == design_before
    assert (spec / "traceability.yaml").read_text(encoding="utf-8") == trace_before


def test_cli_default_is_dry_run(tmp_path: Path) -> None:
    spec = _make_spec(tmp_path, design=DESIGN_FREEFORM, trace=TRACE_FREEFORM)
    design_before = (spec / "design.yaml").read_text(encoding="utf-8")

    rc = bt.main([str(spec)])  # no --apply

    assert rc == 0
    assert (spec / "design.yaml").read_text(encoding="utf-8") == design_before


def test_missing_traceability_is_reported_not_crashed(tmp_path: Path) -> None:
    spec = _make_spec(tmp_path, design=DESIGN_FREEFORM, trace=None)
    result = bt.process_spec(spec, apply=True)

    # design.yaml still gets D-ids; traceability absence is a reported gap, not a write.
    assert result.wrote == ["design.yaml"]
    assert result.trace.present is False
    assert any("traceability.yaml is missing" in g for g in result.gaps)


def test_block_form_design_ids_are_rewritten(tmp_path: Path) -> None:
    # design_ids expressed as a block list (not inline [...]) must also be rewritten.
    trace = """artifact: traceability
spec: demo
requirement_links:
  - requirement_id: R1
    design_ids:
      - alpha
      - beta
design_links:
  - design_id: alpha
    task_ids: [T1]
task_links:
  - task_id: T1
    files:
      - path: a.py
        relevance: primary
    evidence_ids: [E1]
"""
    spec = _make_spec(tmp_path, design=DESIGN_FREEFORM, trace=trace)
    bt.process_spec(spec, apply=True)
    trace_out = (spec / "traceability.yaml").read_text(encoding="utf-8")
    assert "- D1" in trace_out and "- D2" in trace_out
    assert "alpha" not in trace_out and "beta" not in trace_out


def test_unresolved_reference_left_untouched_and_reported(tmp_path: Path) -> None:
    # A design reference that matches no design id/title is left as-is and reported.
    trace = """artifact: traceability
spec: demo
requirement_links:
  - requirement_id: R1
    design_ids: [alpha, ghost]
design_links:
  - design_id: alpha
    task_ids: [T1]
task_links:
  - task_id: T1
    files:
      - path: a.py
        relevance: primary
    evidence_ids: [E1]
"""
    spec = _make_spec(tmp_path, design=DESIGN_FREEFORM, trace=trace)
    result = bt.process_spec(spec, apply=True)
    trace_out = (spec / "traceability.yaml").read_text(encoding="utf-8")
    assert "ghost" in trace_out  # untouched
    assert any(tok == "ghost" for _, tok in result.trace.unresolved)
    assert any("`ghost`" in g for g in result.gaps)
