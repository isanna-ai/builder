"""T3: the architect's proposed gate lane is plumbed into the policy engine as
ADVISORY input only, and a guard rejects any agent attempt to write a gate
decision or select its own final lane (AC-R1-3, AC-R8-2).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from _yaml import yaml  # type: ignore

from _dispatch_runtime import gate_policy
from _dispatch_runtime.phase_runtime import resolve_gate_lane_proposal


def _write_handoff(specs_dir: Path, spec_id: str, data: dict) -> None:
    spec_dir = specs_dir / spec_id
    spec_dir.mkdir(parents=True, exist_ok=True)
    (spec_dir / "handoff.yaml").write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


# --- AC-R1-3: the proposed lane + risk signals reach the engine as advisory input


def test_resolve_reads_proposed_lane_and_risk_signals_proposal_advisory(tmp_path):
    _write_handoff(tmp_path, "demo", {
        "completed_phase": "plan", "next_phase": "implement", "ready": True,
        "proposed_gate_lane": "A", "gate_risk_signals": ["docs", "tests"],
    })
    proposed, signals = resolve_gate_lane_proposal(tmp_path, "demo")
    assert proposed == "A"
    assert signals == ["docs", "tests"]


def test_advisory_input_flows_into_decide_and_can_be_overridden_proposal_advisory(tmp_path):
    _write_handoff(tmp_path, "demo", {
        "completed_phase": "plan", "next_phase": "implement", "ready": True,
        "proposed_gate_lane": "A", "gate_risk_signals": ["migration"],
    })
    proposed, signals = resolve_gate_lane_proposal(tmp_path, "demo")
    policy = gate_policy.load_policy(None)
    decision = gate_policy.decide(proposed, signals, policy)
    # The engine is the SOLE decider: an under-classified proposal (A) touching a
    # policy-listed lane-C surface is overridden upward, never taken at face value.
    assert decision.lane == "C"
    assert decision.proposed_lane == "A"


def test_missing_or_malformed_handoff_yields_no_proposal_proposal_advisory(tmp_path):
    proposed, signals = resolve_gate_lane_proposal(tmp_path, "no-such-spec")
    assert proposed is None
    assert signals == []


def test_unrecognized_proposed_lane_value_is_ignored_not_coerced_proposal_advisory(tmp_path):
    _write_handoff(tmp_path, "demo", {
        "completed_phase": "plan", "next_phase": "implement", "ready": True,
        "proposed_gate_lane": "Z-not-a-real-lane", "gate_risk_signals": [],
    })
    proposed, signals = resolve_gate_lane_proposal(tmp_path, "demo")
    assert proposed is None


# --- AC-R8-2: no agent can write a gate decision or select its own final lane


def test_agent_written_gate_lane_key_is_rejected_outright():
    with_forbidden = {"proposed_gate_lane": "B", "gate_lane": "A"}
    try:
        gate_policy.reject_agent_authored_decision(with_forbidden)
        assert False, "expected SelfClassificationError"
    except gate_policy.SelfClassificationError:
        pass


def test_agent_written_final_lane_or_gate_decision_keys_are_all_rejected():
    for key in sorted(gate_policy.FORBIDDEN_AGENT_GATE_KEYS):
        try:
            gate_policy.reject_agent_authored_decision({key: "A"})
            assert False, f"expected SelfClassificationError for key {key!r}"
        except gate_policy.SelfClassificationError:
            pass


def test_advisory_only_keys_never_trip_the_guard():
    gate_policy.reject_agent_authored_decision({"proposed_gate_lane": "C", "gate_risk_signals": ["auth_surface"]})
    gate_policy.reject_agent_authored_decision({})
    gate_policy.reject_agent_authored_decision(None)


def test_resolve_raises_when_handoff_carries_a_forbidden_gate_key(tmp_path):
    _write_handoff(tmp_path, "demo", {
        "completed_phase": "plan", "next_phase": "implement", "ready": True,
        "proposed_gate_lane": "A", "gate_risk_signals": ["docs"],
        "gate_lane": "A",  # self-classification attempt
    })
    try:
        resolve_gate_lane_proposal(tmp_path, "demo")
        assert False, "expected SelfClassificationError"
    except gate_policy.SelfClassificationError:
        pass


def test_scheduler_fails_closed_to_lane_b_when_handoff_self_classifies(tmp_path):
    """End-to-end: even if the plan turn's handoff.yaml directly writes
    gate_lane: 'A' (an attempted flow-through bypass), the scheduler never
    honors it -- the poisoned proposal is discarded and the spec falls to the
    safe lane-B default rather than auto-passing on the agent's say-so."""
    from _dispatch_runtime.config import DispatchConfig, LaneConfig
    from _dispatch_runtime.queue_store import QueueStore
    from _dispatch_runtime.scheduler import DispatchScheduler

    spec_dir = tmp_path / ".builder" / "specs" / "demo"
    spec_dir.mkdir(parents=True)
    (spec_dir / "spec.yaml").write_text(
        yaml.safe_dump({"name": "demo", "status": "planned", "current_phase": "implement", "plan_gate": True}),
        encoding="utf-8",
    )
    (spec_dir / "tasks.yaml").write_text("artifact: tasks\n", encoding="utf-8")
    _write_handoff(tmp_path / ".builder" / "specs", "demo", {
        "completed_phase": "plan", "next_phase": "implement", "ready": True,
        "proposed_gate_lane": "A", "gate_risk_signals": ["docs"],
        "gate_lane": "A",
    })

    store = QueueStore(tmp_path)
    cfg = DispatchConfig(
        queue_store_path=tmp_path,
        lanes={"claude": LaneConfig(name="claude", provider="claude-code-cli")},
        routing_policy={"default": "ordered"},
        cooldown_policy={"default_seconds": 60},
        retry_policy={"max_attempts": 3, "initial_seconds": 5, "max_seconds": 30, "jitter_seconds": 0},
        pipeline={"plan_gate": False, "default_lane": "claude"},
    )
    scheduler = DispatchScheduler(store, cfg, executor=None, owner_id="driver-a", project_dir=tmp_path)
    item = store.enqueue(task_ref={
        "kind": "builder-phase-batch",
        "runner_task_ref": ".builder/specs/demo/runs/phase-plan.yaml",
        "spec_id": "demo",
    })

    scheduler._advance_after_success(item, "plan")

    marker = tmp_path / "queue" / "gates" / "demo.json"
    assert marker.exists()  # NOT auto-passed despite the injected gate_lane: "A"
    payload = json.loads(marker.read_text(encoding="utf-8"))
    assert payload["gate_lane"] == "B"
    assert payload["proposed_lane"] is None  # the poisoned proposal was discarded
