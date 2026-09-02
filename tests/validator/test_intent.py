from __future__ import annotations

from pathlib import Path

from scripts._validators.common import ValidationContext
from scripts._validators.intent import run
from scripts._validators.traceability import validate_traceability


SYSTEM_MODEL = """version: 1
what:
  entities:
    - id: purchase_flow
      name: Purchase flow
  capabilities:
    - id: place_order
      name: Place order
who:
  actors:
    - id: shopper
      name: Shopper
      capabilities: [place_order]
when:
  events: []
where:
  boundaries: []
why:
  rules:
    - id: trust_rule
      statement: Orders must remain trustworthy.
      applies_to: [purchase_flow, place_order]
how:
  behaviors:
    - capability: place_order
      success: Shopper can place an order.
      failures: [order is silently dropped]
upstream:
  sources: []
downstream:
  sinks: []
"""


INTENT = """artifact: intent
title: Intent-first checkout
spec: demo
outcome: Reduce checkout ambiguity for AI-assisted runs.
goal:
  summary: Make the desired outcome explicit before downstream planning.
references:
  system_model: [purchase_flow, place_order, trust_rule]
constraints:
  - id: C1
    statement: Preserve current approval gates for this additive rollout.
failure_conditions:
  - id: F1
    statement: Do not introduce a second competing source of truth for system state.
success_signals:
  - id: S1
    statement: Intent can be validated independently before requirements are written.
non_goals:
  - Collapse workflow phases in this PR.
"""


TRACEABILITY = """artifact: traceability
spec: demo
intent_links:
  - intent_id: C1
    requirement_ids: [R1]
requirement_links:
  - requirement_id: R1
    design_ids: [D1]
design_links:
  - design_id: D1
    task_ids: [T1]
task_links:
  - task_id: T1
    files:
      - path: README.md
        relevance: supporting
    evidence_ids: [E1]
"""


REQUIREMENTS = """artifact: requirements
title: Demo requirements
spec: demo
requirements:
  - id: R1
    title: Intent is first-class
    user_story: As a maintainer, I want intent captured explicitly so that downstream artifacts can reference it.
    acceptance:
      - WHEN a new spec starts, the system SHALL allow intent.yaml to exist before requirements expansion.
"""


DESIGN = """artifact: design
title: Demo design
spec: demo
responsibility_allocation:
  - surface: validator
    keep: schema loading
    change: add intent validation
    why: additive rollout
core_changes:
  - id: D1
    title: Validate intent
    summary: Add intent validation without changing downstream behavior.
telemetry_strategy:
  - Record additive intent adoption later.
verification_strategy:
  - command: python3 scripts/validate-spec.py demo --strict
"""


TASKS = """artifact: tasks
title: Demo tasks
spec: demo
tasks:
  - id: T1
    title: Add intent validator
    repo: builder
    files: [schemas/intent.schema.yaml, scripts/_validators/intent.py]
    steps:
      - text: Add additive intent validation.
    verify:
      - command: python3 -m pytest tests/validator/test_intent.py -q
    done_when: Intent validator passes.
    tdd:
      mode: required
    depends_on: []
    parallel_with: []
"""


def make_spec(tmp_path: Path, *, intent_text: str = INTENT) -> Path:
    spec = tmp_path / ".builder" / "specs" / "demo"
    spec.mkdir(parents=True)
    (spec / "intent.yaml").write_text(intent_text, encoding="utf-8")
    (spec / "system-model.yaml").write_text(SYSTEM_MODEL, encoding="utf-8")
    (spec / "requirements.yaml").write_text(REQUIREMENTS, encoding="utf-8")
    (spec / "design.yaml").write_text(DESIGN, encoding="utf-8")
    (spec / "tasks.yaml").write_text(TASKS, encoding="utf-8")
    (spec / "traceability.yaml").write_text(TRACEABILITY, encoding="utf-8")
    return spec


