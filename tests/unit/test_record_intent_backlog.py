from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location("record_intent_backlog", SCRIPTS / "record.py")
record = importlib.util.module_from_spec(spec)
sys.modules["record_intent_backlog"] = record
spec.loader.exec_module(record)


def test_record_build_renders_claimed_only_intent_backlog(tmp_path):
    root = tmp_path / "repo"
    (root / ".builder" / "specs").mkdir(parents=True)
    (root / ".builder" / "intents" / "i1").mkdir(parents=True)
    (root / ".builder" / "dispatch.yaml").write_text("queue_store:\n  path: .builder/dispatch-queue\n", encoding="utf-8")
    (root / ".builder" / "intents" / "i1" / "intent.yaml").write_text(
        "artifact: intent-object\nintent: i1\ntitle: Intent title\nstatus: accepted\nproblem: p\nwhy: w\n"
        "success_criteria:\n  - id: SC-1\n    statement: s\nnon_goals:\n  - n\n"
        "ssot_delta:\n  capabilities: []\n  behaviors: []\n  journeys: []\nspecs: []\n",
        encoding="utf-8",
    )
    out = tmp_path / "out"
    assert record.main(["build", "--root", str(root), "--out", str(out)]) == 0
    page = (out / root.name / "roadmap.html").read_text(encoding="utf-8")
    assert "Intent backlog" in page
    assert "Intent title" in page
    assert "claimed" in page


def test_invalid_intent_renders_only_path_keyed_diagnostic_placeholder(tmp_path):
    root = tmp_path / "repo"
    (root / ".builder" / "specs").mkdir(parents=True)
    path = root / ".builder" / "intents" / "bad" / "intent.yaml"
    path.parent.mkdir(parents=True)
    (root / ".builder" / "dispatch.yaml").write_text("queue_store:\n  path: .builder/dispatch-queue\n", encoding="utf-8")
    path.write_text(
        "artifact: intent-object\nintent: bad\ntitle: UNTRUSTED SECRET TITLE\nstatus: proposed\n"
        "problem: UNTRUSTED PROBLEM\nwhy: w\nsuccess_criteria:\n  - id: SC-1\n    statement: s\n"
        "non_goals:\n  - n\nssot_delta:\n  capabilities: []\n  behaviors: []\n  journeys: []\n"
        "specs: []\nrogue: true\n",
        encoding="utf-8",
    )
    out = tmp_path / "out"
    assert record.main(["build", "--root", str(root), "--out", str(out)]) == 0
    page = (out / root.name / "roadmap.html").read_text(encoding="utf-8")
    assert ".builder/intents/bad/intent.yaml" in page
    assert "invalid intent file" in page
    assert "UNTRUSTED SECRET TITLE" not in page
    assert "UNTRUSTED PROBLEM" not in page
