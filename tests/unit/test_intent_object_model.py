from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from _validators.common import load_schema, validate_schema
from _yaml import yaml
import _yaml_compat

planning_spec = importlib.util.spec_from_file_location("planning_intent_model", SCRIPTS / "planning.py")
planning = importlib.util.module_from_spec(planning_spec)
sys.modules["planning_intent_model"] = planning
planning_spec.loader.exec_module(planning)

intent_spec = importlib.util.spec_from_file_location("intent_model_under_test", SCRIPTS / "_intent_model.py")
intent_model = importlib.util.module_from_spec(intent_spec)
sys.modules["intent_model_under_test"] = intent_model
intent_spec.loader.exec_module(intent_model)


def _intent_file(tmp_path: Path, intent_id: str = "ship-search", extra: str = "") -> Path:
    repo = tmp_path / "repo"
    path = repo / ".builder" / "intents" / intent_id / "intent.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(
        "artifact: intent-object\n"
        f"intent: {intent_id}\n"
        "title: Search ships\n"
        "status: proposed\n"
        "problem: Search is absent\n"
        "why: Operators cannot find work\n"
        "success_criteria:\n"
        "  - id: SC-1\n"
        "    statement: Search returns relevant intents\n"
        "non_goals:\n"
        "  - Replace release membership\n"
        "ssot_delta:\n"
        "  capabilities:\n"
        "    - target: capability.search\n"
        "      change: create\n"
        "  behaviors:\n"
        "    - target: behavior.search-results\n"
        "      change: enrich\n"
        "  journeys:\n"
        "    - target: journey.find-intent\n"
        "      change: rewire\n"
        "specs:\n"
        "  - search-spec\n"
        + extra,
        encoding="utf-8",
    )
    return path


def test_loads_strict_intent_object(tmp_path):
    path = _intent_file(tmp_path)
    intent = intent_model.load_intent_object(path, path.parents[3], planning.parse_spec_ref)
    assert intent.intent == "ship-search"
    assert intent.success_criteria[0].id == "SC-1"
    assert intent.ssot_delta["capabilities"][0].target == "capability.search"


def test_rejects_directory_id_mismatch_and_nested_unknown_keys(tmp_path):
    path = _intent_file(tmp_path, extra="rogue: true\n")
    path.write_text(path.read_text(encoding="utf-8").replace("intent: ship-search", "intent: not-the-dir"), encoding="utf-8")
    try:
        intent_model.load_intent_object(path, path.parents[3], planning.parse_spec_ref)
    except ValueError as exc:
        text = str(exc)
    else:
        raise AssertionError("expected strict parse failure")
    assert "does not match parent directory" in text or "unknown key" in text


def test_rejects_duplicate_criteria_delta_and_member_refs(tmp_path):
    path = _intent_file(
        tmp_path,
        extra=(
            "success_criteria:\n"
            "  - id: SC-1\n"
            "    statement: one\n"
            "  - id: SC-1\n"
            "    statement: two\n"
            "non_goals:\n"
            "  - x\n"
            "ssot_delta:\n"
            "  capabilities:\n"
            "    - target: dup\n"
            "      change: create\n"
            "    - target: dup\n"
            "      change: enrich\n"
            "  behaviors: []\n"
            "  journeys: []\n"
            "specs:\n"
            "  - dup\n"
            "  - dup\n"
        ),
    )
    try:
        intent_model.load_intent_object(path, path.parents[3], planning.parse_spec_ref)
    except ValueError as exc:
        assert "duplicate" in str(exc)
    else:
        raise AssertionError("expected duplicate failure")


def test_rejects_spec_local_intent_yaml(tmp_path):
    path = tmp_path / "repo" / ".builder" / "specs" / "demo" / "intent.yaml"
    path.parent.mkdir(parents=True)
    path.write_text("artifact: intent-object\n", encoding="utf-8")
    try:
        intent_model.load_intent_object(path, path.parents[4], planning.parse_spec_ref)
    except ValueError as exc:
        assert "out of scope" in str(exc)
    else:
        raise AssertionError("expected spec-local rejection")


def test_schema_is_machine_readable_and_strict_at_every_nested_level(tmp_path):
    path = _intent_file(tmp_path)
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    schema, errors = load_schema("intent-object.schema.yaml")
    assert errors == []
    assert validate_schema(data, schema, "intent") == []

    data["success_criteria"][0]["rogue"] = True
    assert "unknown field `rogue`" in "\n".join(validate_schema(data, schema, "intent"))


def _assert_load_rejected(path: Path, pattern: str) -> None:
    try:
        intent_model.load_intent_object(path, path.parents[3], planning.parse_spec_ref)
    except ValueError as exc:
        assert re.search(pattern, str(exc)), str(exc)
    else:
        raise AssertionError("expected strict intent rejection")


def test_rejects_noncanonical_declared_statuses(tmp_path):
    for index, replacement in enumerate(("status: Accepted", "status: fulfilled")):
        path = _intent_file(tmp_path / str(index))
        path.write_text(
            path.read_text(encoding="utf-8").replace("status: proposed", replacement),
            encoding="utf-8",
        )
        _assert_load_rejected(path, "invalid status")


def test_rejects_release_weight_mapping_as_an_intent_member(tmp_path):
    path = _intent_file(tmp_path)
    path.write_text(
        path.read_text(encoding="utf-8").replace("  - search-spec\n", "  - spec: search-spec\n    weight: 2\n"),
        encoding="utf-8",
    )
    _assert_load_rejected(path, r"specs\[0\] must be")


def test_rejects_duplicate_yaml_keys(tmp_path):
    path = _intent_file(tmp_path, extra="status: accepted\n")
    _assert_load_rejected(path, "duplicate YAML key 'status'")


def test_zero_dependency_yaml_fallback_also_rejects_duplicate_keys(tmp_path):
    path = _intent_file(tmp_path, extra="status: accepted\n")
    original_yaml = intent_model.yaml
    intent_model.yaml = _yaml_compat
    try:
        _assert_load_rejected(path, "duplicate YAML key 'status'")
    finally:
        intent_model.yaml = original_yaml


def test_rejects_symlinked_intent_directory_escape(tmp_path):
    repo = tmp_path / "repo"
    outside = tmp_path / "outside" / "escaped"
    outside.mkdir(parents=True)
    real_path = _intent_file(tmp_path / "source")
    (outside / "intent.yaml").write_bytes(real_path.read_bytes())
    (repo / ".builder" / "intents").mkdir(parents=True)
    (repo / ".builder" / "intents" / "ship-search").symlink_to(outside, target_is_directory=True)
    escaped = repo / ".builder" / "intents" / "ship-search" / "intent.yaml"
    try:
        intent_model.load_intent_object(escaped, repo, planning.parse_spec_ref)
    except ValueError as exc:
        assert "symlinked intent path refused" in str(exc)
    else:
        raise AssertionError("expected symlinked parent rejection")
