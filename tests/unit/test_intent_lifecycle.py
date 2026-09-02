from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location("planning_intent_lifecycle", SCRIPTS / "planning.py")
planning = importlib.util.module_from_spec(spec)
sys.modules["planning_intent_lifecycle"] = planning
spec.loader.exec_module(planning)

EXPECTED_PRE_IMPLEMENTATION_STATUSES = {
    "specifying",
    "specified",
    "spec-reviewed",
    "designed",
    "reviewed",
    "planned",
}
EXPECTED_IMPLEMENTATION_OR_LATER_STATUSES = {
    "implementing",
    "implemented",
    "adversarially-reviewed",
    "verifying",
    "verified",
    "verified_with_tasks",
    "archived",
    "syncing",
    "synced",
}


def _repo(tmp_path: Path, status: str) -> Path:
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
    return repo


def test_decomposed_when_all_members_pre_implementation(tmp_path):
    inventory, _ = planning.intent_inventory(_repo(tmp_path, "planned"))
    assert inventory[0].visible_state == "decomposed"


def test_in_flight_when_any_member_has_entered_implementation(tmp_path):
    inventory, _ = planning.intent_inventory(_repo(tmp_path, "implementing"))
    assert inventory[0].visible_state == "in-flight"


def test_terminal_declared_states_project_exactly(tmp_path):
    repo = _repo(tmp_path, "planned")
    path = repo / ".builder" / "intents" / "i1" / "intent.yaml"
    path.write_text(path.read_text(encoding="utf-8").replace("status: accepted", "status: rejected") + "reason: nope\n", encoding="utf-8")
    inventory, _ = planning.intent_inventory(repo)
    assert inventory[0].visible_state == "rejected"


def test_every_canonical_pre_implementation_status_projects_decomposed(tmp_path):
    assert planning.PRE_IMPLEMENTATION_STATUSES == EXPECTED_PRE_IMPLEMENTATION_STATUSES
    for index, status in enumerate(sorted(EXPECTED_PRE_IMPLEMENTATION_STATUSES)):
        inventory, diagnostics = planning.intent_inventory(_repo(tmp_path / str(index), status))
        assert diagnostics == []
        assert inventory[0].visible_state == "decomposed"


def test_every_canonical_implementation_or_later_status_projects_in_flight(tmp_path):
    assert planning.IMPLEMENTATION_OR_LATER_STATUSES == EXPECTED_IMPLEMENTATION_OR_LATER_STATUSES
    for index, status in enumerate(sorted(EXPECTED_IMPLEMENTATION_OR_LATER_STATUSES)):
        inventory, diagnostics = planning.intent_inventory(_repo(tmp_path / str(index), status))
        assert diagnostics == []
        assert inventory[0].visible_state == "in-flight"


def test_unrecognized_member_status_fails_closed_at_accepted(tmp_path):
    for index, status in enumerate(("Planned", "unknown", "")):
        inventory, diagnostics = planning.intent_inventory(_repo(tmp_path / str(index), status))
        assert diagnostics == []
        assert inventory[0].visible_state == "accepted"
        assert "unrecognized status" in inventory[0].findings[0]
