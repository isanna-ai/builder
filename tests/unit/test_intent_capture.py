from __future__ import annotations

import contextlib
import importlib.util
import io
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


capture_mod = _load("isanna_capture_test", "isanna_capture.py")
isanna = _load("isanna_capture_cli_test", "isanna.py")

from _yaml import yaml
from tests.unit.public_export_support import require_repo_asset


def _run(argv):
    out = io.StringIO()
    err = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = isanna.main(argv)
    return code, out.getvalue(), err.getvalue()


# ------------------------------------------------------------------ T1: capture core


def test_capture_writes_proposed_intent(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    path = capture_mod.capture_intent(
        root,
        intent_id="idea-x",
        title="T",
        problem="P",
        why="W",
        success_criteria=["first outcome"],
    )
    assert path == root / ".builder" / "intents" / "idea-x" / "intent.yaml"
    assert path.is_file()
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert data["status"] == "proposed"
    assert data["artifact"] == "intent-object"
    assert data["why"] == "W"
    assert any(item["statement"] == "first outcome" for item in data["success_criteria"])


def test_capture_status_is_never_accepted(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    path = capture_mod.capture_intent(
        root,
        intent_id="idea-x",
        title="T",
        problem="P",
        why="W",
        success_criteria=["first outcome"],
    )
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert data["status"] == "proposed"
    assert data["status"] not in {"accepted", "rejected", "superseded"}


def test_capture_refuses_existing_id(tmp_path):
    root = tmp_path / "repo"
    intent_dir = root / ".builder" / "intents" / "idea-x"
    intent_dir.mkdir(parents=True)
    existing = intent_dir / "intent.yaml"
    existing.write_bytes(b"artifact: intent-object\nintent: idea-x\ntitle: pre\nstatus: proposed\n"
                          b"problem: p\nwhy: w\nsuccess_criteria:\n  - id: SC-1\n    statement: s\n"
                          b"non_goals:\n  - n\nssot_delta:\n  capabilities: []\n  behaviors: []\n"
                          b"  journeys: []\nspecs: []\n")
    original = existing.read_bytes()
    raised = False
    try:
        capture_mod.capture_intent(
            root,
            intent_id="idea-x",
            title="T",
            problem="P",
            why="W",
            success_criteria=["first outcome"],
        )
    except Exception:
        raised = True
    assert raised
    assert existing.read_bytes() == original


def test_capture_refuses_unsafe_id_without_writing(tmp_path):
    # Path-traversal / separator ids must be refused BEFORE any file or dir is written,
    # so capture can never escape .builder/intents/<id>/ (system-model build_proposed_intent).
    root = tmp_path / "repo"
    root.mkdir()
    for bad in ["../escape", "a/b", ".", "..", "", "a\\b"]:
        raised = False
        try:
            capture_mod.capture_intent(
                root,
                intent_id=bad,
                title="T",
                problem="P",
                why="W",
                success_criteria=["first outcome"],
            )
        except Exception:
            raised = True
        assert raised, f"unsafe id {bad!r} must be refused"
    intents_dir = root / ".builder" / "intents"
    leaked = list(intents_dir.glob("*")) if intents_dir.exists() else []
    assert leaked == [], f"unsafe id leaked intent artifacts: {leaked}"


def test_capture_refuses_missing_required_fields_without_writing(tmp_path):
    # A required field that is empty/blank raises before anything lands on disk.
    root = tmp_path / "repo"
    root.mkdir()
    bad_cases = [
        {"title": ""},
        {"problem": "  "},
        {"why": ""},
        {"success_criteria": []},
        {"success_criteria": [""]},
    ]
    for override in bad_cases:
        kwargs = dict(intent_id="idea-bad", title="T", problem="P", why="W", success_criteria=["first outcome"])
        kwargs.update(override)
        raised = False
        try:
            capture_mod.capture_intent(root, **kwargs)
        except Exception:
            raised = True
        assert raised, f"invalid field {override!r} must be refused"
    intents_dir = root / ".builder" / "intents"
    leaked = list(intents_dir.glob("*")) if intents_dir.exists() else []
    assert leaked == [], f"invalid field leaked intent artifacts: {leaked}"


def test_capture_cli_refuses_traversal_id(tmp_path):
    # The CLI surface must refuse a traversal id with a non-zero exit and no write.
    repo = tmp_path / "repo"
    repo.mkdir()
    code, out, err = _run([
        "capture",
        "--root", str(repo),
        "--id", "../escape",
        "--title", "T",
        "--problem", "P",
        "--why", "W",
        "--success", "first outcome",
    ])
    assert code != 0
    assert "capture refused" in err
    assert not (repo / ".builder" / "intents" / "escape" / "intent.yaml").exists()
    assert not (repo.parent / "escape" / "intent.yaml").exists()


# ------------------------------------------------------------------ T2: CLI verb


def test_capture_cli_writes_proposed_and_prints_receipt(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    code, out, err = _run([
        "capture",
        "--root", str(repo),
        "--id", "idea-y",
        "--title", "T",
        "--problem", "P",
        "--why", "W",
        "--success", "first outcome",
    ])
    assert code == 0, err
    assert "intent captured: idea-y -> proposed" in out
    path = repo / ".builder" / "intents" / "idea-y" / "intent.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert data["status"] == "proposed"


def test_capture_cli_needs_no_controlling_tty(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()

    with patch.object(isanna, "_open_controlling_tty", side_effect=OSError("no controlling terminal")):
        code, out, err = _run([
            "capture",
            "--root", str(repo),
            "--id", "idea-z",
            "--title", "T",
            "--problem", "P",
            "--why", "W",
            "--success", "first outcome",
        ])
    assert code == 0, err
    assert "intent captured: idea-z -> proposed" in out


# ------------------------------------------------------------------ T3: /idea skill


def _skill_text() -> str:
    skill = require_repo_asset(
        ROOT, ".claude/skills/idea/SKILL.md", "the /idea skill contract"
    )
    return skill.read_text(encoding="utf-8")


def test_idea_skill_wraps_capture_cli():
    text = _skill_text()
    assert "isanna capture" in text


def test_idea_skill_states_propose_only():
    text = _skill_text()
    lowered = text.lower()
    assert "propose" in lowered
    assert "accept" in lowered and "reject" in lowered and "supersede" in lowered
    assert "human-only" in lowered or "human only" in lowered
