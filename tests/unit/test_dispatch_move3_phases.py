"""Move 3 — the sync-era pipeline (spec -> plan -> implement -> verify -> sync).

These tests pin the dual-mode phase model: new specs run the 4-phase chain while
in-flight legacy 7-phase specs keep normalizing. The completion predicate is
exercised by injecting already-parsed artifact dicts (via _safe_yaml) so the test
verifies the *validation logic*, not YAML serialization — production parses these
with the container venv's real PyYAML, and the host yaml shim cannot round-trip
nested mappings.
"""

from __future__ import annotations

from pathlib import Path

from _dispatch_runtime import phase_runtime as pr
from _dispatch_runtime.phase_runtime import (
    LEGACY_PHASE_ORDER,
    PHASE_ORDER,
    REVIEW_PHASES,
    SPEC_PHASE_ORDER,
    build_phase_goal,
    detect_phase,
    expected_spec_status,
    model_for_phase,
    next_phase,
    normalize_phase,
    required_phase_artifact_groups,
    validate_phase_completion,
)
from _dispatch_runtime.phase_routing import capability_for_phase, route_lane


# --- phase order / normalization --------------------------------------------
def test_pipeline_default_includes_terminal_sync():
    assert PHASE_ORDER == ["spec", "plan", "implement", "verify", "sync"]
    assert SPEC_PHASE_ORDER == ["spec", "plan", "implement", "verify", "sync"]
    assert LEGACY_PHASE_ORDER[0] == "1-specify" and LEGACY_PHASE_ORDER[-1] == "7-archive"


def test_next_phase_walks_four_phase_and_terminates():
    assert next_phase("spec") == "plan"
    assert next_phase("plan") == "implement"
    assert next_phase("implement") == "verify"
    assert next_phase("verify") == "sync"
    assert next_phase("sync") is None


def test_next_phase_still_walks_legacy_chain():
    assert next_phase("4-plan") == "5-implement"
    assert next_phase("6-verify") == "7-archive"
    assert next_phase("7-archive") is None


def test_normalize_phase_new_and_legacy():
    for p in ("spec", "plan", "implement", "verify", "sync"):
        assert normalize_phase(p) == p
    assert normalize_phase("1-specify") == "1-specify"
    assert normalize_phase("4") == "4-plan"           # legacy bare number
    assert normalize_phase("design") == "2-design"    # legacy keyword
    assert normalize_phase("nonsense") is None


# --- routing: model / capability / lane -------------------------------------
def test_model_for_phase_sonnet_only_for_implement():
    # Committed routing (95179c4): opus for spec/plan/verify, sonnet ONLY for implement.
    assert model_for_phase("implement") == "sonnet"
    for p in ("spec", "plan", "verify", "sync"):
        assert model_for_phase(p) == "opus"


def test_capability_for_phase_four_phase():
    assert capability_for_phase("spec") == "deep_reasoner"
    assert capability_for_phase("plan") == "structured_planner"
    assert capability_for_phase("implement") == "fast_editor"
    assert capability_for_phase("verify") == "independent_reviewer"
    assert capability_for_phase("sync") == "deep_reasoner"


def test_route_lane_claude_locked_unless_codex_requested():
    lanes = ["claude", "codex"]
    assert route_lane("spec", lanes, default_lane="claude") == "claude"
    assert route_lane("implement", lanes, default_lane="claude") == "claude"
    # an explicit codex request carries through (the only path to codex)
    assert route_lane("implement", lanes, requested_lane="codex", default_lane="claude") == "codex"


def test_review_phases_include_spec_and_verify():
    assert "spec" in REVIEW_PHASES        # spec self-reviews (merged review pass)
    assert "verify" in REVIEW_PHASES
    assert "plan" not in REVIEW_PHASES
    assert "implement" not in REVIEW_PHASES


