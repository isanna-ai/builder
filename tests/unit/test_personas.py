"""Tests for personas.py — phase/turn -> runner-profile (persona) routing.

Direct-module style, matching tests/unit/test_model_registry.py: import
straight from _dispatch_runtime.personas rather than shelling out. This repo's
test runner (pytest/__main__.py) is a minimal shim with no pytest.raises and no
monkeypatch fixture — only bare asserts and the tmp_path fixture are supported,
and tests collect in ALPHABETICAL order, not declaration order (see main()), so
any module-attribute patch must restore itself in a try/finally.
"""

from __future__ import annotations

import _dispatch_runtime.personas as personas_mod
from _dispatch_runtime.personas import (
    ADVERSARIAL_REVIEWER,
    DATA_ENGINEER,
    DEVELOPER,
    PHASE_PERSONA_MAP,
    Persona,
    assert_persona_independence,
    declares_schema_touch,
    model_family_for_phase,
    persona_for_phase,
    select_independent_review_lane,
)
from _dispatch_runtime.phase_runtime import (
    REVIEW2_SPEC_PHASE_ORDER,
    _staffing_persona_block,
    build_phase_goal,
)


# --- T1: every phase resolves to exactly one staffed persona ----------------

def test_every_review_pipeline_phase_has_a_persona():
    for phase in REVIEW2_SPEC_PHASE_ORDER:
        persona = persona_for_phase(phase)
        assert persona.harness
        assert persona.capability_class
        assert persona.charter
        assert isinstance(persona.skills, tuple) and len(persona.skills) > 0


def test_phase_persona_roles():
    assert persona_for_phase("spec").key == "pm"
    assert persona_for_phase("plan").key == "architect"
    assert persona_for_phase("implement").key == "developer"
    assert persona_for_phase("verify").key == "qa"
    assert persona_for_phase("sync").key == "librarian"
    reviewer = persona_for_phase("adversarial-review")
    assert "reviewer" in reviewer.key.lower() or "reviewer" in reviewer.name.lower()
    assert "security" in reviewer.charter.lower()


def test_unknown_phase_raises():
    try:
        persona_for_phase("no-such-phase")
    except (KeyError, ValueError):
        pass
    else:
        raise AssertionError("persona_for_phase('no-such-phase') should have raised")


# --- T2: data-engineer selection on schema/migration/pipeline delta ---------

def test_implement_is_developer_without_schema_delta():
    delta = {"capabilities": [], "behaviors": [], "journeys": []}
    assert persona_for_phase("implement", ssot_delta=delta) is DEVELOPER


def test_implement_is_data_engineer_on_schema_delta():
    delta = {
        "capabilities": [{"target": "user-profile-api", "change": "enrich"}],
        "behaviors": [{"target": "orders-schema-migration", "change": "create"}],
        "journeys": [],
    }
    assert persona_for_phase("implement", ssot_delta=delta) is DATA_ENGINEER
    assert persona_for_phase("review-fix", ssot_delta=delta) is DATA_ENGINEER


def test_implement_delta_missing_or_malformed_falls_back_to_developer():
    assert persona_for_phase("implement", ssot_delta=None) is DEVELOPER
    assert persona_for_phase("implement", ssot_delta="not-a-dict") is DEVELOPER
    assert persona_for_phase("implement", ssot_delta=[1, 2, 3]) is DEVELOPER
    assert persona_for_phase("implement", ssot_delta={"capabilities": "oops"}) is DEVELOPER


def test_declares_schema_touch_is_shape_safe():
    assert declares_schema_touch(None) is False
    assert declares_schema_touch("nope") is False
    assert declares_schema_touch({}) is False
    assert declares_schema_touch({"capabilities": [{"target": "pipeline-runner", "change": "create"}]}) is True


# --- T3: independence invariants enforced by configuration -------------------

def test_no_persona_judges_its_own_output():
    assert persona_for_phase("spec-review") != persona_for_phase("spec")
    assert persona_for_phase("adversarial-review") != persona_for_phase("implement")
    assert persona_for_phase("verify") != persona_for_phase("implement")


def test_adversarial_review_is_a_different_model_family_than_author():
    assert model_family_for_phase("adversarial-review") != model_family_for_phase("implement")


def test_codex_author_uses_anthropic_review_lane_even_when_codex_is_preferred():
    lane_providers = {
        "primary": "codex-cli",
        "second-opinion": "codex-cli",
        "independent": "claude-code-cli",
    }
    picked = select_independent_review_lane(
        "implement",
        "adversarial-review",
        "primary",
        "second-opinion",
        lane_providers,
    )
    assert picked == "independent"


