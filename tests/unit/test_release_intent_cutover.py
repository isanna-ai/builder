from _builder_project_model.common import ValidationError
from _builder_project_model.parsers import ProjectManifest, ProjectRepo, parse_release_manifest


def _project() -> ProjectManifest:
    return ProjectManifest(
        product="builder",
        title="Builder",
        description="",
        default_repo="builder",
        repos=[ProjectRepo(alias="builder", repo_id="builder")],
        backlog=[],
        releases=[],
    )


def test_live_release_requires_non_empty_intents(tmp_path):
    path = tmp_path / "demo.yaml"
    path.write_text("schema_version: 1\nname: demo\nstatus: draft\nintents: []\n", encoding="utf-8")
    try:
        parse_release_manifest(path, project=_project())
    except ValidationError as exc:
        assert "intents must be a non-empty list" in str(exc)
    else:
        raise AssertionError("expected ValidationError")


def test_historical_release_rejects_intents(tmp_path):
    path = tmp_path / "demo.yaml"
    path.write_text("schema_version: 1\nname: demo\nstatus: shipped\nintents:\n  - a\n", encoding="utf-8")
    try:
        parse_release_manifest(path, project=_project())
    except ValidationError as exc:
        assert "historical releases must not declare intents" in str(exc)
    else:
        raise AssertionError("expected ValidationError")
