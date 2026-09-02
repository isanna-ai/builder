"""T1: the versioned, deterministic three-lane gate-policy engine.

decide() maps (proposed_lane, risk_signals) against a versioned policy
document to exactly one lane (A, B, C) — deterministic, side-effect free,
and the sole decider (the proposed lane is advisory input only).
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from _dispatch_runtime import gate_policy


def _policy(**overrides):
    base = gate_policy.load_policy(None)
    base.update(overrides)
    return base


# --- AC-R1-1 / AC-R1-2: exactly one lane, deterministic, resolved version recorded


def test_decide_returns_exactly_one_of_a_b_c_decision_or_version():
    policy = _policy()
    decision = gate_policy.decide("B", ["docs"], policy)
    assert decision.lane in ("A", "B", "C")


def test_decide_is_deterministic_for_identical_inputs_decision_or_version():
    policy = _policy()
    first = gate_policy.decide("B", ["auth_surface"], policy)
    second = gate_policy.decide("B", ["auth_surface"], policy)
    assert first == second


def test_decide_records_the_resolved_policy_version_decision_or_version():
    policy = _policy(version="42")
    decision = gate_policy.decide("A", ["docs"], policy)
    assert decision.policy_version == "42"


def test_decide_version_recorded_even_when_lane_c_overrides_decision_or_version():
    policy = _policy(version="7")
    decision = gate_policy.decide("A", ["migration"], policy)
    assert decision.policy_version == "7"
    assert decision.lane == "C"


# --- AC-R1-3: proposed lane is advisory input only; engine may override upward


def test_under_classified_proposal_is_overridden_upward_to_lane_c():
    policy = _policy()
    decision = gate_policy.decide("A", ["migration"], policy)
    assert decision.lane == "C"
    assert decision.proposed_lane == "A"  # recorded, but not honored as final


def test_engine_never_lowers_below_the_proposed_floor():
    # Nothing in the declared signals justifies C, but the architect proposed
    # C out of caution -- the engine treats that as a floor, not a ceiling.
    policy = _policy()
    decision = gate_policy.decide("C", [], policy)
    assert decision.lane == "C"
    assert decision.data_lane == "B"


def test_proposed_lane_is_recorded_but_data_is_what_decides_the_default():
    policy = _policy()
    # proposed B, no risk signals declared -> stays B (the default), not A.
    decision = gate_policy.decide("B", [], policy)
    assert decision.lane == "B"


def test_unknown_or_missing_proposed_lane_defaults_to_the_safe_b_floor():
    policy = _policy()
    decision = gate_policy.decide(None, [], policy)
    assert decision.lane == "B"
    decision2 = gate_policy.decide("not-a-lane", [], policy)
    assert decision2.lane == "B"


# --- AC-R4-2: the lane-C list is versioned policy DATA, editable without code changes


def test_lane_c_membership_is_driven_entirely_by_policy_data_not_code(tmp_path):
    custom_policy_path = tmp_path / "gate-lane-policy.yaml"
    custom_policy_path.write_text(
        "version: '2'\n"
        "lane_a_surfaces: [docs]\n"
        "lane_c_surfaces: [totally_custom_surface]\n",
        encoding="utf-8",
    )
    policy = gate_policy.load_policy(custom_policy_path)
    assert policy["version"] == "2"

    # A surface that is lane-C by DEFAULT policy data is no longer special
    # once a project's policy document omits it...
    decision_default_surface = gate_policy.decide("A", ["migration"], policy)
    assert decision_default_surface.lane != "C"

    # ...while the custom-declared surface now IS lane-C, purely from data.
    decision_custom_surface = gate_policy.decide("A", ["totally_custom_surface"], policy)
    assert decision_custom_surface.lane == "C"


def test_default_policy_lists_the_required_lane_c_surfaces():
    policy = gate_policy.load_policy(None)
    required = {
        "migration", "critical_ssot_delta", "auth_surface",
        "payment_surface", "deploy_config", "public_contract",
    }
    assert required <= set(policy["lane_c_surfaces"])


def test_default_policy_is_loaded_from_a_shipped_versioned_document():
    assert gate_policy.SHIPPED_POLICY_PATH.is_file()
    assert gate_policy.load_policy(None) == gate_policy.load_policy(gate_policy.SHIPPED_POLICY_PATH)


def test_load_policy_missing_file_falls_back_to_default_without_raising(tmp_path):
    policy = gate_policy.load_policy(tmp_path / "does-not-exist.yaml")
    assert policy["version"] == gate_policy.DEFAULT_POLICY["version"]


# --- lane-A data-driven eligibility helper (used by the gate integration in T2)


def test_lane_a_flow_through_ready_requires_artifacts_and_green_validators(tmp_path):
    spec_dir = tmp_path
    assert gate_policy.lane_a_flow_through_ready(spec_dir, [["tasks.yaml"]], validators_green=True) is False
    (spec_dir / "tasks.yaml").write_text("x: 1\n", encoding="utf-8")
    assert gate_policy.lane_a_flow_through_ready(spec_dir, [["tasks.yaml"]], validators_green=True) is True
    assert gate_policy.lane_a_flow_through_ready(spec_dir, [["tasks.yaml"]], validators_green=False) is False


# --- decide() is pure / side-effect free


def test_decide_never_touches_disk(tmp_path):
    policy = _policy()
    before = sorted(tmp_path.iterdir())
    gate_policy.decide("B", ["auth_surface"], policy)
    after = sorted(tmp_path.iterdir())
    assert before == after
