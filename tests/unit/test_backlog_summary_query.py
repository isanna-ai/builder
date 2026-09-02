from __future__ import annotations

import contextlib
import importlib.util
import io
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

planning_spec = importlib.util.spec_from_file_location("planning_backlog_summary_query", SCRIPTS / "planning.py")
planning = importlib.util.module_from_spec(planning_spec)
sys.modules["planning_backlog_summary_query"] = planning
planning_spec.loader.exec_module(planning)


def test_backlog_summary_is_deterministic_and_authored_ordered(tmp_path):
    root = tmp_path / "repo"
    (root / ".builder" / "releases").mkdir(parents=True, exist_ok=True)
    for intent_id, target in (("a", "capability.alpha"), ("b", "capability.beta")):
        path = root / ".builder" / "intents" / intent_id / "intent.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "artifact: intent-object\n"
            f"intent: {intent_id}\n"
            f"title: {intent_id}\n"
            "status: accepted\nproblem: p\nwhy: w\n"
            "success_criteria:\n  - id: sc-1\n    statement: s\n"
            "non_goals:\n  - n\n"
            "ssot_delta:\n"
            f"  capabilities:\n    - target: {target}\n      change: create\n"
            "  behaviors: []\n  journeys: []\n"
            "specs: []\n",
            encoding="utf-8",
        )
    (root / ".builder" / "releases" / "demo.yaml").write_text(
        "release: demo\nproduct: demo\ntitle: demo\nstatus: active\nintents:\n  - a\n  - b\n",
        encoding="utf-8",
    )

    args = SimpleNamespace(root=str(root), projects_root=str(tmp_path))
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = planning.cmd_release_backlog_summary(args)
    assert code == 0
    assert out.getvalue().splitlines()[:2] == [
        "capability.alpha: a [demo] accepted create",
        "capability.beta: b [demo] accepted create",
    ]