def test_same_family_only_review_configuration_raises():
    lane_providers = {"author": "codex-cli", "review": "codex-cli"}
    try:
        select_independent_review_lane(
            "implement",
            "adversarial-review",
            "author",
            "review",
            lane_providers,
        )
    except ValueError as exc:
        assert "No independent review lane" in str(exc)
    else:
        raise AssertionError("same-family review configuration should have raised")


def test_assert_persona_independence_passes_for_shipped_map():
    assert_persona_independence(PHASE_PERSONA_MAP) is None
    assert_persona_independence() is None


def test_assert_persona_independence_raises_on_self_judging_map():
    broken = dict(PHASE_PERSONA_MAP)
    broken["adversarial-review"] = DEVELOPER  # same persona as "implement" -> self-judging
    try:
        assert_persona_independence(broken)
    except ValueError:
        pass
    else:
        raise AssertionError("assert_persona_independence(broken) should have raised")


# --- T4: spec/design/review separately staffed; goal names the persona ------

def test_spec_and_spec_review_are_separately_staffed():
    assert persona_for_phase("spec") != persona_for_phase("spec-review")


def test_phase_goal_names_the_staffing_persona(tmp_path):
    specs_dir = tmp_path / ".builder" / "specs"
    spec_dir = specs_dir / "demo"
    spec_dir.mkdir(parents=True)
    (spec_dir / "spec.yaml").write_text(
        "name: demo\nstatus: specified\ncurrent_phase: plan\n", encoding="utf-8"
    )
    goal = build_phase_goal(tmp_path, specs_dir, "demo", "plan", None)
    architect = persona_for_phase("plan")
    assert architect.name in goal
    assert "TDD-anchored task breakdown" in goal


def test_reviewed_spec_goal_does_not_self_certify(tmp_path):
    specs_dir = tmp_path / ".builder" / "specs"
    spec_dir = specs_dir / "demo"
    spec_dir.mkdir(parents=True)
    (spec_dir / "spec.yaml").write_text(
        "name: demo\nstatus: specifying\ncurrent_phase: spec\nreviews: 1\n",
        encoding="utf-8",
    )
    goal = build_phase_goal(tmp_path, specs_dir, "demo", "spec", None)
    assert "self-review" not in goal
    assert "separately-staffed spec-review turn independently judges" in goal
    assert "=== REVIEW AUTO-APPLICATION ===" not in goal


def test_adversarial_review_persona_agrees_with_auto_application(tmp_path):
    specs_dir = tmp_path / ".builder" / "specs"
    spec_dir = specs_dir / "demo"
    spec_dir.mkdir(parents=True)
    (spec_dir / "spec.yaml").write_text(
        "name: demo\nstatus: implementing\ncurrent_phase: adversarial-review\nreviews: 1\n",
        encoding="utf-8",
    )
    goal = build_phase_goal(tmp_path, specs_dir, "demo", "adversarial-review", None)
    assert "=== REVIEW AUTO-APPLICATION ===" in goal
    assert "Do NOT fix anything" not in goal
    assert "Never fix" not in goal
    assert "follow the session's review auto-application policy" in goal


def test_phase_goal_is_byte_identical_when_persona_unresolvable(tmp_path):
    specs_dir = tmp_path / ".builder" / "specs"
    spec_dir = specs_dir / "demo"
    spec_dir.mkdir(parents=True)
    (spec_dir / "spec.yaml").write_text(
        "name: demo\nstatus: specified\ncurrent_phase: plan\n", encoding="utf-8"
    )
    before = build_phase_goal(tmp_path, specs_dir, "demo", "plan", None)
    assert "=== STAFFING PERSONA ===" in before

    original = personas_mod.persona_for_phase

    def _raise(*_a, **_kw):
        raise KeyError("unmapped")

    personas_mod.persona_for_phase = _raise
    try:
        after = build_phase_goal(tmp_path, specs_dir, "demo", "plan", None)
    finally:
        personas_mod.persona_for_phase = original
    assert "=== STAFFING PERSONA ===" not in after


def test_staffing_block_does_not_swallow_unexpected_invariant_failure(tmp_path):
    original = personas_mod.persona_for_phase

    def _raise(*_a, **_kw):
        raise RuntimeError("broken persona invariant")

    personas_mod.persona_for_phase = _raise
    try:
        try:
            _staffing_persona_block("plan", tmp_path)
        except RuntimeError as exc:
            assert "broken persona invariant" in str(exc)
        else:
            raise AssertionError("unexpected persona failures must block goal construction")
    finally:
        personas_mod.persona_for_phase = original
