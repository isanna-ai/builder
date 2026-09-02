from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location("planning_intent_boundary", SCRIPTS / "planning.py")
planning = importlib.util.module_from_spec(spec)
sys.modules["planning_intent_boundary"] = planning
spec.loader.exec_module(planning)


def test_intents_do_not_change_release_completeness(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".builder" / "specs" / "a").mkdir(parents=True)
    (repo / ".builder" / "specs" / "a" / "spec.yaml").write_text("status: planned\n", encoding="utf-8")
    (repo / ".builder" / "releases").mkdir(parents=True)
    (repo / ".builder" / "releases" / "r1.yaml").write_text(
        "release: r1\nproduct: repo\ntitle: r1\nstatus: shipped\nspecs:\n  - a\n",
        encoding="utf-8",
    )
    (repo / ".builder" / "intents" / "i1").mkdir(parents=True)
    (repo / ".builder" / "intents" / "i1" / "intent.yaml").write_text(
        "artifact: intent-object\nintent: i1\ntitle: t\nstatus: accepted\nproblem: p\nwhy: w\n"
        "success_criteria:\n  - id: SC-1\n    statement: s\nnon_goals:\n  - n\n"
        "ssot_delta:\n  capabilities: []\n  behaviors: []\n  journeys: []\nspecs: []\n",
        encoding="utf-8",
    )
    comp = planning.completeness(planning.load_releases(repo)[0], planning._registry(repo, None))
    assert comp.total == 1
    assert comp.verified == 0
