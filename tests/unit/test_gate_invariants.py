"""T7: preserved trust invariants (D7) — regression coverage locking in that
`fulfilled` and gate evidence stay computed from host-verified runs, and that
no agent-supplied value can ever become the final gate decision, even as the
gate-policy engine and the driver are added to the pipeline.
"""

from __future__ import annotations

import dataclasses
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from _dispatch_runtime import gate_policy


def _module_source(relpath: str) -> str:
    return (SCRIPTS / relpath).read_text(encoding="utf-8")


# --- AC-R8-1: fulfilled / gate evidence stay computed from host-verified runs -


def test_gate_policy_module_never_references_fulfilled_host_verified():
    source = _module_source("_dispatch_runtime/gate_policy.py")
    assert "fulfilled" not in source.lower()


def test_builder_driver_module_never_references_fulfilled_host_verified():
    source = _module_source("builder-driver.py")
    assert "fulfilled" not in source.lower()


def test_gate_policy_module_never_writes_gate_evidence_bundles_host_verified():
    """gate_evidence.py owns the host-verified evidence bundle; gate_policy.py
    must never import it, let alone write through it."""
    source = _module_source("_dispatch_runtime/gate_policy.py")
    assert "gate_evidence" not in source


def test_builder_driver_module_never_writes_gate_evidence_bundles_host_verified():
    source = _module_source("builder-driver.py")
    assert "gate_evidence" not in source


def test_decide_is_pure_and_never_touches_disk_or_env_host_verified(tmp_path):
    policy = gate_policy.load_policy(None)
    before = sorted(tmp_path.iterdir())
    decision = gate_policy.decide("B", ["auth_surface"], policy)
    after = sorted(tmp_path.iterdir())
    assert before == after
    assert decision.lane == "C"  # sanity: the call actually did something


def test_lane_a_flow_through_ready_only_READS_artifacts_never_writes_host_verified(tmp_path):
    """The lane-A readiness check may only observe artifacts already on disk
    (written by validate_phase_completion / the host verify run) -- it must
    never itself create/mutate the artifacts it inspects."""
    before = sorted(tmp_path.iterdir())
    gate_policy.lane_a_flow_through_ready(tmp_path, [["tasks.yaml"]], validators_green=True)
    after = sorted(tmp_path.iterdir())
    assert before == after


# --- AC-R8-2: no agent-supplied value can become the final gate decision -----


def test_gate_lane_decision_is_immutable_once_produced():
    policy = gate_policy.load_policy(None)
    decision = gate_policy.decide("B", ["docs"], policy)
    try:
        decision.lane = "C"  # type: ignore[misc]
        assert False, "GateLaneDecision must be frozen -- no post-hoc mutation of the final lane"
    except dataclasses.FrozenInstanceError:
        pass


def test_decide_is_the_only_producer_of_gate_lane_decision_in_the_module():
    """Regression lock: `GateLaneDecision(` may only be constructed inside
    `decide()` — if a future change adds a second code path that fabricates a
    decision (e.g. from raw agent input), this test catches it."""
    source = _module_source("_dispatch_runtime/gate_policy.py")
    construction_sites = source.count("GateLaneDecision(\n") + source.count("GateLaneDecision(lane=")
    assert construction_sites == 1, "GateLaneDecision must be constructed in exactly one place: decide()"


def test_forbidden_gate_keys_cover_every_plausible_self_classification_spelling():
    required = {"gate_lane", "final_lane", "gate_decision", "lane_decision", "resolved_lane"}
    assert required <= gate_policy.FORBIDDEN_AGENT_GATE_KEYS


def test_reject_agent_authored_decision_blocks_every_forbidden_key():
    for key in gate_policy.FORBIDDEN_AGENT_GATE_KEYS:
        try:
            gate_policy.reject_agent_authored_decision({key: "A", "proposed_gate_lane": "B"})
            assert False, f"expected a rejection for forbidden key {key!r}"
        except gate_policy.SelfClassificationError:
            pass


def test_extract_proposed_gate_lane_never_reads_a_final_lane_field():
    """Even without the hard guard, the plain extractor structurally ignores
    any final-lane-shaped key -- defense in depth alongside the raise-based
    guard in `reject_agent_authored_decision`."""
    poisoned = {
        "proposed_gate_lane": "B",
        "gate_risk_signals": ["docs"],
        "gate_lane": "A", "final_lane": "A", "gate_decision": "A",
    }
    proposed, signals = gate_policy.extract_proposed_gate_lane(poisoned)
    assert proposed == "B"  # only the advisory key was read
    assert signals == ["docs"]


def test_no_agent_authored_value_can_reach_decide_as_the_policy_argument():
    """`decide()`'s THIRD argument is the versioned policy document, never
    agent output -- an agent cannot smuggle a self-favoring lane in by
    posing as the policy. Passing agent-shaped data as `policy` simply finds
    no lane_c/lane_a surfaces there and safely falls through to the B
    default, it never crashes or gets treated as authoritative."""
    agent_shaped_payload = {"gate_lane": "A", "final_lane": "A"}
    decision = gate_policy.decide("A", ["migration"], agent_shaped_payload)
    # No lane_c_surfaces in the "policy" -> migration isn't recognized -> B default,
    # then raised to the proposed floor "A" -> still never silently grants C-bypass
    # or reads gate_lane/final_lane off the payload.
    assert decision.lane in ("A", "B")
    assert not hasattr(decision, "gate_lane")
