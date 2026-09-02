"""Regression tests for the A/B memory-gain harness's PLAN-GATE ISOLATION fix.

Background (the bug this guards against)
========================================
isanna's live ``dispatch.yaml`` runs with ``pipeline.plan_gate: false`` so specs
auto-advance ``spec -> plan -> implement -> verify`` unattended. The A/B harness
must measure the PLAN phase ONLY. The original harness ran the benchmark plan
phases on the SHARED live queue, so the moment a plan phase succeeded the scheduler
AUTO-ENQUEUED the implement phase (``_advance_after_success`` falls through to
``_enqueue_phase`` when plan_gate is false), and the next FIFO ``run --once``
dispatched that IMPLEMENT phase instead of the next plan — contaminating the arms
and burning the claude lane on implement work.

The fix: the harness writes a TEMP config derived from ``--config`` that overrides
exactly two keys — ``pipeline.plan_gate = true`` (STOP after plan) and
``queue_store.path = <isolated abs dir>`` (never touch the live queue) — and uses
that temp config for draft + enqueue + run.

These tests assert all three required properties:
  1. the temp config has plan_gate=true + an isolated queue distinct from source;
  2. the harness uses the temp config path in its draft/enqueue/run commands;
  3. (code-level, via the real dispatch runtime) plan_gate:true does NOT
     auto-enqueue the post-plan (implement) phase, whereas plan_gate:false does.

The local pytest runner (./pytest) supports only the ``tmp_path`` fixture, so each
test is a plain function; the hyphenated harness script is loaded via importlib.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from _yaml import yaml  # type: ignore


REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_runner():
    # scripts/ab-memory-gain.py is hyphenated => not importable by name.
    path = REPO_ROOT / "scripts" / "ab-memory-gain.py"
    spec = importlib.util.spec_from_file_location("ab_memory_gain", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["ab_memory_gain"] = module
    spec.loader.exec_module(module)
    return module


def _write_source_config(root: Path) -> Path:
    """A minimal SOURCE dispatch.yaml that mirrors the live one: plan_gate FALSE and
    the live (non-isolated) queue path. The harness must NOT mutate this file."""
    cfg = root / ".builder" / "dispatch.yaml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(
        yaml.safe_dump(
            {
                "queue_store": {"path": str(root / ".builder" / "dispatch-queue")},
                "lanes": [
                    {"name": "claude", "provider": "claude-code-cli"},
                    {"name": "codex", "provider": "codex-cli"},
                ],
                "routing_policy": {"default": "ordered"},
                "pipeline": {"plan_gate": False, "deliver": {"enabled": False}},
                "retry_policy": {"max_attempts": 2},
                "cooldown_policy": {"default_seconds": 600},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return cfg


# --- 1. temp config: plan_gate=true + isolated queue distinct from source ----


def test_temp_config_sets_plan_gate_true_and_isolated_queue(tmp_path):
    rt = _load_runner()
    root = tmp_path
    source = _write_source_config(root)
    run_id = "20260608-000000"

    cfg_path, queue_dir = rt.build_temp_config(source, root, run_id)

    # The temp config is a DIFFERENT file from the source (live config untouched).
    assert cfg_path != source
    assert cfg_path.exists()

    data = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    # plan_gate forced ON => the scheduler STOPS after the plan phase.
    assert data["pipeline"]["plan_gate"] is True
    # queue_store.path is the ISOLATED dir, distinct from the source's live queue.
    src_data = yaml.safe_load(source.read_text(encoding="utf-8"))
    assert data["queue_store"]["path"] != src_data["queue_store"]["path"]
    assert data["queue_store"]["path"] == str(queue_dir)
    assert "ab-queue-" in data["queue_store"]["path"]
    assert Path(data["queue_store"]["path"]).is_absolute()

    # Everything else carried through from the source (lanes preserved).
    assert {l["name"] for l in data["lanes"]} == {"claude", "codex"}

    # The live source config was NOT modified (plan_gate still false there).
    assert src_data["pipeline"]["plan_gate"] is False


def test_temp_config_parent_parent_resolves_to_project_root(tmp_path):
    rt = _load_runner()
    root = tmp_path
    source = _write_source_config(root)
    cfg_path, _ = rt.build_temp_config(source, root, "rid-1")
    # The dispatch CLI derives project_dir = config.parent.parent; it MUST be root
    # so specs under <root>/.builder/specs are found.
    assert cfg_path.resolve().parent.parent == root.resolve()


def test_isolated_queue_distinct_from_live_dispatch_queue(tmp_path):
    rt = _load_runner()
    root = tmp_path
    q = rt.isolated_queue_path(root, "rid-2")
    assert q != (root / ".builder" / "dispatch-queue")
    assert "ab-queue-rid-2" in str(q)


# --- 2. the harness uses the temp config path in its commands ----------------


def test_run_commands_use_the_temp_config_path():
    rt = _load_runner()
    temp_cfg = "/proj/.builder/ab-dispatch-RID.yaml"
    enq = rt.enqueue_plan_command(temp_cfg, "spec-x")
    run = rt.run_once_command(temp_cfg)
    # Both the enqueue and the run --once carry the TEMP config path (not the live).
    assert temp_cfg in enq
    assert "--config" in enq and enq[enq.index("--config") + 1] == temp_cfg
    assert temp_cfg in run
    assert "--config" in run and run[run.index("--config") + 1] == temp_cfg


def test_dry_run_plan_threads_temp_config_into_every_plan_command():
    rt = _load_runner()
    temp_cfg = "/proj/.builder/ab-dispatch-RID.yaml"
    plan = rt.build_dry_run_plan(
        config_path=temp_cfg,
        root="/proj",
        arm_specs={rt.ARM_OFF: ["off0", "off1"], rt.ARM_HIVEMIND: ["hv0", "hv1"]},
        arms=[rt.ARM_OFF, rt.ARM_HIVEMIND],
        base_env={"HIVEMIND_MCP_URL": "u", "HIVEMIND_API_KEY": "k"},
        live_hivemind={"HIVEMIND_MCP_URL": "u", "HIVEMIND_API_KEY": "k"},
        do_seed=False,
        do_report=False,
        telegram=False,
        temp_config=temp_cfg,
        isolated_queue="/proj/.builder/ab-queue-RID",
        plan_gate=True,
    )
    plan_steps = [s for s in plan["steps"] if s["kind"] == "plan"]
    assert plan_steps, "expected plan steps"
    for step in plan_steps:
        assert temp_cfg in step["enqueue_command"]
        assert temp_cfg in step["run_command"]
    # The isolation block records plan_gate true + the isolated queue.
    assert plan["isolation"]["plan_gate"] is True
    assert plan["isolation"]["temp_config"] == temp_cfg
    assert plan["isolation"]["isolated_queue"].endswith("ab-queue-RID")


def test_resolved_root_derives_from_config_parent_parent(tmp_path):
    rt = _load_runner()
    source = _write_source_config(tmp_path)
    args = rt.build_parser().parse_args(["--config", str(source), "--dry-run"])
    root = rt._resolve_root(args)
    assert root == tmp_path.resolve()


# --- 3. code-level proof: plan_gate:true does NOT auto-enqueue implement ------
#
# This drives the REAL dispatch runtime (DispatchScheduler._advance_after_success):
# the same code path the daemon takes after a plan phase succeeds. With
# plan_gate=True it writes a gate marker and returns WITHOUT enqueuing the next
# phase; with plan_gate=False it enqueues the implement phase (the contamination
# the harness fix prevents). We assert BOTH branches so the proof is unambiguous.


def _make_scheduler(tmp_path, *, plan_gate: bool):
    from _dispatch_runtime.config import DispatchConfig, LaneConfig
    from _dispatch_runtime.queue_store import QueueStore
    from _dispatch_runtime.scheduler import DispatchScheduler

    queue_root = tmp_path / ".builder" / "ab-queue-test"
    store = QueueStore(queue_root)
    config = DispatchConfig(
        queue_store_path=queue_root,
        lanes={"claude": LaneConfig(name="claude", provider="claude-code-cli")},
        routing_policy={"default": "ordered"},
        cooldown_policy={"default_seconds": 60},
        retry_policy={"max_attempts": 2, "initial_seconds": 5, "max_seconds": 30, "jitter_seconds": 0},
        pipeline={"plan_gate": plan_gate, "default_lane": "claude", "deliver": {"enabled": False}},
    )
    # project_dir = queue_root.parent.parent = tmp_path (so specs/<id> resolves here).
    scheduler = DispatchScheduler(store, config, executor=None, owner_id="dispatch-1", project_dir=tmp_path)
    return store, scheduler, queue_root


def _seed_planned_spec(tmp_path, spec_id: str) -> None:
    """A spec whose plan phase just completed: a fresh handoff.yaml points plan ->
    implement, which _advance_after_success reads to resolve the next phase."""
    spec_dir = tmp_path / ".builder" / "specs" / spec_id
    spec_dir.mkdir(parents=True, exist_ok=True)
    (spec_dir / "handoff.yaml").write_text(
        yaml.safe_dump(
            {"completed_phase": "plan", "next_phase": "implement", "spec": spec_id, "ready": True},
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def _enqueue_completed_plan_item(store, spec_id: str):
    item = store.enqueue(
        task_ref={
            "kind": "builder-phase-batch",
            "runner_task_ref": f".builder/specs/{spec_id}/runs/phase-plan.yaml",
            "spec_id": spec_id,
        },
        lane="claude",
    )
    return store.get_item(item.id)


def test_plan_gate_true_does_not_auto_enqueue_implement(tmp_path):
    spec_id = "ab-bench-plangate"
    _seed_planned_spec(tmp_path, spec_id)
    store, scheduler, queue_root = _make_scheduler(tmp_path, plan_gate=True)
    item = _enqueue_completed_plan_item(store, spec_id)

    before = len(store.reconstruct().items)
    # Drive the real advance logic for a COMPLETED plan phase.
    scheduler._advance_after_success(item, "plan")
    after_items = store.reconstruct().items
    after = len(after_items)

    # plan_gate=True => NO new (implement) item was enqueued.
    assert after == before, "plan_gate:true must NOT auto-enqueue the post-plan phase"
    refs = [str(it.task_ref.get("runner_task_ref", "")) for it in after_items.values()]
    assert not any("phase-implement" in r for r in refs), "no implement phase may be enqueued under plan_gate:true"
    # Instead, a plan-approval GATE marker is written (the pipeline HOLDS after plan).
    gate_marker = queue_root / "queue" / "gates" / f"{spec_id}.json"
    assert gate_marker.exists(), "plan_gate:true must write a plan-approval gate marker and hold"


def test_plan_gate_false_auto_enqueues_implement_the_contamination(tmp_path):
    # The control: with plan_gate FALSE (the common live setting) the SAME code path
    # DOES auto-enqueue implement — this is exactly the contamination the harness's
    # temp config (plan_gate:true + isolated queue) prevents.
    spec_id = "ab-bench-nogate"
    _seed_planned_spec(tmp_path, spec_id)
    store, scheduler, queue_root = _make_scheduler(tmp_path, plan_gate=False)
    item = _enqueue_completed_plan_item(store, spec_id)

    before = len(store.reconstruct().items)
    scheduler._advance_after_success(item, "plan")
    after_items = store.reconstruct().items

    assert len(after_items) == before + 1, "plan_gate:false auto-advances (enqueues the next phase)"
    refs = [str(it.task_ref.get("runner_task_ref", "")) for it in after_items.values()]
    assert any("phase-implement" in r for r in refs), "plan_gate:false enqueues the implement phase"
    # And NO gate marker is written (the pipeline did not hold).
    assert not (queue_root / "queue" / "gates" / f"{spec_id}.json").exists()
