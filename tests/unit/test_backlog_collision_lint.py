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

planning_spec = importlib.util.spec_from_file_location("planning_backlog_lint", SCRIPTS / "planning.py")
planning = importlib.util.module_from_spec(planning_spec)
sys.modules["planning_backlog_lint"] = planning
planning_spec.loader.exec_module(planning)


def _intent(root: Path, intent_id: str, target: str, change: str) -> None:
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
        f"  capabilities:\n    - target: {target}\n      change: {change}\n"
        "  behaviors: []\n  journeys: []\n"
        "specs: []\n",
        encoding="utf-8",
    )


def test_release_lint_blocks_on_exact_capability_collision(tmp_path):
    root = tmp_path / "repo"
    _intent(root, "a", "capability.search", "create")
    _intent(root, "b", "capability.search", "rewire")
    release = root / ".builder" / "releases" / "demo.yaml"
    release.parent.mkdir(parents=True, exist_ok=True)
    release.write_text(
        "release: demo\nproduct: demo\ntitle: demo\nstatus: active\nintents:\n  - a\n  - b\n",
        encoding="utf-8",
    )
    args = SimpleNamespace(root=str(root), projects_root=str(tmp_path), release_id=None, verbose=False)
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        code = planning.cmd_release_lint(args)
    assert code == 1
    assert "backlog capability collision capability.search" in err.getvalue()
    assert "a [demo] accepted create" in err.getvalue()
    assert "b [demo] accepted rewire" in err.getvalue()


def test_release_lint_stays_green_for_unique_capability_targets(tmp_path):
    root = tmp_path / "repo"
    _intent(root, "a", "capability.alpha", "create")
    _intent(root, "b", "capability.beta", "rewire")
    release = root / ".builder" / "releases" / "demo.yaml"
    release.parent.mkdir(parents=True, exist_ok=True)
    release.write_text(
        "release: demo\nproduct: demo\ntitle: demo\nstatus: active\nintents:\n  - a\n  - b\n",
        encoding="utf-8",
    )
    args = SimpleNamespace(root=str(root), projects_root=str(tmp_path), release_id=None, verbose=False)
    assert planning.cmd_release_lint(args) == 0
