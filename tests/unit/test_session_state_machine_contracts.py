from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
FIXTURES = ROOT / "tests" / "fixtures" / "builder_project_model" / "sessions" / "v1"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from _builder_project_model.common import ValidationError
from _builder_project_model.session_schema import parse_session_record


def _path(name: str) -> Path:
    return FIXTURES / name


def test_session_schema_accepts_locked_forward_lifecycle_states():
    assert parse_session_record(_path("session-starting.json")).data["state"] == "starting"
    assert parse_session_record(_path("session-active.json")).data["state"] == "active"
    assert parse_session_record(_path("session-reaping.json")).data["state"] == "reaping"
    assert parse_session_record(_path("session-closed.json")).data["state"] == "closed"


def test_session_schema_requires_supported_schema_version(tmp_path):
    source = _path("session-starting.json").read_text(encoding="utf-8")
    for label, mutated in (
        ("missing", source.replace('  "schema_version": 1,\n', "")),
        ("unsupported", source.replace('"schema_version": 1', '"schema_version": 2')),
    ):
        path = tmp_path / f"session-{label}.json"
        path.write_text(mutated, encoding="utf-8")
        try:
            parse_session_record(path)
        except ValidationError as exc:
            assert any("unsupported schema_version" in issue.render() for issue in exc.issues)
        else:
            raise AssertionError(f"expected {label} schema_version rejection")


def test_session_schema_rejects_invalid_transition_and_requires_pgid_after_starting():
    try:
        parse_session_record(_path("session-bad-transition.json"))
    except ValidationError as exc:
        assert any("invalid transition 'starting' -> 'reaping'" in issue.render() for issue in exc.issues)
    else:
        raise AssertionError("expected invalid-transition rejection")

    bad = _path("session-active.json").read_text(encoding="utf-8").replace('"pgid": 1235', '"pgid": null')
    temp = _path("session-active.json").parent / "_tmp-active.json"
    temp.write_text(bad, encoding="utf-8")
    try:
        parse_session_record(temp)
    except ValidationError as exc:
        assert any("non-starting session must have pgid > 1" in issue.render() for issue in exc.issues)
    else:
        raise AssertionError("expected pgid requirement")
    finally:
        temp.unlink(missing_ok=True)
