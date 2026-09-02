from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location("planning_intent_fulfillment", SCRIPTS / "planning.py")
planning = importlib.util.module_from_spec(spec)
sys.modules["planning_intent_fulfillment"] = planning
spec.loader.exec_module(planning)


def _repo(tmp_path: Path, status: str, verification: str) -> Path:
    repo = tmp_path / "repo"
    (repo / ".builder" / "specs" / "a").mkdir(parents=True)
    (repo / ".builder" / "specs" / "a" / "spec.yaml").write_text(f"status: {status}\n", encoding="utf-8")
    (repo / ".builder" / "intents" / "i1").mkdir(parents=True)
    (repo / ".builder" / "intents" / "i1" / "intent.yaml").write_text(
        "artifact: intent-object\nintent: i1\ntitle: t\nstatus: accepted\nproblem: p\nwhy: w\n"
        "success_criteria:\n  - id: SC-1\n    statement: s\nnon_goals:\n  - n\n"
        "ssot_delta:\n  capabilities: []\n  behaviors: []\n  journeys: []\nspecs:\n  - a\n",
        encoding="utf-8",
    )
    planning._scan_cache[str(repo.resolve())] = {"specs": [{"spec": "a", "verification": verification}]}
    return repo


def test_fulfilled_requires_host_verified_and_synced(tmp_path):
    inventory, _ = planning.intent_inventory(_repo(tmp_path, "synced", "host-verified"))
    assert inventory[0].visible_state == "fulfilled"


def test_fulfilled_stays_unreachable_without_both_signals(tmp_path):
    inventory, _ = planning.intent_inventory(_repo(tmp_path, "verified", "host-verified"))
    assert inventory[0].visible_state != "fulfilled"


def test_synced_without_host_verification_is_never_fulfilled(tmp_path):
    for index, verification in enumerate(("unknown", "planned", "self-reported")):
        inventory, _ = planning.intent_inventory(_repo(tmp_path / str(index), "synced", verification))
        assert inventory[0].visible_state == "in-flight"
