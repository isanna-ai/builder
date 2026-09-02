"""Archiving a spec used to break every release that named it.

`/isanna-archive` MOVES `.builder/specs/<id>/` to `.builder/specs/archive/YYYY-MM-DD-<id>/`,
while release membership resolved members only at `.builder/specs/<id>`. The moment a
release-referenced spec was archived, `isanna release lint` reported it as a dangling ref --
so a spec whose own `next_action` read "Run /isanna-archive <name>" could not have that
action performed without breaking the release it belonged to. Two documented workflows
contradicting each other.

Measured on `phase-level-model-routing`: archiving it by the documented procedure turned a
clean `release lint: 6 release(s) clean` into 3 dangling-ref findings. No release in the repo
referenced an archived spec, so the combination had never been exercised.

Resolution order is live-first: an archived copy must never shadow a live spec of the same id.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from _validators.runtime import resolve_spec_dir  # noqa: E402


def _specs(tmp_path: Path) -> Path:
    d = tmp_path / ".builder" / "specs"
    d.mkdir(parents=True)
    return d


def test_a_live_spec_resolves(tmp_path):
    specs = _specs(tmp_path)
    (specs / "demo").mkdir()
    assert resolve_spec_dir(specs, "demo") == specs / "demo"


def test_a_date_prefixed_archived_spec_resolves(tmp_path):
    # THE REGRESSION: this is the shape `/isanna-archive` actually writes.
    specs = _specs(tmp_path)
    archived = specs / "archive" / "2026-07-29-demo"
    archived.mkdir(parents=True)
    assert resolve_spec_dir(specs, "demo") == archived


def test_a_bare_archived_spec_resolves(tmp_path):
    # Older archive entries carry no date prefix; both forms are in the tree today.
    specs = _specs(tmp_path)
    archived = specs / "archive" / "demo"
    archived.mkdir(parents=True)
    assert resolve_spec_dir(specs, "demo") == archived


def test_a_live_spec_wins_over_an_archived_copy(tmp_path):
    # An archived copy must never shadow live work. If a spec id is revived, the live
    # directory is the answer and the archive is history.
    specs = _specs(tmp_path)
    (specs / "demo").mkdir()
    (specs / "archive" / "2026-07-29-demo").mkdir(parents=True)
    assert resolve_spec_dir(specs, "demo") == specs / "demo"


def test_the_newest_archive_wins_when_a_spec_was_archived_twice(tmp_path):
    # Deterministic rather than filesystem-order dependent.
    specs = _specs(tmp_path)
    (specs / "archive" / "2026-01-02-demo").mkdir(parents=True)
    newest = specs / "archive" / "2026-07-29-demo"
    newest.mkdir(parents=True)
    assert resolve_spec_dir(specs, "demo") == newest


def test_an_unknown_spec_resolves_to_nothing(tmp_path):
    assert resolve_spec_dir(_specs(tmp_path), "nope") is None


def test_a_prefix_collision_does_not_match(tmp_path):
    # `2026-07-29-demo-two` must not satisfy a lookup for `demo`.
    specs = _specs(tmp_path)
    (specs / "archive" / "2026-07-29-demo-two").mkdir(parents=True)
    assert resolve_spec_dir(specs, "demo") is None


def test_a_file_named_like_a_spec_is_not_a_spec_dir(tmp_path):
    specs = _specs(tmp_path)
    (specs / "archive").mkdir()
    (specs / "archive" / "2026-07-29-demo").write_text("not a directory", encoding="utf-8")
    assert resolve_spec_dir(specs, "demo") is None


def test_a_missing_archive_directory_is_not_an_error(tmp_path):
    assert resolve_spec_dir(_specs(tmp_path), "demo") is None


# --- the end-to-end symptom -----------------------------------------------------


def test_release_membership_no_longer_dangles_on_an_archived_spec(tmp_path):
    import planning

    repo = tmp_path / "builder"
    specs = repo / ".builder" / "specs"
    archived = specs / "archive" / "2026-07-29-demo"
    archived.mkdir(parents=True)
    (archived / "spec.yaml").write_text(
        "name: demo\nstatus: archived\ncurrent_phase: archived\nnext_action: \"None\"\n",
        encoding="utf-8")

    registry = planning.Registry(tmp_path, repo)
    ref = planning.SpecRef(alias=None, spec_id="demo", raw="demo")
    spec_dir, err = registry.spec_dir(ref)
    assert err is None, err
    assert spec_dir is not None and spec_dir.is_dir()
    assert spec_dir.name == "2026-07-29-demo"
