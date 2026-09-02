from __future__ import annotations

import importlib.util
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

planning_spec = importlib.util.spec_from_file_location("planning_backlog_scope", SCRIPTS / "planning.py")
planning = importlib.util.module_from_spec(planning_spec)
sys.modules["planning_backlog_scope"] = planning
planning_spec.loader.exec_module(planning)


def test_backlog_query_target_validation_is_exact_and_case_sensitive():
    assert planning.validate_backlog_target("capability.search") == "capability.search"
    assert planning.validate_backlog_target(" capability.search ") == "capability.search"
    try:
        planning.validate_backlog_target("")
    except ValueError as exc:
        assert "required non-empty string" in str(exc)
    else:
        raise AssertionError("expected invalid target rejection")


def test_backlog_query_matches_exact_target_only(tmp_path):
    root = tmp_path / "repo"
    (root / ".builder" / "releases").mkdir(parents=True, exist_ok=True)
    (root / ".builder" / "intents" / "i1").mkdir(parents=True, exist_ok=True)
    (root / ".builder" / "intents" / "i2").mkdir(parents=True, exist_ok=True)
    (root / ".builder" / "releases" / "demo.yaml").write_text(
        "release: demo\nproduct: demo\ntitle: demo\nstatus: active\nintents:\n  - i1\n  - i2\n",
        encoding="utf-8",
    )
    (root / ".builder" / "intents" / "i1" / "intent.yaml").write_text(
        "artifact: intent-object\nintent: i1\ntitle: i1\nstatus: accepted\nproblem: p\nwhy: w\n"
        "success_criteria:\n  - id: sc-1\n    statement: s\nnon_goals:\n  - n\n"
        "ssot_delta:\n  capabilities:\n    - target: capability.search\n      change: create\n  behaviors: []\n  journeys: []\n"
        "specs: []\n",
        encoding="utf-8",
    )
    (root / ".builder" / "intents" / "i2" / "intent.yaml").write_text(
        "artifact: intent-object\nintent: i2\ntitle: i2\nstatus: accepted\nproblem: p\nwhy: w\n"
        "success_criteria:\n  - id: sc-1\n    statement: s\nnon_goals:\n  - n\n"
        "ssot_delta:\n  capabilities:\n    - target: Capability.Search\n      change: create\n  behaviors: []\n  journeys: []\n"
        "specs: []\n",
        encoding="utf-8",
    )

    rows, diagnostics = planning.backlog_capability_owners(root, "capability.search", planning.Registry(tmp_path, root))

    assert diagnostics == []
    assert [row.intent_id for row in rows] == ["i1"]


def test_unreferenced_malformed_intent_does_not_poison_active_backlog(tmp_path):
    root = tmp_path / "repo"
    (root / ".builder" / "releases").mkdir(parents=True, exist_ok=True)
    active = root / ".builder" / "intents" / "active" / "intent.yaml"
    active.parent.mkdir(parents=True, exist_ok=True)
    active.write_text(
        "artifact: intent-object\nintent: active\ntitle: active\nstatus: accepted\nproblem: p\nwhy: w\n"
        "success_criteria:\n  - id: sc-1\n    statement: s\nnon_goals:\n  - n\n"
        "ssot_delta:\n  capabilities:\n    - target: capability.active\n      change: create\n"
        "  behaviors: []\n  journeys: []\nspecs: []\n",
        encoding="utf-8",
    )
    parked = root / ".builder" / "intents" / "parked" / "intent.yaml"
    parked.parent.mkdir(parents=True, exist_ok=True)
    parked.write_text("not: [valid\n", encoding="utf-8")
    (root / ".builder" / "releases" / "live.yaml").write_text(
        "release: live\nproduct: demo\ntitle: live\nstatus: active\nintents:\n  - active\n",
        encoding="utf-8",
    )

    index, diagnostics = planning.active_backlog_capability_index(
        root, planning.Registry(tmp_path, root)
    )

    assert diagnostics == []
    assert list(index) == ["capability.active"]


def test_canonical_project_inventory_loads_intents_from_resolved_release_home(tmp_path):
    fixture = ROOT / "tests" / "fixtures" / "builder_project_model" / "home" / "portfolio"
    portfolio = tmp_path / "portfolio"
    shutil.copytree(fixture, portfolio)
    for repo_name in ("alpha-repo", "beta-repo", "shared-repo"):
        (portfolio / repo_name / ".git").mkdir()
    alpha_intent = portfolio / "alpha-repo" / ".builder" / "intents" / "alpha-release-work" / "intent.yaml"
    alpha_intent.write_text(
        alpha_intent.read_text(encoding="utf-8").replace(
            "  capabilities: []",
            "  capabilities:\n    - target: capability.alpha\n      change: create",
        ),
        encoding="utf-8",
    )
    shared_repo = portfolio / "shared-repo"
    registry = planning.Registry(portfolio, shared_repo, product_context="alpha")

    index, diagnostics = planning.active_backlog_capability_index(
        shared_repo, registry, product_context="alpha"
    )

    assert diagnostics == []
    assert [row.intent_id for row in index["capability.alpha"].rows] == ["alpha-release-work"]


def test_terminal_intent_member_findings_are_excluded_from_active_backlog(tmp_path):
    root = tmp_path / "repo"
    (root / ".builder" / "releases").mkdir(parents=True, exist_ok=True)
    terminal = root / ".builder" / "intents" / "rejected-work" / "intent.yaml"
    terminal.parent.mkdir(parents=True, exist_ok=True)
    terminal.write_text(
        "artifact: intent-object\nintent: rejected-work\ntitle: rejected\nstatus: rejected\n"
        "problem: p\nwhy: w\nreason: no longer wanted\n"
        "success_criteria:\n  - id: sc-1\n    statement: s\nnon_goals:\n  - n\n"
        "ssot_delta:\n  capabilities:\n    - target: capability.parked\n      change: create\n"
        "  behaviors: []\n  journeys: []\nspecs:\n  - missing-spec\n",
        encoding="utf-8",
    )
    (root / ".builder" / "releases" / "live.yaml").write_text(
        "release: live\nproduct: demo\ntitle: live\nstatus: active\nintents:\n  - rejected-work\n",
        encoding="utf-8",
    )

    index, diagnostics = planning.active_backlog_capability_index(
        root, planning.Registry(tmp_path, root)
    )

    assert index == {}
    assert diagnostics == []