# --- expected status + artifact gates ---------------------------------------
def test_expected_status_per_phase():
    assert expected_spec_status("spec") == "specified"
    assert expected_spec_status("plan") == "planned"
    assert expected_spec_status("implement") == "implementing"
    assert expected_spec_status("verify") == "syncing"
    assert expected_spec_status("sync") == "synced"


def test_required_artifacts_per_phase():
    spec_groups = required_phase_artifact_groups("spec")
    flat = [f for g in spec_groups for f in g]
    assert "requirements.yaml" in flat and "design.yaml" in flat
    assert required_phase_artifact_groups("verify") == []  # terminal, no artifact gate


# --- goal construction ------------------------------------------------------
def _read_section(goal: str) -> str:
    marker = "=== ARTIFACTS TO READ FIRST ===\n"
    if marker not in goal:
        return ""
    return goal.split(marker, 1)[1].split("\n===", 1)[0]


def test_build_phase_goal_spec_is_merged_pass(tmp_path):
    specs_dir = tmp_path / ".builder" / "specs"
    spec_dir = specs_dir / "demo"
    spec_dir.mkdir(parents=True)
    (spec_dir / "spec.yaml").write_text(
        "name: demo\nstatus: specifying\ncurrent_phase: spec\n", encoding="utf-8"
    )
    spec_goal = build_phase_goal(tmp_path, specs_dir, "demo", "spec", None)
    plan_goal = build_phase_goal(tmp_path, specs_dir, "demo", "plan", None)

    assert "specify + design + review in ONE pass" in spec_goal
    # spec PRODUCES design — it must not be told to pre-read it; later phases do.
    assert "design.yaml" not in _read_section(spec_goal)
    assert "design.yaml" in _read_section(plan_goal)


# --- completion predicate (logic via injected parsed artifacts) -------------
def _with_fake_yaml(canned: dict[str, dict]):
    """Patch _safe_yaml to return canned dicts by filename while still honoring
    on-disk existence (validate checks .exists() before parsing)."""
    def fake(path):
        p = Path(path)
        if not p.exists():
            return None
        return canned.get(p.name, {})
    return fake


def test_validate_completion_spec_phase_passes(tmp_path):
    specs_dir = tmp_path / ".builder" / "specs"
    spec_dir = specs_dir / "demo"
    spec_dir.mkdir(parents=True)
    for fn in ("phase-log.yaml", "spec.yaml", "handoff.yaml", "requirements.yaml", "design.yaml"):
        (spec_dir / fn).write_text("x: 1\n", encoding="utf-8")
    canned = {
        "phase-log.yaml": {"phases": [
            {"phase": "spec", "completed": "2026-06-05T12:00:00Z", "outcome": "SUCCEEDED"},
        ]},
        "spec.yaml": {"status": "specified", "current_phase": "plan"},
        "handoff.yaml": {"next_phase": "plan", "ready": True, "completed_phase": "spec"},
    }
    orig = pr._safe_yaml
    pr._safe_yaml = _with_fake_yaml(canned)
    try:
        res = validate_phase_completion(specs_dir, "demo", "spec")
    finally:
        pr._safe_yaml = orig
    assert res.passed, res.reason


def test_validate_completion_verify_advances_to_sync(tmp_path):
    specs_dir = tmp_path / ".builder" / "specs"
    spec_dir = specs_dir / "demo"
    spec_dir.mkdir(parents=True)
    for fn in ("phase-log.yaml", "spec.yaml", "handoff.yaml"):
        (spec_dir / fn).write_text("x: 1\n", encoding="utf-8")
    canned = {
        "phase-log.yaml": {"phases": [
            {"phase": "verify", "completed": "2026-06-05T12:00:00Z", "outcome": "VERIFIED"},
        ]},
        "spec.yaml": {"status": "syncing", "current_phase": "sync"},
        "handoff.yaml": {"next_phase": "sync", "ready": True, "completed_phase": "verify"},
    }
    orig = pr._safe_yaml
    pr._safe_yaml = _with_fake_yaml(canned)
    try:
        res = validate_phase_completion(specs_dir, "demo", "verify")
    finally:
        pr._safe_yaml = orig
    assert res.passed, res.reason


