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

planning_spec = importlib.util.spec_from_file_location("planning_backlog_capability_query", SCRIPTS / "planning.py")
planning = importlib.util.module_from_spec(planning_spec)
sys.modules["planning_backlog_capability_query"] = planning
planning_spec.loader.exec_module(planning)


def _seed(root: Path) -> None:
    (root / ".builder" / "releases").mkdir(parents=True, exist_ok=True)
    (root / ".builder" / "intents" / "a").mkdir(parents=True, exist_ok=True)
    (root / ".builder" / "releases" / "demo.yaml").write_text(
        "release: demo\nproduct: demo\ntitle: demo\nstatus: active\nintents:\n  - a\n",
        encoding="utf-8",
    )
    (root / ".builder" / "intents" / "a" / "intent.yaml").write_text(
        "artifact: intent-object\nintent: a\ntitle: A\nstatus: accepted\nproblem: p\nwhy: w\n"
        "success_criteria:\n  - id: sc-1\n    statement: s\nnon_goals:\n  - n\n"
        "ssot_delta:\n  capabilities:\n    - target: capability.search\n      change: rewire\n  behaviors: []\n  journeys: []\n"
        "specs: []\n",
        encoding="utf-8",
    )


def test_capability_owners_command_prints_explicit_zero_owner_result(tmp_path):
    root = tmp_path / "repo"
    _seed(root)
    args = SimpleNamespace(root=str(root), projects_root=str(tmp_path), target="capability.none")
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = planning.cmd_release_capability_owners(args)
    assert code == 0
    assert "no active backlog intents declare capability.none" in out.getvalue()


def test_capability_owners_command_rejects_invalid_target(tmp_path):
    root = tmp_path / "repo"
    _seed(root)
    args = SimpleNamespace(root=str(root), projects_root=str(tmp_path), target="")
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        code = planning.cmd_release_capability_owners(args)
    assert code == 2
    assert "required non-empty string" in err.getvalue()
