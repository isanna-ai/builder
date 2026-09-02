from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location("planning_intent_diag", SCRIPTS / "planning.py")
planning = importlib.util.module_from_spec(spec)
sys.modules["planning_intent_diag"] = planning
spec.loader.exec_module(planning)


def test_invalid_intents_become_path_keyed_diagnostics(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".builder" / "intents" / "bad").mkdir(parents=True)
    (repo / ".builder" / "intents" / "bad" / "intent.yaml").write_text("artifact: nope\n", encoding="utf-8")
    inventory, diagnostics = planning.intent_inventory(repo)
    assert not inventory
    assert diagnostics
    assert diagnostics[0].path == ".builder/intents/bad/intent.yaml"


def test_malformed_yaml_is_a_diagnostic_instead_of_crashing_inventory(tmp_path):
    repo = tmp_path / "repo"
    path = repo / ".builder" / "intents" / "broken" / "intent.yaml"
    path.parent.mkdir(parents=True)
    path.write_text("artifact: intent-object\nsuccess_criteria: [\n", encoding="utf-8")
    inventory, diagnostics = planning.intent_inventory(repo)
    assert inventory == []
    assert diagnostics[0].path == ".builder/intents/broken/intent.yaml"
    assert "malformed YAML" in diagnostics[0].findings[0]


def test_unreadable_intent_file_is_a_path_keyed_diagnostic(tmp_path):
    repo = tmp_path / "repo"
    path = repo / ".builder" / "intents" / "unreadable" / "intent.yaml"
    path.parent.mkdir(parents=True)
    path.write_text("artifact: intent-object\n", encoding="utf-8")
    real_read_text = Path.read_text

    def refusing_read(candidate, *args, **kwargs):
        if candidate == path:
            raise PermissionError("permission denied for test")
        return real_read_text(candidate, *args, **kwargs)

    with patch.object(Path, "read_text", refusing_read):
        inventory, diagnostics = planning.intent_inventory(repo)
    assert inventory == []
    assert diagnostics[0].path == ".builder/intents/unreadable/intent.yaml"
    assert "unreadable intent file" in diagnostics[0].findings[0]


def test_duplicate_declared_ids_are_reported_even_when_directory_agreement_also_fails(tmp_path):
    repo = tmp_path / "repo"
    for directory in ("same", "other"):
        path = repo / ".builder" / "intents" / directory / "intent.yaml"
        path.parent.mkdir(parents=True)
        path.write_text(
            "artifact: intent-object\nintent: same\ntitle: t\nstatus: proposed\nproblem: p\nwhy: w\n"
            "success_criteria:\n  - id: SC-1\n    statement: s\nnon_goals:\n  - n\n"
            "ssot_delta:\n  capabilities: []\n  behaviors: []\n  journeys: []\nspecs: []\n",
            encoding="utf-8",
        )
    inventory, diagnostics = planning.intent_inventory(repo)
    assert inventory == []
    combined = "\n".join(finding for item in diagnostics for finding in item.findings)
    assert "duplicate intent id 'same'" in combined
    assert "does not match parent directory" in combined


def test_malformed_delta_entries_are_deterministic_diagnostics(tmp_path):
    bad_deltas = (
        "  capabilities:\n    - target: cap\n      change: invent\n",
        "  capabilities:\n    - target: cap\n      change: create\n      rogue: true\n",
    )
    for index, bad_delta in enumerate(bad_deltas):
        repo = tmp_path / str(index) / "repo"
        path = repo / ".builder" / "intents" / "bad" / "intent.yaml"
        path.parent.mkdir(parents=True)
        path.write_text(
            "artifact: intent-object\nintent: bad\ntitle: t\nstatus: proposed\nproblem: p\nwhy: w\n"
            "success_criteria:\n  - id: SC-1\n    statement: s\nnon_goals:\n  - n\n"
            f"ssot_delta:\n{bad_delta}  behaviors: []\n  journeys: []\nspecs: []\n",
            encoding="utf-8",
        )
        inventory, diagnostics = planning.intent_inventory(repo)
        assert inventory == []
        assert diagnostics and "ssot_delta.capabilities[0]" in diagnostics[0].findings[0]