def test_intent_validator_accepts_valid_intent(tmp_path: Path) -> None:
    spec = make_spec(tmp_path)
    assert run(ValidationContext(spec)).errors == []


def test_intent_validator_rejects_unknown_system_model_reference(tmp_path: Path) -> None:
    spec = make_spec(tmp_path, intent_text=INTENT.replace("trust_rule", "missing_rule"))
    errors = "\n".join(run(ValidationContext(spec)).errors)
    assert "unknown system-model reference `missing_rule`" in errors


def test_intent_validator_reports_duplicate_ids(tmp_path: Path) -> None:
    duplicate_intent = INTENT.replace(
        "success_signals:\n  - id: S1\n    statement: Intent can be validated independently before requirements are written.",
        "success_signals:\n  - id: C1\n    statement: Intent can be validated independently before requirements are written.",
    )
    spec = make_spec(tmp_path, intent_text=duplicate_intent)
    errors = "\n".join(run(ValidationContext(spec)).errors)
    assert "duplicate id `C1`" in errors


def test_intent_validator_reports_missing_system_model_for_reference_checks(tmp_path: Path) -> None:
    spec = make_spec(tmp_path)
    (spec / "system-model.yaml").unlink()
    errors = "\n".join(run(ValidationContext(spec)).errors)
    assert "system-model reference check skipped" in errors


def test_intent_validator_rejects_empty_non_goal_entry(tmp_path: Path) -> None:
    spec = make_spec(tmp_path, intent_text=INTENT.replace("- Collapse workflow phases in this PR.", "- \"\""))
    errors = "\n".join(run(ValidationContext(spec)).errors)
    assert "non_goals" in errors


def test_traceability_accepts_optional_intent_links(tmp_path: Path) -> None:
    spec = make_spec(tmp_path)
    data = {
        "artifact": "traceability",
        "spec": "demo",
        "intent_links": [{"intent_id": "C1", "requirement_ids": ["R1"]}],
        "requirement_links": [{"requirement_id": "R1", "design_ids": ["D1"]}],
        "design_links": [{"design_id": "D1", "task_ids": ["T1"]}],
        "task_links": [{"task_id": "T1", "files": [{"path": "README.md", "relevance": "supporting"}], "evidence_ids": ["E1"]}],
    }
    assert validate_traceability(data, "traceability.yaml", spec) == []


def test_traceability_rejects_unknown_intent_reference(tmp_path: Path) -> None:
    spec = make_spec(tmp_path)
    data = {
        "artifact": "traceability",
        "spec": "demo",
        "intent_links": [{"intent_id": "missing", "requirement_ids": ["R1"]}],
        "requirement_links": [{"requirement_id": "R1", "design_ids": ["D1"]}],
        "design_links": [{"design_id": "D1", "task_ids": ["T1"]}],
        "task_links": [{"task_id": "T1", "files": [{"path": "README.md", "relevance": "supporting"}], "evidence_ids": ["E1"]}],
    }
    errors = "\n".join(validate_traceability(data, "traceability.yaml", spec))
    assert "unknown intent `missing`" in errors


def test_traceability_reports_missing_intent_yaml_for_intent_links(tmp_path: Path) -> None:
    spec = make_spec(tmp_path)
    (spec / "intent.yaml").unlink()
    data = {
        "artifact": "traceability",
        "spec": "demo",
        "intent_links": [{"intent_id": "C1", "requirement_ids": ["R1"]}],
        "requirement_links": [{"requirement_id": "R1", "design_ids": ["D1"]}],
        "design_links": [{"design_id": "D1", "task_ids": ["T1"]}],
        "task_links": [{"task_id": "T1", "files": [{"path": "README.md", "relevance": "supporting"}], "evidence_ids": ["E1"]}],
    }
    errors = "\n".join(validate_traceability(data, "traceability.yaml", spec))
    assert "intent link validation skipped" in errors
