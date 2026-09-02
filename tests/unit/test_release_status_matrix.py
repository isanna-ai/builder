from pathlib import Path

from _builder_project_model.common import release_membership_field
from _yaml import yaml
import planning


def test_release_status_matrix_is_explicit():
    assert release_membership_field("draft") == "intents"
    assert release_membership_field("active") == "intents"
    assert release_membership_field("shipped") == "specs"
    assert release_membership_field("cancelled") == "specs"
    assert release_membership_field("archived") == "specs"
    assert release_membership_field("abandoned") == "specs"


def test_release_schema_declares_the_same_status_and_membership_matrix():
    root = Path(__file__).resolve().parents[2]
    schema = yaml.safe_load((root / "schemas" / "release.schema.yaml").read_text(encoding="utf-8"))
    assert schema["properties"]["status"]["enum"] == [
        "draft", "active", "shipped", "cancelled", "abandoned", "archived"
    ]
    assert schema["properties"]["intents"]["minItems"] == 1
    assert schema["properties"]["specs"]["minItems"] == 1
    live_rule, historical_rule = schema["allOf"]
    assert live_rule["if"]["properties"]["status"]["enum"] == ["draft", "active"]
    assert live_rule["then"]["required"] == ["intents"]
    assert historical_rule["if"]["properties"]["status"]["enum"] == [
        "shipped", "cancelled", "abandoned", "archived"
    ]
    assert historical_rule["then"]["required"] == ["specs"]


def test_legacy_planning_parser_rejects_historical_intents_by_name(tmp_path):
    path = tmp_path / "history.yaml"
    path.write_text(
        "release: history\nstatus: cancelled\nintents:\n  - should-not-be-here\n",
        encoding="utf-8",
    )
    release = planning.parse_release(path, tmp_path)
    assert any("historical releases must not declare intents" in error for error in release.parse_errors)
