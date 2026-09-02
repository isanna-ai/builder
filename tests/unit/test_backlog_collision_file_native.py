from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

planning_spec = importlib.util.spec_from_file_location("planning_backlog_file_native", SCRIPTS / "planning.py")
planning = importlib.util.module_from_spec(planning_spec)
sys.modules["planning_backlog_file_native"] = planning
planning_spec.loader.exec_module(planning)
record_spec = importlib.util.spec_from_file_location("record_backlog_file_native", SCRIPTS / "record.py")
record = importlib.util.module_from_spec(record_spec)
sys.modules["record_backlog_file_native"] = record
record_spec.loader.exec_module(record)


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _seed(root: Path) -> None:
    (root / ".builder").mkdir(parents=True)
    _write(root / ".builder" / "dispatch.yaml", {"queue_store": {"path": ".builder/dispatch-queue"}})
    _write(root / ".builder" / "product.yaml", {"product": "demo", "title": "Demo", "repos": [{"alias": root.name}]})
    _write(
        root / ".builder" / "releases" / "r1.yaml",
        {"release": "r1", "product": "demo", "title": "r1", "status": "active", "intents": ["a", "b"]},
    )
    for intent_id in ("a", "b"):
        _write(
            root / ".builder" / "intents" / intent_id / "intent.yaml",
            {
                "artifact": "intent-object",
                "intent": intent_id,
                "title": intent_id,
                "status": "accepted",
                "problem": "p",
                "why": "w",
                "success_criteria": [{"id": "sc-1", "statement": "s"}],
                "non_goals": ["n"],
                "ssot_delta": {
                    "capabilities": [{"target": "capability.shared", "change": "create"}],
                    "behaviors": [],
                    "journeys": [],
                },
                "specs": [],
            },
        )


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


def test_backlog_surfaces_are_file_native_read_only_and_do_not_change_completeness(tmp_path):
    root = tmp_path / "demo-repo"
    _seed(root)
    registry = planning._registry(root, str(tmp_path))
    release = planning.find_release(root, "r1")
    before_completeness = planning.completeness(release, registry)
    before_files = _snapshot(root)

    with patch("socket.socket", side_effect=AssertionError("network access is outside the backlog slice")):
        assert planning.main(["lint", "--root", str(root), "--projects-root", str(tmp_path)]) == 1
        assert planning.main(["capability-owners", "capability.shared", "--root", str(root), "--projects-root", str(tmp_path)]) == 0
        assert planning.main(["backlog-summary", "--root", str(root), "--projects-root", str(tmp_path)]) == 0
        assert record.main(["build", "--root", str(root), "--out", str(tmp_path / "record-out")]) == 0

    after_completeness = planning.completeness(release, registry)
    assert _snapshot(root) == before_files
    assert (after_completeness.verified, after_completeness.total, after_completeness.percent) == (
        before_completeness.verified,
        before_completeness.total,
        before_completeness.percent,
    )
    assert not list(root.rglob("*.db"))
    assert not list(root.rglob("*.sqlite*"))