def test_validate_completion_rejects_self_loop_current_phase(tmp_path):
    """If the agent leaves current_phase on the just-completed phase, the gate
    must fail (else the dispatcher would re-run the same phase forever)."""
    specs_dir = tmp_path / ".builder" / "specs"
    spec_dir = specs_dir / "demo"
    spec_dir.mkdir(parents=True)
    for fn in ("phase-log.yaml", "spec.yaml", "handoff.yaml", "requirements.yaml", "design.yaml"):
        (spec_dir / fn).write_text("x: 1\n", encoding="utf-8")
    canned = {
        "phase-log.yaml": {"phases": [
            {"phase": "spec", "completed": "2026-06-05T12:00:00Z", "outcome": "SUCCEEDED"},
        ]},
        "spec.yaml": {"status": "specified", "current_phase": "spec"},  # <- did not advance
        "handoff.yaml": {"next_phase": "plan", "ready": True, "completed_phase": "spec"},
    }
    orig = pr._safe_yaml
    pr._safe_yaml = _with_fake_yaml(canned)
    try:
        res = validate_phase_completion(specs_dir, "demo", "spec")
    finally:
        pr._safe_yaml = orig
    assert not res.passed


# --- #14: verify auto-archive must not break terminal completion -------------
def test_resolve_spec_dir_canonical_then_archive_no_false_positive(tmp_path):
    specs = tmp_path / "specs"
    (specs / "demo").mkdir(parents=True)
    assert pr._resolve_spec_dir(specs, "demo") == specs / "demo"  # canonical wins
    (specs / "demo").rmdir()
    arch = specs / "archive" / "2026-06-05-demo"
    arch.mkdir(parents=True)
    assert pr._resolve_spec_dir(specs, "demo") == arch  # falls back to archive
    # a different archived spec sharing a trailing word must NOT match
    (specs / "archive" / "2026-06-05-mutation-guard").mkdir(parents=True)
    assert pr._resolve_spec_dir(specs, "guard") == specs / "guard"  # no false positive


def test_validate_completion_sync_tolerates_archive(tmp_path):
    """#14: a synced spec that the operator archived (dir moved, status/current
    changed) must still validate as complete — the dir move + status flip are not
    incompletion."""
    specs_dir = tmp_path / ".builder" / "specs"
    arch = specs_dir / "archive" / "2026-06-05-demo"  # canonical specs/demo is GONE
    arch.mkdir(parents=True)
    for fn in ("phase-log.yaml", "spec.yaml", "sync-result.yaml"):
        (arch / fn).write_text("x: 1\n", encoding="utf-8")
    canned = {
        "phase-log.yaml": {"phases": [
            {"phase": "sync", "completed": "2026-06-05T12:00:00Z", "outcome": "SUCCEEDED"},
            {"phase": "7-archive", "completed": "2026-06-05T12:05:00Z", "outcome": "ARCHIVED"},
        ]},
        "spec.yaml": {"status": "archived", "current_phase": "archive"},  # archive changed both
    }
    orig = pr._safe_yaml
    pr._safe_yaml = _with_fake_yaml(canned)
    try:
        res = validate_phase_completion(specs_dir, "demo", "sync")
    finally:
        pr._safe_yaml = orig
    assert res.passed, res.reason


def test_build_phase_goal_verify_advances_to_sync(tmp_path):
    specs_dir = tmp_path / ".builder" / "specs"
    spec_dir = specs_dir / "demo"
    spec_dir.mkdir(parents=True)
    (spec_dir / "spec.yaml").write_text(
        "name: demo\nstatus: implementing\ncurrent_phase: verify\n", encoding="utf-8"
    )
    goal = build_phase_goal(tmp_path, specs_dir, "demo", "verify", None)
    assert "not terminal" in goal
    assert "status: syncing" in goal
    assert "current_phase: sync" in goal


