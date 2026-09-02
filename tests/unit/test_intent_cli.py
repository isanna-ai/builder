from __future__ import annotations

import contextlib
import importlib.util
import io
import argparse
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location("isanna_intent_cli", SCRIPTS / "isanna.py")
isanna = importlib.util.module_from_spec(spec)
sys.modules["isanna_intent_cli"] = isanna
spec.loader.exec_module(isanna)


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / ".builder" / "intents" / "i1").mkdir(parents=True)
    (repo / ".builder" / "intents" / "i1" / "intent.yaml").write_text(
        "artifact: intent-object\nintent: i1\ntitle: t\nstatus: proposed\nproblem: p\nwhy: w\n"
        "success_criteria:\n  - id: SC-1\n    statement: s\nnon_goals:\n  - n\n"
        "ssot_delta:\n  capabilities: []\n  behaviors: []\n  journeys: []\nspecs: []\n",
        encoding="utf-8",
    )
    return repo


def _run(argv):
    out = io.StringIO()
    err = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = isanna.main(argv)
    return code, out.getvalue(), err.getvalue()


def _run_intent(args):
    out = io.StringIO()
    err = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = isanna.cmd_intent(args)
    return code, out.getvalue(), err.getvalue()


class _FakeControllingTTY:
    def __init__(self, response: str, *, is_tty: bool = True):
        self.response = response
        self.is_tty = is_tty
        self.closed = False

    def isatty(self):
        return self.is_tty

    def write(self, _text):
        return None

    def flush(self):
        return None

    def readline(self):
        return self.response + "\n"

    def close(self):
        self.closed = True


def test_intent_cli_requires_controlling_tty(tmp_path):
    repo = _repo(tmp_path)
    args = argparse.Namespace(intent_verb="accept", intent_id="i1", root=str(repo), reason=None, superseded_by=None)
    with patch.object(isanna, "_open_controlling_tty", side_effect=OSError("no controlling terminal")):
        code, _, err = _run_intent(args)
    assert code == 2 and "TTY required" in err


def test_intent_cli_requires_exact_confirmation_and_preserves_bytes_on_refusal(tmp_path):
    repo = _repo(tmp_path)
    path = repo / ".builder" / "intents" / "i1" / "intent.yaml"
    original = path.read_bytes()
    args = argparse.Namespace(intent_verb="accept", intent_id="i1", root=str(repo), reason=None, superseded_by=None)
    with patch.object(isanna, "_open_controlling_tty", return_value=_FakeControllingTTY("nope")):
        code, _, err = _run_intent(args)
    assert code == 2
    assert "confirmation mismatch" in err
    assert path.read_bytes() == original


def test_intent_cli_accepts_and_reject_requires_reason(tmp_path):
    repo = _repo(tmp_path)
    accept = argparse.Namespace(intent_verb="accept", intent_id="i1", root=str(repo), reason=None, superseded_by=None)
    with patch.object(isanna, "_open_controlling_tty", return_value=_FakeControllingTTY("i1 accepted")):
        code, out, err = _run_intent(accept)
    assert code == 0, err
    assert "intent updated" in out
    reject = argparse.Namespace(intent_verb="reject", intent_id="i1", root=str(repo), reason=None, superseded_by=None)
    with patch.object(isanna, "_open_controlling_tty", return_value=_FakeControllingTTY("i1 rejected")):
        code, _, err = _run_intent(reject)
    assert code == 2 and "--reason is required" in err


def test_intent_cli_rejects_and_supersedes_with_terminal_metadata(tmp_path):
    repo = _repo(tmp_path)
    reject = argparse.Namespace(intent_verb="reject", intent_id="i1", root=str(repo), reason="not now", superseded_by=None)
    with patch.object(isanna, "_open_controlling_tty", return_value=_FakeControllingTTY("i1 rejected")):
        code, _, err = _run_intent(reject)
    assert code == 0, err
    text = (repo / ".builder" / "intents" / "i1" / "intent.yaml").read_text(encoding="utf-8")
    assert "status: rejected" in text and "reason: not now" in text

    repo = _repo(tmp_path / "second")
    supersede = argparse.Namespace(
        intent_verb="supersede",
        intent_id="i1",
        root=str(repo),
        reason="better intent",
        superseded_by="i2",
    )
    with patch.object(isanna, "_open_controlling_tty", return_value=_FakeControllingTTY("i1 superseded")):
        code, _, err = _run_intent(supersede)
    assert code == 0, err
    text = (repo / ".builder" / "intents" / "i1" / "intent.yaml").read_text(encoding="utf-8")
    assert "status: superseded" in text and "superseded_by: i2" in text


