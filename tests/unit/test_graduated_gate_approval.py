"""T2: graduated (lane A/B/C) gate approval wired into the dispatcher plan gate.

Exercises `DispatchScheduler._advance_after_success` directly (the same seam
`test_dispatch_scheduler.py` uses) against a spec whose plan phase just
SUCCEEDED, with `pipeline.plan_gate` armed for the spec, so the decided lane
governs what happens next instead of the old blanket human-approve.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from _yaml import yaml  # type: ignore

from _dispatch_runtime.config import DispatchConfig, LaneConfig
from _dispatch_runtime.queue_store import QueueStore
from _dispatch_runtime.scheduler import DispatchScheduler


def dispatch_config(tmp_path) -> DispatchConfig:
    lanes = {"claude": LaneConfig(name="claude", provider="claude-code-cli")}
    return DispatchConfig(
        queue_store_path=tmp_path,
        lanes=lanes,
        routing_policy={"default": "ordered"},
        cooldown_policy={"default_seconds": 60},
        retry_policy={"max_attempts": 3, "initial_seconds": 5, "max_seconds": 30, "jitter_seconds": 0},
        pipeline={"plan_gate": False, "default_lane": "claude"},
    )


def _make_spec(root: Path, spec_id: str, *, plan_gate: bool, proposed_lane=None, risk_signals=None) -> Path:
    spec_dir = root / ".builder" / "specs" / spec_id
    spec_dir.mkdir(parents=True, exist_ok=True)
    spec = {"name": spec_id, "status": "planned", "current_phase": "implement"}
    if plan_gate:
        spec["plan_gate"] = True
    (spec_dir / "spec.yaml").write_text(yaml.safe_dump(spec, sort_keys=False), encoding="utf-8")
    (spec_dir / "tasks.yaml").write_text("artifact: tasks\n", encoding="utf-8")
    handoff = {"completed_phase": "plan", "next_phase": "implement", "ready": True}
    if proposed_lane is not None:
        handoff["proposed_gate_lane"] = proposed_lane
    if risk_signals is not None:
        handoff["gate_risk_signals"] = risk_signals
    (spec_dir / "handoff.yaml").write_text(yaml.safe_dump(handoff, sort_keys=False), encoding="utf-8")
    return spec_dir


def _make_scheduler(tmp_path):
    store = QueueStore(tmp_path)
    scheduler = DispatchScheduler(
        store, dispatch_config(tmp_path), executor=None, owner_id="driver-a", project_dir=tmp_path,
    )
    return store, scheduler


def _enqueue_plan_item(store: QueueStore, spec_id: str):
    return store.enqueue(task_ref={
        "kind": "builder-phase-batch",
        "runner_task_ref": f".builder/specs/{spec_id}/runs/phase-plan.yaml",
        "spec_id": spec_id,
    })


def _gates_dir(tmp_path) -> Path:
    return tmp_path / "queue" / "gates"


# --- lane A: auto-pass when artifacts exist + validators green (SUCCESS => green) -


def test_lane_a_flows_through_with_no_human_action_lane_a_or_lane_c(tmp_path):
    spec_id = "docs-only"
    _make_spec(tmp_path, spec_id, plan_gate=True, proposed_lane="A", risk_signals=["docs"])
    store, scheduler = _make_scheduler(tmp_path)
    item = _enqueue_plan_item(store, spec_id)

    scheduler._advance_after_success(item, "plan")

    gates = _gates_dir(tmp_path)
    assert not (gates / f"{spec_id}.json").exists()  # no gate left pending
    snapshot = store.reconstruct()
    refs = [str((it.task_ref or {}).get("runner_task_ref")) for it in snapshot.items.values()]
    assert any("phase-implement.yaml" in r for r in refs)  # enqueued straight through


def test_lane_a_does_not_autopass_when_required_artifacts_missing_lane_a_or_lane_c(tmp_path):
    spec_id = "docs-missing-artifact"
    spec_dir = _make_spec(tmp_path, spec_id, plan_gate=True, proposed_lane="A", risk_signals=["docs"])
    (spec_dir / "tasks.yaml").unlink()  # required plan artifact missing
    store, scheduler = _make_scheduler(tmp_path)
    item = _enqueue_plan_item(store, spec_id)

    scheduler._advance_after_success(item, "plan")

    gates = _gates_dir(tmp_path)
    marker = gates / f"{spec_id}.json"
    assert marker.exists()  # held, not flowed through
    assert json.loads(marker.read_text(encoding="utf-8"))["gate_lane"] == "A"
    snapshot = store.reconstruct()
    refs = [str((it.task_ref or {}).get("runner_task_ref")) for it in snapshot.items.values()]
    assert not any("phase-implement.yaml" in r for r in refs)


# --- lane C: policy-listed high-risk surface stays closed until explicit approval -


def test_lane_c_surface_holds_closed_regardless_of_elapsed_time_lane_a_or_lane_c(tmp_path):
    spec_id = "auth-change"
    _make_spec(tmp_path, spec_id, plan_gate=True, proposed_lane="A", risk_signals=["auth_surface"])
    store, scheduler = _make_scheduler(tmp_path)
    item = _enqueue_plan_item(store, spec_id)

    scheduler._advance_after_success(item, "plan")

    gates = _gates_dir(tmp_path)
    marker = gates / f"{spec_id}.json"
    assert marker.exists()
    payload = json.loads(marker.read_text(encoding="utf-8"))
    assert payload["gate_lane"] == "C"

    # Simulate a long time elapsing -- lane C must never auto-open on silence.
    scheduler._process_veto_windows()
    assert marker.exists()
    assert not (gates / f"{spec_id}.approved").exists()


def test_lane_c_opens_only_after_explicit_recorded_approval_lane_a_or_lane_c(tmp_path):
    spec_id = "migration-change"
    _make_spec(tmp_path, spec_id, plan_gate=True, proposed_lane="B", risk_signals=["migration"])
    store, scheduler = _make_scheduler(tmp_path)
    item = _enqueue_plan_item(store, spec_id)
    scheduler._advance_after_success(item, "plan")

    gates = _gates_dir(tmp_path)
    marker = gates / f"{spec_id}.json"
    payload = json.loads(marker.read_text(encoding="utf-8"))
    assert payload["gate_lane"] == "C"

    # Explicit recorded human approval -- mirrors the `approve` CLI's own write.
    scheduler._open_gate(spec_id, payload)
    assert (gates / f"{spec_id}.approved").exists()
    assert not marker.exists()
    snapshot = store.reconstruct()
    refs = [str((it.task_ref or {}).get("runner_task_ref")) for it in snapshot.items.values()]
    assert any("phase-implement.yaml" in r for r in refs)


# --- lane B: the default; opens after the quiet period with no hold ----------


def test_lane_b_is_the_default_when_not_listed_a_or_c():
    pass  # covered at the policy-engine level (test_gate_policy.py); see below for gate integration


def test_lane_b_gate_holds_and_notifies_when_opened(tmp_path):
    spec_id = "generic-change"
    _make_spec(tmp_path, spec_id, plan_gate=True, proposed_lane="B", risk_signals=["backend_logic"])
    store, scheduler = _make_scheduler(tmp_path)
    item = _enqueue_plan_item(store, spec_id)

    scheduler._advance_after_success(item, "plan")

    gates = _gates_dir(tmp_path)
    marker = gates / f"{spec_id}.json"
    assert marker.exists()
    payload = json.loads(marker.read_text(encoding="utf-8"))
    assert payload["gate_lane"] == "B"
    assert payload.get("opened_at")

    notif_dir = tmp_path / "queue" / "notifications"
    kinds = [p.name for p in notif_dir.glob("*veto_window_opened*")]
    assert kinds, "expected a veto_window_opened notification"


def test_lane_b_gate_opens_after_quiet_period_elapses_with_no_hold(tmp_path):
    spec_id = "generic-change-2"
    _make_spec(tmp_path, spec_id, plan_gate=True, proposed_lane="B", risk_signals=["backend_logic"])
    store, scheduler = _make_scheduler(tmp_path)
    item = _enqueue_plan_item(store, spec_id)
    scheduler._advance_after_success(item, "plan")

    gates = _gates_dir(tmp_path)
    marker = gates / f"{spec_id}.json"
    payload = json.loads(marker.read_text(encoding="utf-8"))
    # Backdate opened_at well past the default quiet period.
    payload["opened_at"] = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat().replace("+00:00", "Z")
    marker.write_text(json.dumps(payload), encoding="utf-8")

    opened = scheduler._process_veto_windows()

    assert opened == [spec_id]
    assert not marker.exists()
    assert (gates / f"{spec_id}.approved").exists()
    snapshot = store.reconstruct()
    refs = [str((it.task_ref or {}).get("runner_task_ref")) for it in snapshot.items.values()]
    assert any("phase-implement.yaml" in r for r in refs)


def test_lane_b_gate_does_not_open_before_quiet_period_elapses(tmp_path):
    spec_id = "generic-change-3"
    _make_spec(tmp_path, spec_id, plan_gate=True, proposed_lane="B", risk_signals=["backend_logic"])
    store, scheduler = _make_scheduler(tmp_path)
    item = _enqueue_plan_item(store, spec_id)
    scheduler._advance_after_success(item, "plan")

    gates = _gates_dir(tmp_path)
    marker = gates / f"{spec_id}.json"

    opened = scheduler._process_veto_windows()  # opened_at is "now" -- quiet period not elapsed

    assert opened == []
    assert marker.exists()


def test_lane_b_uses_decision_time_quiet_period_after_policy_changes(tmp_path):
    spec_id = "policy-change"
    _make_spec(tmp_path, spec_id, plan_gate=True, proposed_lane="B", risk_signals=["backend_logic"])
    policy_path = tmp_path / ".builder" / "gate-lane-policy.yaml"
    policy_path.write_text(
        "version: 'first'\nlane_a_surfaces: [docs]\nlane_c_surfaces: [migration]\n"
        "veto_window: {quiet_period_seconds: 3600}\n",
        encoding="utf-8",
    )
    store, scheduler = _make_scheduler(tmp_path)
    scheduler._advance_after_success(_enqueue_plan_item(store, spec_id), "plan")

    marker = _gates_dir(tmp_path) / f"{spec_id}.json"
    payload = json.loads(marker.read_text(encoding="utf-8"))
    payload["opened_at"] = (
        datetime.now(timezone.utc) - timedelta(seconds=60)
    ).isoformat().replace("+00:00", "Z")
    marker.write_text(json.dumps(payload), encoding="utf-8")

    # A new policy may govern future gates, but must not retroactively shorten
    # this versioned decision's already-open quiet period.
    policy_path.write_text(
        "version: 'second'\nlane_a_surfaces: [docs]\nlane_c_surfaces: [migration]\n"
        "veto_window: {quiet_period_seconds: 0}\n",
        encoding="utf-8",
    )
    assert scheduler._process_veto_windows() == []
    assert marker.exists()


# --- AC-R3-3: a hold recorded during the veto window prevents the gate opening -


def test_hold_action_prevents_the_gate_from_opening(tmp_path):
    spec_id = "generic-change-4"
    _make_spec(tmp_path, spec_id, plan_gate=True, proposed_lane="B", risk_signals=["backend_logic"])
    store, scheduler = _make_scheduler(tmp_path)
    item = _enqueue_plan_item(store, spec_id)
    scheduler._advance_after_success(item, "plan")

    gates = _gates_dir(tmp_path)
    marker = gates / f"{spec_id}.json"
    payload = json.loads(marker.read_text(encoding="utf-8"))
    payload["opened_at"] = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat().replace("+00:00", "Z")
    marker.write_text(json.dumps(payload), encoding="utf-8")

    held = scheduler.hold_veto_window(spec_id, reason="not yet")
    assert held is True
    assert (gates / f"{spec_id}.hold").exists()

    opened = scheduler._process_veto_windows()

    assert opened == []
    assert marker.exists()
    assert not (gates / f"{spec_id}.approved").exists()


def test_hold_veto_window_is_a_noop_when_no_gate_pending(tmp_path):
    store, scheduler = _make_scheduler(tmp_path)
    assert scheduler.hold_veto_window("nonexistent-spec") is False


def test_operator_cli_exposes_the_lane_b_hold_action(tmp_path):
    from _dispatch_runtime.cli import run

    spec_id = "cli-hold"
    _make_spec(tmp_path, spec_id, plan_gate=True, proposed_lane="B", risk_signals=["backend_logic"])
    store, scheduler = _make_scheduler(tmp_path)
    scheduler._advance_after_success(_enqueue_plan_item(store, spec_id), "plan")
    config_path = tmp_path / ".builder" / "dispatch.yaml"
    config_path.write_text(
        "queue_store: {path: dispatch-queue}\n"
        "lanes:\n  - {name: claude, provider: claude-code-cli}\n"
        "routing_policy: {default: ordered}\n"
        "pipeline: {default_lane: claude}\n"
        "retry_policy: {max_attempts: 3}\n"
        "cooldown_policy: {default_seconds: 60}\n",
        encoding="utf-8",
    )

    assert run(
        ["--config", str(config_path), "hold", spec_id, "--reason", "owner veto"],
        _store_override=store,
    ) == 0
    assert (_gates_dir(tmp_path) / f"{spec_id}.hold").exists()
