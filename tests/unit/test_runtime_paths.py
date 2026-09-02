from pathlib import Path


from _dispatch_runtime.paths import runtime_dir
import planning


def test_runtime_dir_uses_builder_when_only_specpilot_exists(tmp_path: Path) -> None:
    (tmp_path / ".specpilot").mkdir()

    assert runtime_dir(tmp_path) == tmp_path / ".builder"


def test_runtime_dir_prefers_builder_when_it_exists(tmp_path: Path) -> None:
    (tmp_path / ".builder").mkdir()

    assert runtime_dir(tmp_path) == tmp_path / ".builder"


def test_runtime_dir_prefers_builder_when_both_candidates_exist(tmp_path: Path) -> None:
    (tmp_path / ".builder").mkdir()
    (tmp_path / ".specpilot").mkdir()

    assert runtime_dir(tmp_path) == tmp_path / ".builder"


def test_runtime_dir_uses_builder_when_neither_candidate_exists(tmp_path: Path) -> None:
    assert runtime_dir(tmp_path) == tmp_path / ".builder"


def test_load_releases_reads_builder_runtime_directory(tmp_path: Path) -> None:
    releases = tmp_path / ".builder" / "releases"
    releases.mkdir(parents=True)
    (releases / "builder-release.yaml").write_text(
        "release: builder-release\nproduct: demo\ntitle: Builder release\ngoal: compatibility\nstatus: active\nspecs: []\n",
        encoding="utf-8",
    )

    loaded = planning.load_releases(tmp_path)

    assert [release.release_id for release in loaded] == ["builder-release"]