def test_validate_terminal_verify_rejects_crashed_turn(tmp_path):
    """Review finding (HIGH): the terminal relaxation must NOT accept a verify that
    never finished — a stray phase-log entry while status is still 'implementing'."""
    specs_dir = tmp_path / ".builder" / "specs"
    spec_dir = specs_dir / "demo"
    spec_dir.mkdir(parents=True)
    for fn in ("phase-log.yaml", "spec.yaml"):
        (spec_dir / fn).write_text("x: 1\n", encoding="utf-8")
    canned = {
        "phase-log.yaml": {"phases": [
            {"phase": "verify", "completed": "2026-06-05T12:00:00Z", "outcome": "VERIFIED"},
        ]},
        "spec.yaml": {"status": "implementing", "current_phase": "verify"},  # never reached verified
    }
    orig = pr._safe_yaml
    pr._safe_yaml = _with_fake_yaml(canned)
    try:
        res = validate_phase_completion(specs_dir, "demo", "verify")
    finally:
        pr._safe_yaml = orig
    assert not res.passed  # status still 'implementing' -> not a real terminal completion


def test_validate_rejects_bogus_completed_timestamp(tmp_path):
    """Review finding: `completed` must be a real timestamp, not any truthy string."""
    specs_dir = tmp_path / ".builder" / "specs"
    spec_dir = specs_dir / "demo"
    spec_dir.mkdir(parents=True)
    for fn in ("phase-log.yaml", "spec.yaml"):
        (spec_dir / fn).write_text("x: 1\n", encoding="utf-8")
    canned = {
        "phase-log.yaml": {"phases": [
            {"phase": "verify", "completed": "x", "outcome": "VERIFIED"},  # bogus timestamp
        ]},
        "spec.yaml": {"status": "verified", "current_phase": "verify"},
    }
    orig = pr._safe_yaml
    pr._safe_yaml = _with_fake_yaml(canned)
    try:
        res = validate_phase_completion(specs_dir, "demo", "verify")
    finally:
        pr._safe_yaml = orig
    assert not res.passed


def test_build_phase_goal_legacy_6verify_is_not_terminal(tmp_path):
    """Review finding (HIGH): legacy 6-verify is NOT terminal (-> 7-archive); it must
    keep the generic advancing directive, never the terminal 'do not archive' one."""
    specs_dir = tmp_path / ".builder" / "specs"
    spec_dir = specs_dir / "demo"
    spec_dir.mkdir(parents=True)
    (spec_dir / "spec.yaml").write_text(
        "name: demo\nstatus: reviewed\ncurrent_phase: 6-verify\n", encoding="utf-8"
    )
    goal = build_phase_goal(tmp_path, specs_dir, "demo", "6-verify", None)
    assert "TERMINAL phase" not in goal            # must NOT be told it is terminal
    assert "do NOT move, rename, or delete" not in goal
    assert "/sp-" not in goal                     # no legacy-brand leakage
    assert "/isanna-6-verify" in goal              # generic advancing directive


def test_detect_phase_dual_accepts_sp_and_isanna_next_action(tmp_path):
    """Consumer repos + builder baselines still hold /sp- next_action on disk;
    detect_phase must dual-accept both brands (dual-accept, not a hard flip)."""
    specs_dir = tmp_path / ".builder" / "specs"
    for spec_id, next_action in (
        ("legacy-sp", "/sp-plan legacy-sp"),
        ("new-isanna", "/isanna-plan new-isanna"),
    ):
        spec_dir = specs_dir / spec_id
        spec_dir.mkdir(parents=True)
        (spec_dir / "spec.yaml").write_text(
            f'name: {spec_id}\nstatus: specified\nnext_action: "{next_action}"\n',
            encoding="utf-8",
        )
        assert detect_phase(spec_dir, tmp_path, None) == "plan"
