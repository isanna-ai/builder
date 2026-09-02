from __future__ import annotations

import isanna as isanna_cli
from tests.unit.sync_evidence_support import write_host_scope


def test_sync_does_not_mutate_intent_files(tmp_path):
    (tmp_path / ".builder" / "specs" / "demo").mkdir(parents=True)
    (tmp_path / ".builder" / "specs" / "demo" / "spec.yaml").write_text("status: verified\ncurrent_phase: sync\n", encoding="utf-8")
    (tmp_path / ".builder" / "specs" / "demo" / "ssot-delta.yaml").write_text("capabilities: []\nbehaviors: []\njourneys: []\n", encoding="utf-8")
    (tmp_path / ".builder" / "intents" / "demo-intent").mkdir(parents=True)
    intent_path = tmp_path / ".builder" / "intents" / "demo-intent" / "intent.yaml"
    intent_path.write_text("artifact: intent-object\nintent: demo-intent\ntitle: t\nstatus: accepted\nproblem: p\nwhy: w\nsuccess_criteria:\n  - id: sc-1\n    statement: s\nnon_goals:\n  - n\nssot_delta:\n  capabilities: []\n  behaviors: []\n  journeys: []\nspecs:\n  - demo\n", encoding="utf-8")
    before = intent_path.read_text(encoding="utf-8")
    write_host_scope(tmp_path, "demo")
    assert isanna_cli.main(["sync", "--root", str(tmp_path), "--spec", "demo", "--scope-evidence", str(tmp_path / ".builder" / "specs" / "demo" / "sync-scope.yaml")]) == 2
    assert intent_path.read_text(encoding="utf-8") == before
