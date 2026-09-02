"""SSOT readiness, measured per repo instead of asserted.

Builder introduced the SSOT/sync layer after several repos were already brownfield, so
those repos were never bootstrapped. Observed: none of the active specs across
any repo in a real portfolio had EVER synced, none carried
`.builder/sync-adapter.yaml`, `docs/system-behaviors.yaml`, or a published model. Nobody
noticed, because nothing reported it -- `isanna sync` fails closed per spec with
`bootstrap_required`, which only shows up if you happen to run it.

This audit is the measurement that backfill gets tracked against. It answers, per repo:
can this repo sync at all, and how many specs finished without ever syncing?

`sync_blocked` is the load-bearing field. It mirrors builder's own precondition exactly --
`bootstrap_required` fires when the adapter is missing OR `docs/system-behaviors.yaml` is
absent -- so the audit cannot drift from the behaviour it reports on. A published model is
tracked separately: its absence is a real gap but is not what blocks sync.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from _ssot_audit import audit_repo  # noqa: E402


def _repo(tmp_path: Path, name: str = "demo") -> Path:
    repo = tmp_path / name
    (repo / ".builder" / "specs").mkdir(parents=True)
    return repo


def _spec(repo: Path, spec_id: str, *, status: str, delta: bool = False, synced: bool = False):
    d = repo / ".builder" / "specs" / spec_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "spec.yaml").write_text(
        f"name: {spec_id}\nstatus: {status}\ncurrent_phase: x\nnext_action: none\n", encoding="utf-8")
    if delta:
        (d / "ssot-delta.yaml").write_text("capabilities: []\nbehaviors: []\njourneys: []\n", encoding="utf-8")
    if synced:
        (d / "sync-result.yaml").write_text("artifact: sync-result\nresult: synced\n", encoding="utf-8")
    return d


def _bootstrap(repo: Path, *, adapter=True, behaviors=True, model=True):
    if adapter:
        (repo / ".builder" / "sync-adapter.yaml").write_text(
            "artifact: sync-adapter\nmappings: []\n", encoding="utf-8")
    if behaviors:
        (repo / "docs").mkdir(exist_ok=True)
        (repo / "docs" / "system-behaviors.yaml").write_text(
            "schema: system-behaviors/v1\nbehaviors: []\n", encoding="utf-8")
    if model:
        (repo / ".builder" / "model").mkdir(parents=True, exist_ok=True)
        (repo / ".builder" / "model" / "system-model.yaml").write_text("version: 1\n", encoding="utf-8")


# --- presence is not coverage --------------------------------------------------
#
# Measured on a real portfolio: one repo reported STATE `ok` and BEHAVIORS `True` on a
# docs/system-behaviors.yaml holding FIVE entries against a 78,401-line repo -- roughly 6%
# covered, and honestly self-described in the file as "Wave 1". Two other repos showed 78 and
# 106. All three rendered identically as `True`, so the column a reader uses to judge "is this
# repo's SSOT real" could not distinguish a finished curation from a first slice.
#
# The fix is deliberately NOT to make sync_blocked stricter. `isanna sync` decides
# bootstrap_required on `not ssot.is_file()` (isanna.py) -- presence, nothing more -- and this
# field's docstring promises it MIRRORS that rather than restating it. A stricter audit would
# report BLOCKED for a repo where sync would actually proceed: the same lie, pointing the other
# way. The boolean is replaced by a count, which cannot mislead, and no threshold is invented:
# there is no defensible "enough behaviors per line", and a made-up one firing wrongly would be
# its own unearned alarm.
#
# The one case that IS objectively a stub -- a file present but declaring nothing -- is flagged,
# because that unblocks sync while asserting literally nothing about the system.


def _behaviors(repo: Path, count: int) -> None:
    (repo / "docs").mkdir(exist_ok=True)
    body = "schema: system-behaviors/v1\nbehaviors:\n"
    for i in range(count):
        body += f"  - id: b{i}\n    area: a{i}\n    behavior: does a thing\n"
    if count == 0:
        body = "schema: system-behaviors/v1\nbehaviors: []\n"
    (repo / "docs" / "system-behaviors.yaml").write_text(body, encoding="utf-8")


def test_the_audit_counts_behaviors_rather_than_only_noticing_the_file(tmp_path):
    repo = _repo(tmp_path)
    _bootstrap(repo, behaviors=False)
    _behaviors(repo, 5)
    assert audit_repo(repo).behavior_count == 5


def test_a_missing_behaviors_file_counts_zero(tmp_path):
    repo = _repo(tmp_path)
    _bootstrap(repo, behaviors=False)
    a = audit_repo(repo)
    assert a.behavior_count == 0 and a.has_behaviors is False


def test_a_present_but_empty_behaviors_file_is_flagged_as_asserting_nothing(tmp_path):
    # This is the only objectively-stub case, and it is the dangerous one: it satisfies sync's
    # presence check and unblocks the repo while declaring no behaviour at all.
    repo = _repo(tmp_path)
    _bootstrap(repo, behaviors=False)
    _behaviors(repo, 0)
    a = audit_repo(repo)
    assert a.has_behaviors is True and a.behavior_count == 0
    assert a.behaviors_empty is True


def test_a_populated_behaviors_file_is_not_flagged_empty(tmp_path):
    repo = _repo(tmp_path)
    _bootstrap(repo, behaviors=False)
    _behaviors(repo, 3)
    assert audit_repo(repo).behaviors_empty is False


def test_an_absent_behaviors_file_is_not_called_empty(tmp_path):
    # "absent" and "present but says nothing" need different fixes -- write one, or finish one.
    repo = _repo(tmp_path)
    _bootstrap(repo, behaviors=False)
    assert audit_repo(repo).behaviors_empty is False


def test_an_unparseable_behaviors_file_counts_zero_without_raising(tmp_path):
    repo = _repo(tmp_path)
    _bootstrap(repo, behaviors=False)
    (repo / "docs").mkdir(exist_ok=True)
    (repo / "docs" / "system-behaviors.yaml").write_text("behaviors: [unclosed\n", encoding="utf-8")
    a = audit_repo(repo)
    assert a.behavior_count == 0 and a.has_behaviors is True


def test_counting_behaviors_does_not_make_sync_blocked_stricter(tmp_path):
    # THE load-bearing guard. sync_blocked mirrors `isanna sync`, which checks presence only
    # (`not ssot.is_file()`). A five-entry file and a hundred-entry file are both UNBLOCKED,
    # because that is what the tool being mirrored actually does. Making the audit stricter here
    # would report BLOCKED for a repo where sync proceeds -- the same lie, pointing the other way.
    repo = _repo(tmp_path)
    _bootstrap(repo, behaviors=False)
    _behaviors(repo, 1)
    assert audit_repo(repo).sync_blocked is False
    _behaviors(repo, 0)
    assert audit_repo(repo).sync_blocked is False


# --- blindness ----------------------------------------------------------------
#
# Observed on a real repo: while one spec's `spec.yaml` was unreadable (a filesystem that
# intermittently failed reads -- the
# directory listed, the file stat'd through `find`, and `open()` returned ENOENT), `isanna
# ssot audit` did not error. It reported 25 specs instead of 26 and dropped the spec from
# EVERY bucket. The census silently shrank.
#
# That is the exact failure this audit exists to refuse, turned inward. The tool's own
# docstrings say a repo that cannot be read is reported rather than crashed, and that
# blindness is never success -- but an unreadable SPEC was neither reported nor counted, so
# the totals read as complete when they were not. A number that quietly gets smaller is worse
# than an error, because nothing prompts anyone to look.
#
# A directory is judged a SPEC by whether it carries canonical spec artifacts, not by whether
# spec.yaml happens to be readable at this instant. Dirs with no spec shape at all are still
# skipped -- calling every stray directory blind would be its own false alarm.


def _blind_spec(repo: Path, spec_id: str, *, artifacts=("tasks.yaml", "phase-log.yaml")):
    """A directory that is unmistakably a spec but whose spec.yaml cannot be read."""
    d = repo / ".builder" / "specs" / spec_id
    d.mkdir(parents=True, exist_ok=True)
    for name in artifacts:
        (d / name).write_text("artifact: x\n", encoding="utf-8")
    return d


def test_a_spec_whose_identity_cannot_be_read_is_reported_blind_not_dropped(tmp_path):
    repo = _repo(tmp_path)
    _bootstrap(repo)
    _spec(repo, "readable", status="verified", synced=True)
    _blind_spec(repo, "unreadable")
    a = audit_repo(repo)
    assert a.blind == ["unreadable"]


def test_a_blind_spec_still_counts_toward_the_census(tmp_path):
    # The bug's actual symptom: the total silently shrank. A spec that exists but cannot be
    # read is still a spec -- omitting it makes the repo look smaller than it is.
    repo = _repo(tmp_path)
    _bootstrap(repo)
    _spec(repo, "readable", status="verified", synced=True)
    _blind_spec(repo, "unreadable")
    assert audit_repo(repo).spec_count == 2


def test_a_blind_spec_is_never_placed_in_a_sync_bucket(tmp_path):
    # Guessing is worse than admitting blindness: an unreadable spec has no known status, so
    # it cannot be called finished, historical or actionable without inventing evidence.
    repo = _repo(tmp_path)
    _bootstrap(repo)
    _blind_spec(repo, "unreadable")
    a = audit_repo(repo)
    assert a.finished_never_synced == []
    assert a.unsynced_actionable == [] and a.historical_no_provenance == []


def test_a_directory_with_no_spec_shape_is_skipped_rather_than_called_blind(tmp_path):
    # False blindness would be its own unearned alarm, and an audit that cries wolf is
    # ignored exactly when it finally reports something real.
    repo = _repo(tmp_path)
    _bootstrap(repo)
    stray = repo / ".builder" / "specs" / "notes"
    stray.mkdir(parents=True)
    (stray / "README.md").write_text("scratch\n", encoding="utf-8")
    a = audit_repo(repo)
    assert a.blind == [] and a.spec_count == 0


def test_a_spec_yaml_that_exists_but_cannot_be_opened_is_blind(tmp_path):
    # The other half of the live bug. `is_file()` succeeded on the stale dentry in some
    # probes and failed in others, so readability -- not stat -- has to be the test.
    repo = _repo(tmp_path)
    _bootstrap(repo)
    d = _spec(repo, "poisoned", status="verified")
    (d / "spec.yaml").chmod(0o000)
    try:
        a = audit_repo(repo)
    finally:
        (d / "spec.yaml").chmod(0o644)
    assert a.blind == ["poisoned"] and a.spec_count == 1


def test_a_spec_yaml_with_no_status_line_is_blind_not_silently_unfinished(tmp_path):
    # `_declared_status` returned "" for BOTH "unreadable" and "no status key", and "" simply
    # failed the FINISHED_STATUSES test -- so a spec with no declared status vanished from
    # every bucket while still being counted. Same silence, different cause.
    repo = _repo(tmp_path)
    _bootstrap(repo)
    d = repo / ".builder" / "specs" / "statusless"
    d.mkdir(parents=True)
    (d / "spec.yaml").write_text("name: statusless\ncurrent_phase: x\n", encoding="utf-8")
    a = audit_repo(repo)
    assert a.blind == ["statusless"] and a.spec_count == 1


# --- worktrees ----------------------------------------------------------------
#
# A git WORKTREE is not a repo, and it carries its main checkout's `.builder/specs` tree, so
# `isanna ssot audit --projects-root <dir>` counting both double-counts every spec in it. That
# is how this census once overstated a fleet total by 20%. A backlog number that overstates
# itself gets discounted by whoever reads it, and then the real number is not believed either.
#
# Worktrees are excluded from the fleet census and REPORTED rather than silently dropped:
# silently skipping is the same class of bug as the blindness above, just in the other
# direction. Auditing one explicitly with --root still works -- that is a deliberate act.


def _worktree(base: Path, name: str, parent_name: str) -> Path:
    """A git worktree: `.git` is a FILE containing a gitdir pointer, not a directory."""
    wt = base / name
    (wt / ".builder" / "specs").mkdir(parents=True)
    (wt / ".git").write_text(
        f"gitdir: {base / parent_name}/.git/worktrees/{name}\n", encoding="utf-8")
    return wt


def _clone(base: Path, name: str) -> Path:
    repo = base / name
    (repo / ".builder" / "specs").mkdir(parents=True)
    (repo / ".git").mkdir()
    return repo


def test_a_worktree_is_recognized_by_its_gitdir_pointer(tmp_path):
    from _ssot_audit import worktree_parent

    wt = _worktree(tmp_path, "audit-copy", "main-repo")
    assert worktree_parent(wt) == tmp_path / "main-repo"


def test_a_normal_clone_is_not_a_worktree(tmp_path):
    from _ssot_audit import worktree_parent

    assert worktree_parent(_clone(tmp_path, "main-repo")) is None


def test_an_unreadable_or_malformed_git_file_is_not_guessed_to_be_a_worktree(tmp_path):
    # Never raise inside an audit, and never invent a parent: a `.git` file we cannot parse is
    # reported as an ordinary repo rather than silently attached to some other repo's totals.
    from _ssot_audit import worktree_parent

    repo = tmp_path / "odd"
    (repo / ".builder" / "specs").mkdir(parents=True)
    (repo / ".git").write_text("this is not a gitdir pointer\n", encoding="utf-8")
    assert worktree_parent(repo) is None


def test_the_fleet_census_excludes_worktrees_and_names_them(tmp_path):
    from _ssot_audit import enumerate_fleet

    _clone(tmp_path, "main-repo")
    _worktree(tmp_path, "audit-copy", "main-repo")
    roots, skipped = enumerate_fleet(tmp_path)
    assert [r.name for r in roots] == ["main-repo"]
    assert [(s.name, s.parent, s.parent_audited) for s in skipped] == [
        ("audit-copy", "main-repo", True)]


def test_a_worktree_whose_parent_is_not_audited_is_flagged(tmp_path):
    # The one case where skipping actually loses coverage: if the main checkout is not under
    # the projects root, dropping the worktree drops its specs from the fleet entirely. Say so
    # rather than quietly shrinking the census -- that is the bug we just fixed, reintroduced.
    from _ssot_audit import enumerate_fleet

    _worktree(tmp_path, "audit-copy", "elsewhere")
    roots, skipped = enumerate_fleet(tmp_path)
    assert roots == []
    assert [(s.name, s.parent_audited) for s in skipped] == [("audit-copy", False)]


def test_the_fleet_census_still_ignores_directories_that_are_not_wired(tmp_path):
    from _ssot_audit import enumerate_fleet

    _clone(tmp_path, "wired")
    (tmp_path / "not-a-repo").mkdir()
    roots, skipped = enumerate_fleet(tmp_path)
    assert [r.name for r in roots] == ["wired"] and skipped == []


def test_auditing_a_worktree_directly_still_works(tmp_path):
    # --root names one repo on purpose. Refusing there would break a legitimate use; the
    # double-count only exists when enumerating a fleet.
    wt = _worktree(tmp_path, "audit-copy", "main-repo")
    _bootstrap(wt)
    _spec(wt, "s1", status="verified")
    a = audit_repo(wt)
    assert a.spec_count == 1 and a.wired


def test_a_brownfield_repo_reports_sync_blocked(tmp_path):
    repo = _repo(tmp_path)
    _spec(repo, "s1", status="verified")
    r = audit_repo(repo)
    assert r.sync_blocked is True
    assert r.has_adapter is False and r.has_behaviors is False and r.has_model is False


def test_a_bootstrapped_repo_is_not_blocked(tmp_path):
    repo = _repo(tmp_path)
    _bootstrap(repo)
    r = audit_repo(repo)
    assert r.sync_blocked is False


def test_the_adapter_alone_does_not_unblock_sync(tmp_path):
    # bootstrap_required fires when the adapter is missing OR system-behaviors.yaml is absent.
    # Reporting "ready" on half the precondition would send someone to debug a sync that was
    # always going to fail closed.
    repo = _repo(tmp_path)
    _bootstrap(repo, behaviors=False, model=False)
    assert audit_repo(repo).sync_blocked is True


def test_behaviors_alone_does_not_unblock_sync(tmp_path):
    repo = _repo(tmp_path)
    _bootstrap(repo, adapter=False, model=False)
    assert audit_repo(repo).sync_blocked is True


def test_a_missing_model_is_reported_but_does_not_block_sync(tmp_path):
    # A real gap, but not the thing that stops sync. Conflating them would misdirect the fix.
    repo = _repo(tmp_path)
    _bootstrap(repo, model=False)
    r = audit_repo(repo)
    assert r.has_model is False and r.sync_blocked is False


def test_a_malformed_adapter_does_not_count_as_present(tmp_path):
    # `adapter_for_repo` requires artifact: sync-adapter AND a list of mappings. A file that
    # merely exists is not an adapter, and reporting it as one manufactures false readiness.
    repo = _repo(tmp_path)
    _bootstrap(repo, adapter=False)
    (repo / ".builder" / "sync-adapter.yaml").write_text("artifact: nonsense\n", encoding="utf-8")
    r = audit_repo(repo)
    assert r.has_adapter is False and r.sync_blocked is True


def test_finished_specs_that_never_synced_are_counted(tmp_path):
    repo = _repo(tmp_path)
    _spec(repo, "a", status="verified")
    _spec(repo, "b", status="verified", synced=True)
    _spec(repo, "c", status="planned")          # not finished: not counted
    _spec(repo, "d", status="archived")         # archived without syncing: counted
    r = audit_repo(repo)
    assert r.finished_never_synced == ["a", "d"]


def test_specs_carrying_an_ssot_delta_are_counted(tmp_path):
    # One repo had 13 of these: specs that did their half of the contract and had nothing to
    # sync into. They are the ready-made backlog once a repo is bootstrapped.
    repo = _repo(tmp_path)
    _spec(repo, "a", status="verified", delta=True)
    _spec(repo, "b", status="verified")
    r = audit_repo(repo)
    assert r.awaiting_sync == ["a"]


def test_a_synced_spec_is_not_awaiting_sync(tmp_path):
    repo = _repo(tmp_path)
    _spec(repo, "a", status="synced", delta=True, synced=True)
    r = audit_repo(repo)
    assert r.awaiting_sync == [] and r.finished_never_synced == []


def test_archived_directory_specs_are_skipped(tmp_path):
    repo = _repo(tmp_path)
    old = repo / ".builder" / "specs" / "archive" / "2026-01-01-old"
    old.mkdir(parents=True)
    (old / "spec.yaml").write_text("name: old\nstatus: verified\n", encoding="utf-8")
    assert audit_repo(repo).finished_never_synced == []


def test_a_repo_without_a_builder_runtime_is_reported_not_crashed(tmp_path):
    repo = tmp_path / "bare"
    repo.mkdir()
    r = audit_repo(repo)
    assert r.wired is False and r.sync_blocked is True


# --- the provenance boundary --------------------------------------------------
#
# Owner decision 2026-07-29: sync goes FORWARD ONLY. Historical specs are reconciled at
# release level, not synced.
#
# It is not a preference, it is forced. Readmission rebuilds provenance from the
# host-recorded verify-bundle chain, and `readmit.py:92` raises `unsafe-evidence-directory`
# when the spec has no `gate-evidence/` directory. Measured across a real multi-repo portfolio:
# one repo had gate evidence for its specs and the other seven had none between them. Every one
# of those finished specs can never be synced at spec level, no matter how well its repo's
# adapter is curated -- the evidence cannot be reconstructed after the fact.
#
# The audit must therefore separate what someone can act on from what is permanently
# historical. Reporting a flat "152 never synced" forever is a number nobody can ever drive to
# zero, and a check that fires on unchangeable state gets dismissed — then ignored when it
# finally reports something real.


def _gate_evidence(repo, spec_id: str):
    (repo / ".builder" / "specs" / spec_id / "gate-evidence").mkdir(parents=True, exist_ok=True)


def test_a_finished_spec_without_gate_evidence_is_historical_not_actionable(tmp_path):
    repo = _repo(tmp_path)
    _spec(repo, "old", status="verified")
    r = audit_repo(repo)
    assert r.historical_no_provenance == ["old"]
    assert r.unsynced_actionable == []


def test_a_finished_spec_with_gate_evidence_is_actionable(tmp_path):
    repo = _repo(tmp_path)
    _spec(repo, "recent", status="verified")
    _gate_evidence(repo, "recent")
    r = audit_repo(repo)
    assert r.unsynced_actionable == ["recent"]
    assert r.historical_no_provenance == []


def test_the_two_partitions_are_disjoint_and_cover_finished_never_synced(tmp_path):
    # The split must be a partition, not two overlapping views: anything double-counted or
    # dropped makes the totals lie, and the totals are the whole point of the audit.
    repo = _repo(tmp_path)
    _spec(repo, "a", status="verified")
    _spec(repo, "b", status="verified")
    _gate_evidence(repo, "b")
    _spec(repo, "c", status="archived")
    _spec(repo, "d", status="verified", synced=True)
    r = audit_repo(repo)
    assert sorted(r.unsynced_actionable + r.historical_no_provenance) == sorted(r.finished_never_synced)
    assert set(r.unsynced_actionable) & set(r.historical_no_provenance) == set()
    assert "d" not in r.finished_never_synced


def test_a_synced_spec_is_in_neither_partition(tmp_path):
    repo = _repo(tmp_path)
    _spec(repo, "a", status="verified", synced=True)
    _gate_evidence(repo, "a")
    r = audit_repo(repo)
    assert r.unsynced_actionable == [] and r.historical_no_provenance == []


def test_an_empty_gate_evidence_directory_still_counts_as_present(tmp_path):
    # readmit refuses on `not evidence_dir.is_dir()`; it validates the CHAIN separately. The
    # audit reports reachability, not chain validity — claiming a spec is unreadmittable when
    # readmit would merely reject its chain would misreport the fix.
    repo = _repo(tmp_path)
    _spec(repo, "a", status="verified")
    _gate_evidence(repo, "a")
    assert audit_repo(repo).unsynced_actionable == ["a"]


# --- the archive gate --------------------------------------------------------
#
# Owner decision 2026-07-29: /isanna-archive must refuse a spec that never synced. Landed
# STAGED, because switching it on hard today would block archiving in most repos at once
# — none of which can sync yet. `warn` reports and allows; BUILDER_ARCHIVE_REQUIRE_SYNC=enforce
# refuses, and gets switched on per repo as each one's curation completes.


def _with_gate(value, fn):
    import os
    key = "BUILDER_ARCHIVE_REQUIRE_SYNC"
    saved = os.environ.get(key)
    if value is None:
        os.environ.pop(key, None)
    else:
        os.environ[key] = value
    try:
        return fn()
    finally:
        if saved is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = saved


# --- per-repo enforcement -----------------------------------------------------
#
# BUILDER_ARCHIVE_REQUIRE_SYNC was read from the process environment only, so "flip it to
# enforce per repo as that repo's SSOT curation completes" was not actually expressible: the
# setting followed whoever's shell invoked the command, not the repo being archived. With 22
# repos at different stages of backfill, that is the wrong axis entirely.
#
# The setting now also lives in the repo's own .builder/dispatch.yaml under `pipeline`, so it
# travels with the repo. The env var still wins when set, so an operator can override for one
# invocation without editing a committed file.


def _dispatch_yaml(repo, value):
    (repo / ".builder").mkdir(parents=True, exist_ok=True)
    (repo / ".builder" / "dispatch.yaml").write_text(
        f"pipeline:\n  archive_require_sync: {value}\n", encoding="utf-8")


# --- the archive gate must not give advice that cannot be followed -------------
#
# Observed on a real repo, on a spec immediately
# after it was promoted to `verified`: `isanna ssot archive-check` said
#
#   "The repo IS bootstrapped, so run the sync phase; archiving now loses this spec's SSOT
#    update permanently."
#
# That spec has no `gate-evidence/`, so readmission refuses it with
# `unsafe-evidence-directory` and the sync phase can NEVER succeed for it. With
# archive_require_sync=enforce the spec could then neither sync nor be archived -- wedged, on
# advice that cannot be followed. Builder's own audit says "can never sync at spec level" about
# the same spec in the same breath, so two tools disagreed about one fact.
#
# This gate's docstring already states the principle -- reporting the wrong blocker "sends the
# reader to the wrong file". There was simply a third case it did not know about.


def test_a_spec_that_can_never_sync_is_not_told_to_run_the_sync_phase(tmp_path):
    from _ssot_audit import archive_sync_gate

    repo = _repo(tmp_path)
    _bootstrap(repo)
    _spec(repo, "s1", status="verified")  # no gate-evidence/ -> unreadmittable, forever
    v = _with_gate(None, lambda: archive_sync_gate(repo, "s1"))
    assert "run the sync phase" not in v.reason
    assert "never" in v.reason and "gate-evidence" in v.reason


def test_the_permanent_case_names_the_decision_rather_than_a_task(tmp_path):
    # There is no command that fixes this. Saying so is the whole point: an impossible
    # instruction reads as a task, gets attempted, fails, and burns the reader's trust in the
    # gate. This is a release-level reconciliation decision.
    from _ssot_audit import archive_sync_gate

    repo = _repo(tmp_path)
    _bootstrap(repo)
    _spec(repo, "s1", status="verified")
    v = _with_gate(None, lambda: archive_sync_gate(repo, "s1"))
    assert "release-level" in v.reason


def test_a_spec_with_gate_evidence_is_still_told_to_sync(tmp_path):
    # The actionable case must keep its actionable advice -- this spec CAN sync.
    from _ssot_audit import archive_sync_gate

    repo = _repo(tmp_path)
    _bootstrap(repo)
    d = _spec(repo, "s1", status="verified")
    (d / "gate-evidence").mkdir()
    v = _with_gate(None, lambda: archive_sync_gate(repo, "s1"))
    assert "run the sync phase" in v.reason


def test_the_permanent_case_outranks_the_repo_level_blocker(tmp_path):
    # A spec that can never sync will not be helped by bootstrapping its repo, so "bootstrap
    # the repo first" is impossible advice too -- just one level up. Lead with the fact that
    # does not change.
    from _ssot_audit import archive_sync_gate

    repo = _repo(tmp_path)  # deliberately NOT bootstrapped
    _spec(repo, "s1", status="verified")
    v = _with_gate(None, lambda: archive_sync_gate(repo, "s1"))
    assert "gate-evidence" in v.reason
    assert "Bootstrap the repo before archiving" not in v.reason


def test_the_permanent_case_does_not_silently_change_the_archive_POLICY(tmp_path):
    # Telling the truth about WHY is not the same as deciding whether these may be archived.
    # Under enforce this still refuses; whether that should change for the permanently
    # unsyncable class is an owner policy call, not something this fix quietly grants.
    from _ssot_audit import archive_sync_gate

    repo = _repo(tmp_path)
    _bootstrap(repo)
    _spec(repo, "s1", status="verified")
    assert _with_gate("enforce", lambda: archive_sync_gate(repo, "s1")).allowed is False
    assert _with_gate("warn", lambda: archive_sync_gate(repo, "s1")).allowed is True


def test_the_repo_key_alone_can_enforce(tmp_path):
    from _ssot_audit import archive_sync_gate

    repo = _repo(tmp_path)
    _bootstrap(repo)
    _spec(repo, "s1", status="verified")
    _dispatch_yaml(repo, "enforce")
    v = _with_gate(None, lambda: archive_sync_gate(repo, "s1"))
    assert v.allowed is False and v.enforced is True


def test_the_repo_key_alone_can_stay_at_warn(tmp_path):
    from _ssot_audit import archive_sync_gate

    repo = _repo(tmp_path)
    _bootstrap(repo)
    _spec(repo, "s1", status="verified")
    _dispatch_yaml(repo, "warn")
    v = _with_gate(None, lambda: archive_sync_gate(repo, "s1"))
    assert v.allowed is True and v.enforced is False


def test_the_env_var_wins_over_the_repo_key(tmp_path):
    # An operator must be able to override for a single invocation without editing a committed
    # file. Env is the narrower, more deliberate scope, so it takes precedence.
    from _ssot_audit import archive_sync_gate

    repo = _repo(tmp_path)
    _bootstrap(repo)
    _spec(repo, "s1", status="verified")
    _dispatch_yaml(repo, "enforce")
    v = _with_gate("warn", lambda: archive_sync_gate(repo, "s1"))
    assert v.allowed is True and v.enforced is False


def test_the_env_var_wins_when_the_repo_key_is_absent(tmp_path):
    from _ssot_audit import archive_sync_gate

    repo = _repo(tmp_path)
    _bootstrap(repo)
    _spec(repo, "s1", status="verified")
    v = _with_gate("enforce", lambda: archive_sync_gate(repo, "s1"))
    assert v.enforced is True


def test_neither_source_set_stays_at_warn(tmp_path):
    from _ssot_audit import archive_sync_gate

    repo = _repo(tmp_path)
    _bootstrap(repo)
    _spec(repo, "s1", status="verified")
    v = _with_gate(None, lambda: archive_sync_gate(repo, "s1"))
    assert v.allowed is True and v.enforced is False


def test_a_typo_in_the_repo_key_does_not_silently_enforce(tmp_path):
    from _ssot_audit import archive_sync_gate

    repo = _repo(tmp_path)
    _bootstrap(repo)
    _spec(repo, "s1", status="verified")
    _dispatch_yaml(repo, "enfroce")
    v = _with_gate(None, lambda: archive_sync_gate(repo, "s1"))
    assert v.enforced is False


def test_a_malformed_dispatch_yaml_does_not_crash_or_enforce(tmp_path):
    # An unreadable config must degrade to the safe default, not to a refusal and not to a
    # traceback in the middle of an archive.
    from _ssot_audit import archive_sync_gate

    repo = _repo(tmp_path)
    _bootstrap(repo)
    _spec(repo, "s1", status="verified")
    (repo / ".builder" / "dispatch.yaml").write_text("pipeline: [not, a, mapping\n", encoding="utf-8")
    v = _with_gate(None, lambda: archive_sync_gate(repo, "s1"))
    assert v.enforced is False and v.allowed is True


def test_an_empty_env_value_falls_through_to_the_repo_key(tmp_path):
    # BUILDER_ARCHIVE_REQUIRE_SYNC="" is "unset", not "warn" — otherwise an empty export in a
    # shell profile would silently disable every repo's committed setting.
    from _ssot_audit import archive_sync_gate

    repo = _repo(tmp_path)
    _bootstrap(repo)
    _spec(repo, "s1", status="verified")
    _dispatch_yaml(repo, "enforce")
    v = _with_gate("", lambda: archive_sync_gate(repo, "s1"))
    assert v.enforced is True


def test_a_synced_spec_is_allowed_to_archive(tmp_path):
    from _ssot_audit import archive_sync_gate

    repo = _repo(tmp_path)
    _bootstrap(repo)
    _spec(repo, "s1", status="verified", synced=True)
    v = _with_gate("enforce", lambda: archive_sync_gate(repo, "s1"))
    assert v.allowed is True


def test_an_unsynced_spec_is_refused_under_enforce(tmp_path):
    from _ssot_audit import archive_sync_gate

    repo = _repo(tmp_path)
    _bootstrap(repo)
    _spec(repo, "s1", status="verified")
    v = _with_gate("enforce", lambda: archive_sync_gate(repo, "s1"))
    assert v.allowed is False and "never synced" in v.reason


def test_warn_is_the_default_and_allows(tmp_path):
    # Most repos cannot sync at all today. Defaulting to enforce would freeze archiving
    # fleet-wide for a backfill that has not happened yet.
    from _ssot_audit import archive_sync_gate

    repo = _repo(tmp_path)
    _spec(repo, "s1", status="verified")
    v = _with_gate(None, lambda: archive_sync_gate(repo, "s1"))
    assert v.allowed is True and v.enforced is False and v.reason


def test_a_typo_does_not_silently_enforce(tmp_path):
    from _ssot_audit import archive_sync_gate

    repo = _repo(tmp_path)
    _spec(repo, "s1", status="verified")
    v = _with_gate("enfroce", lambda: archive_sync_gate(repo, "s1"))
    assert v.allowed is True and v.enforced is False


def test_an_unbootstrapped_repo_names_that_as_the_reason(tmp_path):
    # "This spec never synced" and "this repo cannot sync at all" need different fixes.
    # Reporting the first when the second is true sends someone to the wrong file.
    from _ssot_audit import archive_sync_gate

    repo = _repo(tmp_path)
    _spec(repo, "s1", status="verified")
    v = _with_gate("enforce", lambda: archive_sync_gate(repo, "s1"))
    assert v.allowed is False and "bootstrap" in v.reason.lower()


def test_an_unknown_spec_is_refused_rather_than_waved_through(tmp_path):
    from _ssot_audit import archive_sync_gate

    repo = _repo(tmp_path)
    _bootstrap(repo)
    v = _with_gate("enforce", lambda: archive_sync_gate(repo, "nope"))
    assert v.allowed is False


def test_cli_archive_check_exit_codes(tmp_path):
    repo = _repo(tmp_path)
    _bootstrap(repo)
    _spec(repo, "s1", status="verified")
    code, out = _with_gate("enforce", lambda: _cli(
        ["ssot", "archive-check", "--root", str(repo), "--spec", "s1"]))
    assert code == 1 and "REFUSED" in out
    _spec(repo, "s2", status="verified", synced=True)
    code, out = _with_gate("enforce", lambda: _cli(
        ["ssot", "archive-check", "--root", str(repo), "--spec", "s2"]))
    assert code == 0


def _cli(argv):
    """Run `isanna ssot ...` capturing stdout. Returns (exit_code, output)."""
    import contextlib
    import importlib.util
    import io

    spec = importlib.util.spec_from_file_location("isanna_ssot_cli", SCRIPTS / "isanna.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = module.main(argv)
    return code, buf.getvalue()


def test_cli_strict_exits_nonzero_on_a_blocked_repo(tmp_path):
    repo = _repo(tmp_path)
    _spec(repo, "s1", status="verified")
    code, out = _cli(["ssot", "audit", "--root", str(repo), "--strict"])
    assert code == 1 and "BLOCKED" in out


def test_cli_strict_exits_zero_when_bootstrapped(tmp_path):
    repo = _repo(tmp_path)
    _bootstrap(repo)
    code, _ = _cli(["ssot", "audit", "--root", str(repo), "--strict"])
    assert code == 0


def test_cli_without_strict_never_changes_the_exit_code(tmp_path):
    # Staged like every other gate here: report first, enforce only when asked.
    repo = _repo(tmp_path)
    _spec(repo, "s1", status="verified")
    code, _ = _cli(["ssot", "audit", "--root", str(repo)])
    assert code == 0


def test_cli_json_carries_the_blocking_reason(tmp_path):
    import json

    repo = _repo(tmp_path)
    _spec(repo, "a", status="verified", delta=True)
    code, out = _cli(["ssot", "audit", "--root", str(repo), "--json"])
    payload = json.loads(out)
    assert payload[0]["sync_blocked"] is True
    assert payload[0]["awaiting_sync"] == ["a"]


def test_results_are_deterministic_in_spec_order(tmp_path):
    repo = _repo(tmp_path)
    for sid in ("zeta", "alpha", "mid"):
        _spec(repo, sid, status="verified")
    assert audit_repo(repo).finished_never_synced == ["alpha", "mid", "zeta"]
