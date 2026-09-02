"""Unit tests for the A/B memory-gain runner's PURE logic.

The runner orchestrates real `claude -p` plan phases, so these tests touch ONLY
the side-effect-free core: the arm -> env-delta mapping (the heart of the A/B
toggle) and the dry-run plan shape. No subprocess, no hivemind, no network.

The local pytest runner (./pytest) supports only the `tmp_path` fixture, so each
test is a plain function and the hyphenated script is loaded via importlib.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def _load_runner():
    # scripts/ab-memory-gain.py is hyphenated => not importable by name.
    path = Path(__file__).resolve().parents[2] / "scripts" / "ab-memory-gain.py"
    spec = importlib.util.spec_from_file_location("ab_memory_gain", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["ab_memory_gain"] = module
    spec.loader.exec_module(module)
    return module


# --- arm -> env delta (the A/B toggle) --------------------------------------


def test_off_arm_strips_both_hivemind_vars():
    rt = _load_runner()
    base = {"PATH": "/bin", "HIVEMIND_MCP_URL": "http://hive", "HIVEMIND_API_KEY": "k"}
    env = rt.arm_env(rt.ARM_OFF, base)
    assert "HIVEMIND_MCP_URL" not in env
    assert "HIVEMIND_API_KEY" not in env
    # Unrelated vars are preserved; the source dict is never mutated.
    assert env["PATH"] == "/bin"
    assert base["HIVEMIND_MCP_URL"] == "http://hive"


def test_hivemind_arm_sets_both_vars_from_live_values():
    rt = _load_runner()
    base = {"PATH": "/bin"}  # off by default (vars absent)
    live = {"HIVEMIND_MCP_URL": "http://hive", "HIVEMIND_API_KEY": "secret"}
    env = rt.arm_env(rt.ARM_HIVEMIND, base, live)
    assert env["HIVEMIND_MCP_URL"] == "http://hive"
    assert env["HIVEMIND_API_KEY"] == "secret"


def test_hivemind_arm_carries_through_base_when_no_live_given():
    rt = _load_runner()
    base = {"HIVEMIND_MCP_URL": "http://hive", "HIVEMIND_API_KEY": "k", "PATH": "/bin"}
    env = rt.arm_env(rt.ARM_HIVEMIND, base)
    assert env["HIVEMIND_MCP_URL"] == "http://hive"
    assert env["HIVEMIND_API_KEY"] == "k"


def test_unknown_arm_raises():
    rt = _load_runner()
    raised = False
    try:
        rt.arm_env("holographic", {})
    except ValueError:
        raised = True
    assert raised


def test_env_delta_labels_strip_and_set():
    rt = _load_runner()
    base = {"HIVEMIND_MCP_URL": "u", "HIVEMIND_API_KEY": "k"}
    off = rt.env_delta(rt.ARM_OFF, base)
    assert off["HIVEMIND_MCP_URL"] == "STRIPPED"
    assert off["HIVEMIND_API_KEY"] == "STRIPPED"
    # From an env where the vars are absent, the hivemind arm SETs them.
    bare = {"PATH": "/bin"}
    live = {"HIVEMIND_MCP_URL": "u", "HIVEMIND_API_KEY": "k"}
    hv = rt.env_delta(rt.ARM_HIVEMIND, bare, live)
    assert hv["HIVEMIND_MCP_URL"] == "SET (was unset)"


def test_env_delta_masks_values():
    rt = _load_runner()
    base = {"HIVEMIND_MCP_URL": "http://hive", "HIVEMIND_API_KEY": "super-secret-key"}
    delta = rt.env_delta(rt.ARM_HIVEMIND, base)
    # The delta is a label, never the raw secret value.
    assert "super-secret-key" not in "".join(delta.values())


def test_hivemind_available():
    rt = _load_runner()
    assert rt.hivemind_available({"HIVEMIND_MCP_URL": "u", "HIVEMIND_API_KEY": "k"})
    assert not rt.hivemind_available({"HIVEMIND_MCP_URL": "u"})
    assert not rt.hivemind_available({"HIVEMIND_MCP_URL": "u", "HIVEMIND_API_KEY": ""})


# --- comparable benchmark intents -------------------------------------------


def test_benchmark_intents_are_comparable():
    rt = _load_runner()
    a = rt.benchmark_intent(0)
    b = rt.benchmark_intent(1)
    # Related finance value-object family: same conventions, different value object.
    assert "value-object" in a and "value-object" in b
    assert "DomainError" in a and "DomainError" in b
    # Moderately substantial (not a one-line trivial helper) and plan-only.
    assert len(a) > 200
    assert "do not implement" in a.lower()
    # Same arm produces the SAME intent (deterministic).
    assert rt.benchmark_intent(0) == a


def test_benchmark_intents_namespaced_per_arm_but_comparable():
    rt = _load_runner()
    off = rt.benchmark_intent(0, rt.ARM_OFF)
    hive = rt.benchmark_intent(0, rt.ARM_HIVEMIND)
    # Differ only by the benign arm namespace token -> still comparable work.
    assert off != hive
    assert off.replace("_off", "_X") == hive.replace("_hive", "_X")


def test_benchmark_spec_id_namespaced_per_arm():
    rt = _load_runner()
    off = rt.benchmark_spec_id(0, "stamp", rt.ARM_OFF)
    hive = rt.benchmark_spec_id(0, "stamp", rt.ARM_HIVEMIND)
    assert off != hive
    assert "off" in off and "hive" in hive
    assert off.startswith("ab-bench-stamp")


# --- command builders --------------------------------------------------------


def test_plan_phase_ref_and_commands():
    rt = _load_runner()
    assert rt.plan_phase_ref("s1") == ".builder/specs/s1/runs/phase-plan.yaml"
    enq = rt.enqueue_plan_command("cfg.yaml", "s1")
    assert "enqueue" in enq and ".builder/specs/s1/runs/phase-plan.yaml" in enq
    run_cmd = rt.run_once_command("cfg.yaml")
    assert "run" in run_cmd and "--once" in run_cmd
    rep = rt.report_command("/root", None, telegram=True)
    assert "report" in rep and "--telegram" in rep
    rep2 = rt.report_command("/root", "/out.md", telegram=False)
    assert "--no-telegram" in rep2 and "/out.md" in rep2


# --- dry-run plan shape ------------------------------------------------------


def test_dry_run_plan_shape_two_arms_two_specs():
    rt = _load_runner()
    plan = rt.build_dry_run_plan(
        config_path="/root/.builder/ab-dispatch-X.yaml",
        root="/root",
        arm_specs={rt.ARM_OFF: ["off0", "off1"], rt.ARM_HIVEMIND: ["hv0", "hv1"]},
        arms=[rt.ARM_OFF, rt.ARM_HIVEMIND],
        base_env={"HIVEMIND_MCP_URL": "u", "HIVEMIND_API_KEY": "k"},
        live_hivemind={"HIVEMIND_MCP_URL": "u", "HIVEMIND_API_KEY": "k"},
        do_seed=True,
        do_report=True,
        telegram=False,
        temp_config="/root/.builder/ab-dispatch-X.yaml",
        isolated_queue="/root/.builder/ab-queue-X",
        plan_gate=True,
    )
    assert set(plan.keys()) == {"arms", "arm_specs", "specs", "seed", "steps", "report", "isolation"}
    # 1 seed + (2 arms * 2 specs) plan steps.
    kinds = [s["kind"] for s in plan["steps"]]
    assert kinds.count("seed") == 1
    assert kinds.count("plan") == 4
    # Unpaired: each arm carries its OWN specs, never shared.
    assert plan["arm_specs"][rt.ARM_OFF] == ["off0", "off1"]
    assert plan["arm_specs"][rt.ARM_HIVEMIND] == ["hv0", "hv1"]
    assert set(plan["arm_specs"][rt.ARM_OFF]).isdisjoint(plan["arm_specs"][rt.ARM_HIVEMIND])
    # Each plan step's expected memory_mode equals its arm.
    for step in plan["steps"]:
        if step["kind"] == "plan":
            assert step["memory_mode_expected"] == step["arm"]
            assert "env_delta" in step and "enqueue_command" in step and "run_command" in step
    # Isolation guarantees surfaced for the dry-run proof.
    assert plan["isolation"]["plan_gate"] is True
    assert plan["isolation"]["temp_config"] == "/root/.builder/ab-dispatch-X.yaml"
    assert plan["isolation"]["isolated_queue"] == "/root/.builder/ab-queue-X"
    assert plan["report"] is not None
    assert plan["report"]["telegram"] is False


def test_dry_run_plan_no_seed_no_report():
    rt = _load_runner()
    plan = rt.build_dry_run_plan(
        config_path="cfg.yaml",
        root="/root",
        arm_specs={rt.ARM_OFF: ["s0"]},
        arms=[rt.ARM_OFF],
        base_env={},
        live_hivemind=None,
        do_seed=False,
        do_report=False,
        telegram=False,
    )
    assert plan["seed"] is False
    assert plan["report"] is None
    assert [s["kind"] for s in plan["steps"]] == ["plan"]


def test_render_dry_run_is_text_and_mentions_arms_and_specs():
    rt = _load_runner()
    plan = rt.build_dry_run_plan(
        config_path="/root/.builder/ab-dispatch-X.yaml",
        root="/root",
        arm_specs={rt.ARM_OFF: ["off0", "off1"], rt.ARM_HIVEMIND: ["hv0", "hv1"]},
        arms=[rt.ARM_OFF, rt.ARM_HIVEMIND],
        base_env={"HIVEMIND_MCP_URL": "u", "HIVEMIND_API_KEY": "k"},
        live_hivemind={"HIVEMIND_MCP_URL": "u", "HIVEMIND_API_KEY": "k"},
        do_seed=True,
        do_report=True,
        telegram=False,
        temp_config="/root/.builder/ab-dispatch-X.yaml",
        isolated_queue="/root/.builder/ab-queue-X",
        plan_gate=True,
    )
    text = rt.render_dry_run(plan)
    assert "DRY RUN" in text
    assert "off" in text and "hivemind" in text
    assert "off0" in text and "hv0" in text
    assert "SEED" in text
    # The isolation guarantees are visible in the rendered text.
    assert "plan_gate" in text and "true" in text.lower()
    assert "ab-queue-X" in text
    # The raw api key value never appears in the rendered text.
    assert "secret" not in text.lower()


# --- manifest round-trip (uses tmp_path) ------------------------------------


def test_manifest_round_trip(tmp_path):
    rt = _load_runner()
    rt.save_manifest(tmp_path, ["a", "b"], "stamp1")
    loaded = rt.load_manifest(tmp_path)
    assert loaded["spec_ids"] == ["a", "b"]
    assert loaded["stamp"] == "stamp1"
    # resolve prefers explicit --specs over the manifest.
    assert rt.resolve_spec_ids(tmp_path, "x,y , z") == ["x", "y", "z"]
    assert rt.resolve_spec_ids(tmp_path, None) == ["a", "b"]


def test_manifest_round_trip_with_arm_specs(tmp_path):
    rt = _load_runner()
    arm_specs = {rt.ARM_OFF: ["off0", "off1"], rt.ARM_HIVEMIND: ["hv0", "hv1"]}
    rt.save_manifest(
        tmp_path, ["off0", "off1", "hv0", "hv1"], "stamp1",
        arm_specs=arm_specs,
        temp_config="/root/.builder/ab-dispatch-stamp1.yaml",
        isolated_queue="/root/.builder/ab-queue-stamp1",
    )
    loaded = rt.load_manifest(tmp_path)
    assert loaded["arm_specs"] == arm_specs
    assert loaded["temp_config"].endswith("ab-dispatch-stamp1.yaml")
    assert loaded["isolated_queue"].endswith("ab-queue-stamp1")
    # resolve_arm_specs prefers the manifest's per-arm map.
    resolved = rt.resolve_arm_specs(tmp_path, None, [rt.ARM_OFF, rt.ARM_HIVEMIND])
    assert resolved == arm_specs
    # explicit --specs applies the same list to every arm.
    expl = rt.resolve_arm_specs(tmp_path, "x,y", [rt.ARM_OFF, rt.ARM_HIVEMIND])
    assert expl == {rt.ARM_OFF: ["x", "y"], rt.ARM_HIVEMIND: ["x", "y"]}


def test_load_manifest_missing_returns_empty(tmp_path):
    rt = _load_runner()
    loaded = rt.load_manifest(tmp_path)
    assert loaded["spec_ids"] == []


# --- drain-loop dispatch detection (fix 1) ----------------------------------


def test_run_dispatched_nothing_detects_drained_and_busy():
    rt = _load_runner()
    # Drained signals.
    assert rt._run_dispatched_nothing("") is True
    assert rt._run_dispatched_nothing("dispatched []") is True
    assert rt._run_dispatched_nothing("nothing to dispatch") is True
    assert rt._run_dispatched_nothing("no dispatch line here") is True
    # A real dispatch of a work item => keep going (NOT drained).
    assert rt._run_dispatched_nothing("dispatched ['work-abc123']") is False
    # The LAST dispatch line wins (a busy line after an empty one).
    busy = "dispatched []\nthen later\ndispatched ['work-xyz']"
    assert rt._run_dispatched_nothing(busy) is False
    # An empty list after a busy one => drained.
    drained_last = "dispatched ['work-1']\nlater\ndispatched []"
    assert rt._run_dispatched_nothing(drained_last) is True


# --- push-distilled binding budget (fix 4) ----------------------------------


def test_push_distilled_budget_binds():
    rt = _load_runner()
    flags = rt.ARM_ENV_FLAGS[rt.ARM_PUSH_DISTILLED]
    # 800 binds against the ~1744-char seeded prior-art block (4000 never bound).
    assert flags["PRIOR_ART_CHAR_BUDGET"] == "800"
    assert flags["PRIOR_ART_REL_GATE"] == "0.5"
    assert flags["MEMORY_DISTILL_MODEL"]  # distiller is set


# --- arm-aware report (fix 3) -----------------------------------------------


def test_render_arm_report_groups_by_arm_and_ignores_unmanifested():
    rt = _load_runner()
    manifest = {
        "arm_specs": {
            rt.ARM_OFF: ["off0", "off1"],
            rt.ARM_PUSH_DISTILLED: ["pdis0"],
        }
    }
    records = [
        {"spec_id": "off0", "plan_tokens_out": 100, "plan_wall_ms": 1000,
         "recall_hits": 0, "decisions_reused": 0, "prior_art_tokens": 0, "decisions_distilled": 0},
        {"spec_id": "off1", "plan_tokens_out": 200, "plan_wall_ms": 2000,
         "recall_hits": 0, "decisions_reused": 0, "prior_art_tokens": 0, "decisions_distilled": 0},
        {"spec_id": "pdis0", "plan_tokens_out": 80, "plan_wall_ms": 900,
         "recall_hits": 3, "decisions_reused": 2, "prior_art_tokens": 750, "decisions_distilled": 5},
        # Not in the manifest -> ignored.
        {"spec_id": "some-other-run-spec", "plan_tokens_out": 9999, "plan_wall_ms": 9,
         "recall_hits": 1, "decisions_reused": 9, "prior_art_tokens": 9, "decisions_distilled": 9},
    ]
    md = rt.render_arm_report(manifest, records)
    # Pure function: a Markdown table with one row per manifested arm.
    assert "| arm | n |" in md
    assert rt.ARM_OFF in md and rt.ARM_PUSH_DISTILLED in md
    # off arm: n=2, mean plan_tokens_out=150.0, hit_rate=0.00
    assert "| off | 2 | 150.0 | 1500.0 | 0.00 |" in md
    # push-distilled arm: n=1, hit_rate=1.00, decisions_distilled mean=5.00
    assert "| push-distilled | 1 | 80.0 | 900.0 | 1.00 | 2.00 | 750.0 | 5.00 |" in md
    # The unmanifested record's outlier value never appears.
    assert "9999" not in md


def test_render_arm_report_empty_records_is_safe():
    rt = _load_runner()
    manifest = {"arm_specs": {rt.ARM_OFF: ["off0"]}}
    md = rt.render_arm_report(manifest, [])
    assert "no records matched" in md
    # Means default to 0 / no crash on empty.
    assert "| arm | n |" in md


def test_render_arm_report_means_skip_none():
    rt = _load_runner()
    manifest = {"arm_specs": {rt.ARM_PULL: ["p0", "p1"]}}
    records = [
        {"spec_id": "p0", "plan_tokens_out": None, "plan_wall_ms": 100,
         "recall_hits": None, "decisions_reused": None, "prior_art_tokens": None, "decisions_distilled": None},
        {"spec_id": "p1", "plan_tokens_out": 50, "plan_wall_ms": None,
         "recall_hits": 2, "decisions_reused": 4, "prior_art_tokens": 10, "decisions_distilled": 1},
    ]
    md = rt.render_arm_report(manifest, records)
    # plan_tokens_out mean skips the None -> 50.0; hit_rate counts p1 only -> 1/2 = 0.50.
    assert "| pull | 2 | 50.0 | 100.0 | 0.50 | 4.00 | 10.0 | 1.00 |" in md
