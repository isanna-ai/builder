from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location("planning_intent_membership", SCRIPTS / "planning.py")
planning = importlib.util.module_from_spec(spec)
sys.modules["planning_intent_membership"] = planning
spec.loader.exec_module(planning)


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / ".builder" / "specs" / "a").mkdir(parents=True)
    (repo / ".builder" / "specs" / "a" / "spec.yaml").write_text("status: planned\n", encoding="utf-8")
    intent = repo / ".builder" / "intents" / "i1"
    intent.mkdir(parents=True)
    return repo


def test_intent_membership_resolves_with_release_ref_grammar(tmp_path):
    repo = _repo(tmp_path)
    (repo / ".builder" / "intents" / "i1" / "intent.yaml").write_text(
        "artifact: intent-object\nintent: i1\ntitle: t\nstatus: accepted\nproblem: p\nwhy: w\n"
        "success_criteria:\n  - id: SC-1\n    statement: s\nnon_goals:\n  - n\n"
        "ssot_delta:\n  capabilities: []\n  behaviors: []\n  journeys: []\nspecs:\n  - a\n",
        encoding="utf-8",
    )
    inventory, diagnostics = planning.intent_inventory(repo)
    assert not diagnostics
    assert inventory[0].members[0].canonical_ref == "a"


def test_intent_membership_surfaces_dangling_member(tmp_path):
    repo = _repo(tmp_path)
    (repo / ".builder" / "intents" / "i1" / "intent.yaml").write_text(
        "artifact: intent-object\nintent: i1\ntitle: t\nstatus: accepted\nproblem: p\nwhy: w\n"
        "success_criteria:\n  - id: SC-1\n    statement: s\nnon_goals:\n  - n\n"
        "ssot_delta:\n  capabilities: []\n  behaviors: []\n  journeys: []\nspecs:\n  - ghost\n",
        encoding="utf-8",
    )
    inventory, _ = planning.intent_inventory(repo)
    assert "dangling member" in inventory[0].findings[0]
