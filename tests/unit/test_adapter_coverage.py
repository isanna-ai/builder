"""Prove an adapter covers a repo's real change surface BEFORE enabling sync there.

Curating an adapter is the load-bearing half of the SSOT backfill, and until now there was no
way to check the result except by running a sync and reading the divergence. That is a bad
feedback loop: the failure arrives at the end of a spec's lifecycle, phrased as "this spec
diverged", when the actual fault is a hole in a repo-level file.

The rule being checked comes from `_sync/adapter.py:observed_tuples`: any changed path matching
NO mapping becomes `{capabilities, unmapped:<path>, enrich}`, which the spec never declared, so
sync flags it as divergence. An incomplete adapter therefore BLOCKS sync rather than weakening
it — which is why coverage is worth proving up front and against REAL paths, not invented ones.

Measured on the one fully curated repo available: 18 mappings, 219 real changed
paths over its last 15 commits, 0 unmapped.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from _ssot_audit import adapter_coverage  # noqa: E402


def _repo(tmp_path: Path, mappings: str) -> Path:
    repo = tmp_path / "repo"
    (repo / ".builder").mkdir(parents=True)
    (repo / ".builder" / "sync-adapter.yaml").write_text(
        f"artifact: sync-adapter\nmappings:\n{mappings}", encoding="utf-8")
    return repo


_SRC_MAPPING = """  - paths: ["src/*"]
    tuples:
      - category: capabilities
        target: demo
        change: enrich
"""


# --- runtime-dir paths are not part of the change surface ----------------------
#
# Sync's changed_paths come from `_git_source_paths` (lane_common.py), which drops any path
# under a runtime dir:
#
#     if name and not any(name.startswith(f"{r}/") for r in RUNTIME_DIR_NAMES)
#
# adapter_coverage was fed raw `git diff --name-only` output, so it counted spec artifacts,
# intents and the published model as unmapped paths — paths sync will never see. Every number
# it produced was inflated by spec-artifact churn. Measured on builder: 242 raw paths vs 205
# real source paths, 222 reported unmapped vs 185 real. That inflation also underwrote the
# result that was used to conclude the curation recipe was sound.
#
# The filter must reuse RUNTIME_DIR_NAMES rather than restate ".builder/" — a second copy
# would drift from the very filter it exists to mirror.


def test_a_builder_runtime_path_is_not_counted(tmp_path):
    repo = _repo(tmp_path, _SRC_MAPPING)
    r = adapter_coverage(repo, ["src/a.py", ".builder/specs/demo/spec.yaml"])
    assert r.unmapped == []
    assert r.checked_paths == 1


def test_a_specpilot_runtime_path_is_not_counted(tmp_path):
    # The legacy runtime dir name is still in RUNTIME_DIR_NAMES and still filtered by sync.
    repo = _repo(tmp_path, _SRC_MAPPING)
    r = adapter_coverage(repo, ["src/a.py", ".specpilot/specs/demo/tasks.yaml"])
    assert r.unmapped == [] and r.checked_paths == 1


def test_a_normal_source_path_is_still_counted(tmp_path):
    repo = _repo(tmp_path, _SRC_MAPPING)
    r = adapter_coverage(repo, ["docs/whatever.md"])
    assert r.unmapped == ["docs/whatever.md"] and r.checked_paths == 1


def test_an_all_runtime_path_set_is_vacuous_not_covered(tmp_path):
    # Filtering must happen BEFORE the vacuous check. A commit that touched only spec
    # artifacts leaves nothing to check, and reporting that as "covered" is the unearned green
    # this tool exists to refuse.
    repo = _repo(tmp_path, _SRC_MAPPING)
    r = adapter_coverage(repo, [".builder/specs/demo/spec.yaml", ".builder/intents/x/intent.yaml"])
    assert r.vacuous is True and r.covered is False and r.checked_paths == 0


def test_a_path_merely_containing_the_runtime_name_is_kept(tmp_path):
    # Only a leading `<runtime>/` segment is a runtime path. `src/.builder-notes.md` is source.
    repo = _repo(tmp_path, _SRC_MAPPING)
    r = adapter_coverage(repo, ["src/.builder-notes.md", "my.builder/x.py"])
    assert r.checked_paths == 2


def test_the_filter_reuses_the_shared_constant(tmp_path):
    # Two copies of the runtime-dir list would drift from the filter this mirrors.
    import _ssot_audit
    from _validators.runtime import RUNTIME_DIR_NAMES

    assert _ssot_audit.RUNTIME_DIR_NAMES is RUNTIME_DIR_NAMES


def test_full_coverage_reports_no_gaps(tmp_path):
    repo = _repo(tmp_path, _SRC_MAPPING)
    r = adapter_coverage(repo, ["src/a.py", "src/b.py"])
    assert r.unmapped == [] and r.covered is True
    assert r.observed_targets == ["demo"]


def test_an_uncovered_path_is_reported(tmp_path):
    repo = _repo(tmp_path, _SRC_MAPPING)
    r = adapter_coverage(repo, ["src/a.py", "README.md", "Makefile"])
    assert r.covered is False
    assert r.unmapped == ["Makefile", "README.md"]


def test_unmapped_paths_are_sorted_for_deterministic_output(tmp_path):
    repo = _repo(tmp_path, _SRC_MAPPING)
    r = adapter_coverage(repo, ["z.txt", "a.txt", "m.txt"])
    assert r.unmapped == ["a.txt", "m.txt", "z.txt"]


def test_a_no_claim_mapping_still_counts_as_covered(tmp_path):
    # `tuples: []` is the documented "recognized, makes no claim" pattern for shared entry
    # points and root chrome. It MUST satisfy coverage -- otherwise the only way to cover a
    # shared path is to invent an SSOT claim about it, which is how stale tuples get written.
    repo = _repo(tmp_path, '  - paths: ["Makefile"]\n    tuples: []\n')
    r = adapter_coverage(repo, ["Makefile"])
    assert r.covered is True and r.unmapped == [] and r.observed_targets == []


def test_a_missing_adapter_is_reported_not_crashed(tmp_path):
    repo = tmp_path / "bare"
    repo.mkdir()
    r = adapter_coverage(repo, ["src/a.py"])
    assert r.adapter_present is False and r.covered is False


def test_a_malformed_adapter_is_reported_not_crashed(tmp_path):
    repo = tmp_path / "bad"
    (repo / ".builder").mkdir(parents=True)
    (repo / ".builder" / "sync-adapter.yaml").write_text("artifact: nonsense\n", encoding="utf-8")
    r = adapter_coverage(repo, ["src/a.py"])
    assert r.adapter_present is False and r.covered is False


def test_no_changed_paths_is_vacuous_not_a_pass(tmp_path):
    # Zero paths would trivially yield zero unmapped. Reporting that as "covered" is the
    # unearned green this project exists to refuse: nothing was checked.
    repo = _repo(tmp_path, _SRC_MAPPING)
    r = adapter_coverage(repo, [])
    assert r.covered is False and r.vacuous is True


def test_glob_crosses_directory_separators(tmp_path):
    # fnmatch semantics: `*` crosses `/`. Authors rely on this, so pin it.
    repo = _repo(tmp_path, _SRC_MAPPING)
    r = adapter_coverage(repo, ["src/deep/nested/a.py"])
    assert r.covered is True
