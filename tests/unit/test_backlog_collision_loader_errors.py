from __future__ import annotations

import importlib.util
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

planning_spec = importlib.util.spec_from_file_location("planning_backlog_loader_errors", SCRIPTS / "planning.py")
planning = importlib.util.module_from_spec(planning_spec)
sys.modules["planning_backlog_loader_errors"] = planning
planning_spec.loader.exec_module(planning)


def test_backlog_inventory_fails_closed_on_invalid_intent_file(tmp_path):
    root = tmp_path / "repo"
    release = root / ".builder" / "releases" / "demo.yaml"
    release.parent.mkdir(parents=True, exist_ok=True)
    release.write_text(
        "release: demo\nproduct: demo\ntitle: demo\nstatus: active\nintents:\n  - broken\n",
        encoding="utf-8",
    )
    intent = root / ".builder" / "intents" / "broken" / "intent.yaml"
    intent.parent.mkdir(parents=True, exist_ok=True)
    intent.write_text(
        "artifact: intent-object\nintent: broken\ntitle: broken\nstatus: accepted\nproblem: p\nwhy: w\n"
        "success_criteria:\n  - id: sc-1\n    statement: s\nnon_goals:\n  - n\n"
        "ssot_delta:\n  capabilities:\n    - target: \n      change: create\n  behaviors: []\n  journeys: []\n"
        "specs: []\n",
        encoding="utf-8",
    )

    index, diagnostics = planning.active_backlog_capability_index(root, planning.Registry(tmp_path, root))

    assert index == {}
    assert diagnostics
    assert any("required non-empty string" in finding for finding in diagnostics), diagnostics


def test_canonical_inventory_validation_error_cannot_fall_back_to_empty_green(tmp_path):
    fixture = ROOT / "tests" / "fixtures" / "builder_project_model" / "home" / "portfolio"
    portfolio = tmp_path / "portfolio"
    shutil.copytree(fixture, portfolio)
    for repo_name in ("alpha-repo", "beta-repo", "shared-repo"):
        (portfolio / repo_name / ".git").mkdir()
    alpha_repo = portfolio / "alpha-repo"
    intent = alpha_repo / ".builder" / "intents" / "alpha-release-work" / "intent.yaml"
    intent.write_text(
        intent.read_text(encoding="utf-8").replace(
            "  capabilities: []",
            "  capabilities:\n    - target:\n      change: create",
        ),
        encoding="utf-8",
    )
    registry = planning.Registry(portfolio, alpha_repo)

    index, diagnostics = planning.active_backlog_capability_index(alpha_repo, registry)

    assert index == {}
    assert diagnostics
    assert any("required non-empty string" in finding for finding in diagnostics), diagnostics
