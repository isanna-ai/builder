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

planning_spec = importlib.util.spec_from_file_location("planning_backlog_lint_failures", SCRIPTS / "planning.py")
planning = importlib.util.module_from_spec(planning_spec)
sys.modules["planning_backlog_lint_failures"] = planning
planning_spec.loader.exec_module(planning)


def test_release_lint_fails_closed_on_upstream_backlog_diagnostics(tmp_path):
    root = tmp_path / "repo"
    release = root / ".builder" / "releases" / "demo.yaml"
    release.parent.mkdir(parents=True, exist_ok=True)
    release.write_text(
        "release: demo\nproduct: demo\ntitle: demo\nstatus: active\nintents:\n  - broken\n",
        encoding="utf-8",
    )
    args = SimpleNamespace(root=str(root), projects_root=str(tmp_path), release_id=None, verbose=False)
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        code = planning.cmd_release_lint(args)
    assert code == 1
    assert "missing backlog intent 'broken'" in err.getvalue()