def test_terminal_state_refusal_is_byte_preserving(tmp_path):
    repo = _repo(tmp_path)
    path = repo / ".builder" / "intents" / "i1" / "intent.yaml"
    path.write_text(path.read_text(encoding="utf-8").replace("status: proposed", "status: rejected") + "reason: final\n", encoding="utf-8")
    original = path.read_bytes()
    args = argparse.Namespace(intent_verb="supersede", intent_id="i1", root=str(repo), reason="change", superseded_by=None)
    with patch.object(isanna, "_open_controlling_tty", return_value=_FakeControllingTTY("i1 superseded")):
        code, _, err = _run_intent(args)
    assert code == 2 and "terminal transitions" in err
    assert path.read_bytes() == original


def test_computed_fulfilled_state_is_immutable_and_byte_preserving(tmp_path):
    repo = _repo(tmp_path)
    path = repo / ".builder" / "intents" / "i1" / "intent.yaml"
    path.write_text(
        path.read_text(encoding="utf-8").replace("status: proposed", "status: accepted").replace("specs: []", "specs:\n  - a"),
        encoding="utf-8",
    )
    original = path.read_bytes()
    member = type(
        "Member",
        (),
        {"finding": None, "status": "synced", "verification": "host-verified", "canonical_ref": "a"},
    )()
    args = argparse.Namespace(intent_verb="reject", intent_id="i1", root=str(repo), reason="too late", superseded_by=None)
    with patch.object(isanna, "_open_controlling_tty", return_value=_FakeControllingTTY("i1 rejected")), patch.object(
        isanna, "_intent_members", return_value=[member]
    ):
        code, _, err = _run_intent(args)
    assert code == 2 and "fulfilled intents are immutable" in err
    assert path.read_bytes() == original


def test_invalid_superseded_by_is_validated_before_atomic_replace(tmp_path):
    repo = _repo(tmp_path)
    path = repo / ".builder" / "intents" / "i1" / "intent.yaml"
    original = path.read_bytes()
    args = argparse.Namespace(intent_verb="supersede", intent_id="i1", root=str(repo), reason="change", superseded_by="   ")
    with patch.object(isanna, "_open_controlling_tty", return_value=_FakeControllingTTY("i1 superseded")), patch.object(
        isanna, "atomic_write_bytes", side_effect=AssertionError("must not write")
    ):
        code, _, err = _run_intent(args)
    assert code == 2 and "must be non-empty" in err
    assert path.read_bytes() == original


def test_symlinked_parent_escape_is_refused_without_touching_target(tmp_path):
    repo = tmp_path / "repo"
    outside = tmp_path / "outside" / "i1"
    outside.mkdir(parents=True)
    source_repo = _repo(tmp_path / "source")
    source = source_repo / ".builder" / "intents" / "i1" / "intent.yaml"
    target = outside / "intent.yaml"
    target.write_bytes(source.read_bytes())
    (repo / ".builder" / "intents").mkdir(parents=True)
    (repo / ".builder" / "intents" / "i1").symlink_to(outside, target_is_directory=True)
    original = target.read_bytes()
    args = argparse.Namespace(intent_verb="accept", intent_id="i1", root=str(repo), reason=None, superseded_by=None)
    with patch.object(isanna, "_open_controlling_tty", return_value=_FakeControllingTTY("i1 accepted")):
        code, _, err = _run_intent(args)
    assert code == 2 and "symlinked intent path refused" in err
    assert target.read_bytes() == original


def test_missing_intent_is_a_deterministic_refusal(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    args = argparse.Namespace(intent_verb="accept", intent_id="missing", root=str(repo), reason=None, superseded_by=None)
    code, _, err = _run_intent(args)
    assert code == 2 and "intent refused:" in err and "No such file" in err
