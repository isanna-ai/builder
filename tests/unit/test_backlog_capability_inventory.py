from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

planning_spec = importlib.util.spec_from_file_location("planning_backlog_inventory", SCRIPTS / "planning.py")
planning = importlib.util.module_from_spec(planning_spec)
sys.modules["planning_backlog_inventory"] = planning
planning_spec.loader.exec_module(planning)


def _release(root: Path, release_id: str, *, status: str, intents: list[str]) -> None:
    path = root / ".builder" / "releases" / f"{release_id}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "release: {release}\nproduct: demo\ntitle: {release}\nstatus: {status}\nintents:\n{intents}".format(
            release=release_id,
            status=status,
            intents="".join(f"  - {intent}\n" for intent in intents),
        ),
        encoding="utf-8",
    )


def _intent(root: Path, intent_id: str, *, status: str = "accepted", capabilities: list[tuple[str, str]] | None = None) -> None:
    path = root / ".builder" / "intents" / intent_id / "intent.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    cap_lines = "".join(
        f"    - target: {target}\n      change: {change}\n" for target, change in (capabilities or [])
    )
    path.write_text(
        "artifact: intent-object\n"
        f"intent: {intent_id}\n"
        f"title: {intent_id}\n"
        f"status: {status}\n"
        "problem: p\n"
        "why: w\n"
        "success_criteria:\n"
        "  - id: sc-1\n"
        "    statement: s\n"
        "non_goals:\n"
        "  - n\n"
        "ssot_delta:\n"
        "  capabilities:\n"
        f"{cap_lines}"
        "  behaviors: []\n"
        "  journeys: []\n"
        "specs: []\n",
        encoding="utf-8",
    )


def test_backlog_inventory_keeps_all_release_provenance_rows_but_counts_distinct_intents(tmp_path):
    root = tmp_path / "repo"
    _intent(root, "shared-owner", capabilities=[("capability.search", "rewire")])
    _intent(root, "second-owner", capabilities=[("capability.search", "enrich")])
    _release(root, "r1", status="active", intents=["shared-owner", "second-owner"])
    _release(root, "r2", status="draft", intents=["shared-owner"])

    registry = planning.Registry(tmp_path, root)
    index, diagnostics = planning.active_backlog_capability_index(root, registry)

    assert diagnostics == []
    owners = index["capability.search"]
    assert [owner.release_id for owner in owners.rows] == ["r1", "r1", "r2"]
    assert [owner.intent_id for owner in owners.rows] == ["shared-owner", "second-owner", "shared-owner"]
    assert owners.collision_intent_ids == ("second-owner", "shared-owner")


def test_backlog_inventory_excludes_terminal_and_historical_release_statuses(tmp_path):
    root = tmp_path / "repo"
    _intent(root, "live", capabilities=[("capability.live", "create")])
    _intent(root, "old", capabilities=[("capability.old", "create")])
    for release_id, status, intent_id in (
        ("draft-release", "draft", "live"),
        ("active-release", "active", "live"),
        ("shipped-release", "shipped", "old"),
        ("cancelled-release", "cancelled", "old"),
        ("archived-release", "archived", "old"),
        ("abandoned-release", "abandoned", "old"),
    ):
        _release(root, release_id, status=status, intents=[intent_id])

    index, diagnostics = planning.active_backlog_capability_index(root, planning.Registry(tmp_path, root))

    assert diagnostics == []
    assert sorted(index) == ["capability.live"]
    assert [row.release_id for row in index["capability.live"].rows] == ["active-release", "draft-release"]
