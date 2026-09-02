from __future__ import annotations

from pathlib import Path

import isanna as isanna_cli
from tests.unit.sync_evidence_support import write_host_scope


def test_isanna_sync_reports_drift(tmp_path: Path):
    (tmp_path / ".builder" / "specs" / "demo").mkdir(parents=True)
    (tmp_path / ".builder" / "specs" / "demo" / "spec.yaml").write_text("status: verified\ncurrent_phase: sync\n", encoding="utf-8")
    (tmp_path / ".builder" / "specs" / "demo" / "ssot-delta.yaml").write_text("capabilities: []\nbehaviors: []\njourneys: []\n", encoding="utf-8")
    (tmp_path / "tests" / "unit").mkdir(parents=True)
    (tmp_path / "tests" / "unit" / "test_demo.py").write_text("def test_real():\n    pass\n", encoding="utf-8")
    (tmp_path / "Makefile").write_text("gate:\n\tpytest tests/unit/test_demo.py -q\n", encoding="utf-8")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "system-behaviors.yaml").write_text(
        "schema: system-behaviors/v1\nbehaviors:\n  - id: b1\n    area: x\n    behavior: y\n    invariant: z\n    breaks_when: never\n    guarding_tests:\n      - tests/unit/test_demo.py::missing\n",
        encoding="utf-8",
    )
    (tmp_path / ".builder" / "sync-adapter.yaml").write_text("artifact: sync-adapter\nmappings: []\n", encoding="utf-8")
    write_host_scope(tmp_path, "demo")
    assert isanna_cli.main(["sync", "--root", str(tmp_path), "--spec", "demo", "--scope-evidence", str(tmp_path / ".builder" / "specs" / "demo" / "sync-scope.yaml")]) == 1
    assert not (tmp_path / ".builder" / "model" / "system-model.yaml").exists()
