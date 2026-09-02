"""R5: scoped delivery. `deliver()`'s new `scoped` kwarg (default False, wired to
pipeline.worktree_isolation by the scheduler) replaces the blanket `git add -A`
with per-file `git add -- <path>` sourced from traceability.yaml/handoff.yaml, and
skips the redundant `git checkout -B` when the tree already reports being on the
delivery branch. `scoped=False` (the default / flag-off call shape) must remain
byte-identical to the pre-R5 behavior: unconditional `checkout -B` + `add -A`.

Model A hardening (H4/L1): every scoped `git add` uses the `--` separator, and a
single failed scoped add aborts the whole scoped path in favor of `git add -A`
(never a partial commit) — see `test_deliver_scoped_add_uses_dash_dash_separator`
/ `test_deliver_scoped_add_failure_falls_back_to_add_dash_a_fail_closed`. Under
`pipeline.worktree_isolation`, `.builder/specs/<id>` is a symlink to the shared
MAIN control dir; `test_deliver_scoped_never_stages_the_symlinked_control_dir`
guards that scoped delivery never explicitly stages it.

Shim-safe: no pytest.raises/monkeypatch — the git/gh runner is the existing
injectable `runner=` seam (a fake object), never subprocess/module patching.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from _dispatch_runtime.delivery import branch_name_for, deliver


class _FakeRunner:
    """Records every argv; answers `git rev-parse --abbrev-ref HEAD` with a fixed
    branch name (so scoped delivery's on-branch check is exercisable) and
    `gh pr create` with a fake URL. Everything else succeeds with empty output."""

    def __init__(self, *, current_branch: str = ""):
        self.calls: list[list[str]] = []
        self.current_branch = current_branch

    def run(self, argv: list[str], cwd: str) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(argv))
        if argv[:3] == ["git", "rev-parse", "--abbrev-ref"] and argv[-1] == "HEAD":
            return subprocess.CompletedProcess(argv, 0, stdout=self.current_branch + "\n", stderr="")
        if argv[:2] == ["gh", "pr"] and "create" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout="https://example.test/pr/1\n", stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")


def _write_traceability(spec_dir: Path, paths: list[str]) -> None:
    spec_dir.mkdir(parents=True, exist_ok=True)
    files_block = "\n".join(f"      - path: {p}" for p in paths)
    (spec_dir / "traceability.yaml").write_text(
        f"task_links:\n  - task_id: T1\n    files:\n{files_block}\n",
        encoding="utf-8",
    )


def test_branch_name_for_matches_default_prefix_and_honors_override():
    assert branch_name_for("demo-spec", {}) == "builder/demo-spec"
    assert branch_name_for("demo-spec", None) == "builder/demo-spec"
    assert branch_name_for("demo-spec", {"branch_prefix": "spec/"}) == "spec/demo-spec"


def test_deliver_default_scoped_false_uses_add_dash_a_and_unconditional_checkout(tmp_path):
    """Flag-off byte-identical guard: omitting `scoped` (its default) must behave
    exactly as delivery did before R5 — unconditional `checkout -B` + `add -A`."""
    runner = _FakeRunner()
    result = deliver(tmp_path, "demo-spec", {"auto_merge": False, "base": "main"},
                      summary="x", runner=runner)

    assert result.ok
    assert ["git", "checkout", "-B", "builder/demo-spec"] in runner.calls
    assert ["git", "add", "-A"] in runner.calls


def test_deliver_scoped_adds_only_traceability_paths_not_dash_a(tmp_path):
    spec_dir = tmp_path / ".builder" / "specs" / "demo-spec"
    _write_traceability(spec_dir, ["src/a.py", "src/b.py"])
    runner = _FakeRunner(current_branch="builder/demo-spec")

    result = deliver(tmp_path, "demo-spec", {"auto_merge": False, "base": "main"},
                      summary="did the thing", runner=runner, scoped=True)

    assert result.ok
    add_calls = [c for c in runner.calls if c[:2] == ["git", "add"]]
    assert add_calls == [["git", "add", "--", "src/a.py"], ["git", "add", "--", "src/b.py"]]
    assert ["git", "add", "-A"] not in runner.calls


def test_deliver_scoped_skips_checkout_when_already_on_branch(tmp_path):
    spec_dir = tmp_path / ".builder" / "specs" / "demo-spec"
    _write_traceability(spec_dir, ["src/a.py"])
    runner = _FakeRunner(current_branch="builder/demo-spec")  # already on it

    result = deliver(tmp_path, "demo-spec", {"auto_merge": False, "base": "main"},
                      summary="x", runner=runner, scoped=True)

    assert result.ok
    checkout_calls = [c for c in runner.calls if c[:2] == ["git", "checkout"]]
    assert checkout_calls == []


def test_deliver_scoped_checks_out_branch_when_not_already_on_it(tmp_path):
    """Defensive fallback: scoped mode still checks out the branch if the tree
    (for whatever reason) isn't already sitting on it."""
    spec_dir = tmp_path / ".builder" / "specs" / "demo-spec"
    _write_traceability(spec_dir, ["src/a.py"])
    runner = _FakeRunner(current_branch="main")  # NOT on the spec branch

    result = deliver(tmp_path, "demo-spec", {"auto_merge": False, "base": "main"},
                      summary="x", runner=runner, scoped=True)

    assert result.ok
    assert ["git", "checkout", "-B", "builder/demo-spec"] in runner.calls


def test_deliver_scoped_falls_back_to_add_dash_a_when_no_traceability_or_handoff(tmp_path):
    """Missing/empty traceability+handoff must not silently deliver nothing —
    falls back to `git add -A` (and records the fallback in `steps`). H-2: the
    fallback must exclude `.builder` (never a plain `add -A`) — see
    `test_deliver_scoped_add_dash_a_fallback_never_stages_builder`."""
    runner = _FakeRunner(current_branch="builder/demo-spec")

    result = deliver(tmp_path, "demo-spec", {"auto_merge": False, "base": "main"},
                      summary="x", runner=runner, scoped=True)

    assert result.ok
    assert ["git", "add", "-A", "--", ":(exclude).builder"] in runner.calls
    assert ["git", "add", "-A"] not in runner.calls
    assert any("falling back to git add -A" in step for step in result.steps)


def test_deliver_scoped_includes_handoff_files_written_and_dedupes(tmp_path):
    spec_dir = tmp_path / ".builder" / "specs" / "demo-spec"
    _write_traceability(spec_dir, ["src/a.py"])
    (spec_dir / "handoff.yaml").write_text(
        "files_written:\n  - src/b.py\n  - src/a.py\n",  # src/a.py is a dup of traceability
        encoding="utf-8",
    )
    runner = _FakeRunner(current_branch="builder/demo-spec")

    result = deliver(tmp_path, "demo-spec", {"auto_merge": False, "base": "main"},
                      summary="x", runner=runner, scoped=True)

    assert result.ok
    add_calls = [c for c in runner.calls if c[:2] == ["git", "add"]]
    assert add_calls == [["git", "add", "--", "src/a.py"], ["git", "add", "--", "src/b.py"]]


def test_deliver_scoped_tolerates_malformed_traceability_and_falls_back(tmp_path):
    """A YAML list at the top level is malformed for this artifact (raises via
    `.get()` under both real PyYAML and the shim if not guarded) — must be treated
    as absent, not crash delivery."""
    spec_dir = tmp_path / ".builder" / "specs" / "demo-spec"
    spec_dir.mkdir(parents=True, exist_ok=True)
    (spec_dir / "traceability.yaml").write_text("- a\n- b\n", encoding="utf-8")
    runner = _FakeRunner(current_branch="builder/demo-spec")

    result = deliver(tmp_path, "demo-spec", {"auto_merge": False, "base": "main"},
                      summary="x", runner=runner, scoped=True)

    assert result.ok
    assert ["git", "add", "-A", "--", ":(exclude).builder"] in runner.calls
    assert ["git", "add", "-A"] not in runner.calls


class _PartialFailRunner(_FakeRunner):
    """Like `_FakeRunner`, but a configured `git add -- <path>` fails (nonzero
    rc) — drives the H4 fail-closed abort-to-`add -A` path."""

    def __init__(self, *, current_branch: str = "", fail_path: str = ""):
        super().__init__(current_branch=current_branch)
        self.fail_path = fail_path

    def run(self, argv: list[str], cwd: str) -> subprocess.CompletedProcess[str]:
        if argv[:2] == ["git", "add"] and "--" in argv and argv[-1] == self.fail_path:
            self.calls.append(list(argv))
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="fatal: pathspec did not match")
        return super().run(argv, cwd)


def test_deliver_scoped_add_uses_dash_dash_separator(tmp_path):
    """L1: every scoped `git add` call must use the `--` separator, so a path
    that looks like an option (e.g. one starting with `-`) is never misread as
    one."""
    spec_dir = tmp_path / ".builder" / "specs" / "demo-spec"
    _write_traceability(spec_dir, ["src/a.py", "src/b.py"])
    runner = _FakeRunner(current_branch="builder/demo-spec")

    result = deliver(tmp_path, "demo-spec", {"auto_merge": False, "base": "main"},
                      summary="x", runner=runner, scoped=True)

    assert result.ok
    scoped_add_calls = [c for c in runner.calls if c[:2] == ["git", "add"] and c != ["git", "add", "-A"]]
    assert scoped_add_calls  # at least one scoped add happened
    assert all(c[2] == "--" for c in scoped_add_calls)


def test_deliver_scoped_add_failure_falls_back_to_add_dash_a_fail_closed(tmp_path):
    """H4: one scoped `git add` failing must abort the scoped path entirely and
    fall back to `git add -A` — never ship a partial commit. The isolated
    worktree may be the ONLY copy of the spec's uncommitted work, so a partial
    stage followed by `_cleanup_worktree` would silently lose the rest of it."""
    spec_dir = tmp_path / ".builder" / "specs" / "demo-spec"
    _write_traceability(spec_dir, ["src/a.py", "src/b.py"])
    runner = _PartialFailRunner(current_branch="builder/demo-spec", fail_path="src/b.py")

    result = deliver(tmp_path, "demo-spec", {"auto_merge": False, "base": "main"},
                      summary="x", runner=runner, scoped=True)

    assert result.ok
    assert ["git", "add", "-A", "--", ":(exclude).builder"] in runner.calls
    assert ["git", "add", "-A"] not in runner.calls
    assert any("aborting scoped add" in step for step in result.steps)


def test_deliver_scoped_all_clean_adds_never_falls_back(tmp_path):
    """Counterpart to the fail-closed case: when every scoped add succeeds,
    `git add -A` is never invoked."""
    spec_dir = tmp_path / ".builder" / "specs" / "demo-spec"
    _write_traceability(spec_dir, ["src/a.py", "src/b.py"])
    runner = _FakeRunner(current_branch="builder/demo-spec")

    result = deliver(tmp_path, "demo-spec", {"auto_merge": False, "base": "main"},
                      summary="x", runner=runner, scoped=True)

    assert result.ok
    assert ["git", "add", "-A"] not in runner.calls


def test_deliver_scoped_never_stages_the_symlinked_control_dir(tmp_path):
    """Model A: even if a traceability/handoff record lists a path under the
    spec's own control dir (a symlink into MAIN under isolation), scoped
    delivery must never explicitly stage it — control state lives in MAIN
    regardless of what the source PR ships."""
    spec_dir = tmp_path / ".builder" / "specs" / "demo-spec"
    _write_traceability(spec_dir, ["src/a.py", ".builder/specs/demo-spec/handoff.yaml"])
    runner = _FakeRunner(current_branch="builder/demo-spec")

    result = deliver(tmp_path, "demo-spec", {"auto_merge": False, "base": "main"},
                      summary="x", runner=runner, scoped=True)

    assert result.ok
    add_calls = [c for c in runner.calls if c[:2] == ["git", "add"]]
    assert add_calls == [["git", "add", "--", "src/a.py"]]


# ---------------------------------------------------------------------------
# R5 pass-2 (fable adversarial review) — H-2, H-1, L-A
# ---------------------------------------------------------------------------


def test_deliver_scoped_add_dash_a_fallback_never_stages_builder(tmp_path):
    """H-2 (DESTRUCTIVE, empirically verified in a sandbox repo — see the task
    report): under Model A, `.builder/specs/<id>` inside the isolated
    worktree is a symlink to the shared MAIN control dir. A plain `git add -A`
    fallback would stage that symlink (mode 120000) plus a `D` for every
    tracked control file beneath it; after the delivery PR's auto-merge, MAIN's
    own `.builder/specs/<id>` becomes a symlink pointing at ITSELF (ELOOP),
    destroying the real spec.yaml/traceability.yaml/handoff.yaml. Both `add -A`
    fallback call SHAPES (no-traceability and H4-fail-closed) must use the
    `:(exclude).builder` pathspec, never bare `-A`."""
    runner = _FakeRunner(current_branch="builder/demo-spec")  # no traceability/handoff -> fallback

    result = deliver(tmp_path, "demo-spec", {"auto_merge": False, "base": "main"},
                      summary="x", runner=runner, scoped=True)

    assert result.ok
    assert ["git", "add", "-A"] not in runner.calls  # never the bare/unsafe form
    assert ["git", "add", "-A", "--", ":(exclude).builder"] in runner.calls


def test_deliver_scoped_skips_commit_on_nothing_staged_instead_of_hard_failing(tmp_path):
    """H-1: a scoped delivery whose adds stage nothing NEW (idempotent
    re-delivery of an already-committed branch, in a worktree that is
    PERMANENTLY dirty because of the untracked `.builder` control symlink)
    must SKIP the commit — detected via `git diff --cached --quiet` (rc=0 =>
    nothing staged), never by string-matching commit's stderr — and still
    proceed to push/PR. Before the fix, `git commit` would run anyway and (in
    a real repo) fail with "no changes added to commit", which the pre-existing
    string tolerance does not recognize, raising and permanently blocking this
    spec's delivery on every retry."""

    class _NoOpCommitRunner(_FakeRunner):
        """`git diff --cached --quiet` reports nothing staged (rc=0). If `git
        commit` were invoked ANYWAY (the pre-fix behavior), it returns the
        untolerated real-git wording for a dirty-but-nothing-staged tree —
        proving the fix must never actually reach that call."""

        def run(self, argv, cwd):
            if argv[:3] == ["git", "diff", "--cached"]:
                self.calls.append(list(argv))
                return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
            if argv[:2] == ["git", "commit"]:
                self.calls.append(list(argv))
                return subprocess.CompletedProcess(
                    argv, 1, stdout="",
                    stderr='no changes added to commit (use "git add" and/or "git commit -a")',
                )
            return super().run(argv, cwd)

    spec_dir = tmp_path / ".builder" / "specs" / "demo-spec"
    _write_traceability(spec_dir, ["src/a.py"])
    runner = _NoOpCommitRunner(current_branch="builder/demo-spec")

    result = deliver(tmp_path, "demo-spec", {"auto_merge": False, "base": "main"},
                      summary="x", runner=runner, scoped=True)

    assert result.ok, result.reason
    assert any(c[:3] == ["git", "diff", "--cached"] for c in runner.calls)
    assert not any(c[:2] == ["git", "commit"] for c in runner.calls)  # skipped, never invoked
    assert any(c[:2] == ["git", "push"] for c in runner.calls)  # still proceeds
    assert any("skipping commit" in step for step in result.steps)


def test_deliver_scoped_commit_still_runs_when_something_is_staged(tmp_path):
    """Counterpart to the skip case: when `git diff --cached --quiet` reports a
    real staged change (rc=1), the commit must still run normally — the H-1
    pre-check only short-circuits the genuinely-empty case."""

    class _StagedRunner(_FakeRunner):
        def run(self, argv, cwd):
            if argv[:3] == ["git", "diff", "--cached"]:
                self.calls.append(list(argv))
                return subprocess.CompletedProcess(argv, 1, stdout="", stderr="")  # something staged
            return super().run(argv, cwd)

    spec_dir = tmp_path / ".builder" / "specs" / "demo-spec"
    _write_traceability(spec_dir, ["src/a.py"])
    runner = _StagedRunner(current_branch="builder/demo-spec")

    result = deliver(tmp_path, "demo-spec", {"auto_merge": False, "base": "main"},
                      summary="x", runner=runner, scoped=True)

    assert result.ok
    assert any(c[:2] == ["git", "commit"] for c in runner.calls)


def test_deliver_default_scoped_false_never_runs_the_diff_cached_precheck(tmp_path):
    """Flag-off byte-identical guard (H-1): the non-scoped path's step log must
    stay untouched — no `git diff --cached --quiet` pre-check call at all."""
    runner = _FakeRunner()

    result = deliver(tmp_path, "demo-spec", {"auto_merge": False, "base": "main"},
                      summary="x", runner=runner)

    assert result.ok
    assert not any(c[:3] == ["git", "diff", "--cached"] for c in runner.calls)


def test_scoped_add_paths_normalizes_dot_slash_absolute_and_traversal_before_filter(tmp_path):
    """L-A: `_scoped_add_paths` must normalize a recorded path (strip a leading
    `./`, drop an absolute or `..`-traversal path outright) BEFORE the
    `.builder/specs/<id>` control-dir filter runs, so none of those forms can
    slip the spec's own (symlinked) control dir past it."""
    from _dispatch_runtime.delivery import _scoped_add_paths

    spec_dir = tmp_path / ".builder" / "specs" / "demo-spec"
    _write_traceability(spec_dir, [
        "./.builder/specs/demo-spec/handoff.yaml",     # dot-slash bypass attempt -> dropped
        "/etc/passwd",                                    # absolute -> out of scope, dropped
        "../../.builder/specs/demo-spec/spec.yaml",     # traversal -> out of scope, dropped
        "./src/a.py",                                     # legit path, just dot-slash prefixed
    ])

    paths, ok = _scoped_add_paths(tmp_path, "demo-spec")

    assert ok
    assert paths == ["src/a.py"]


# ---- delivery RECORD (host-authored provenance for the readiness `merged` rung) ----

from _dispatch_runtime.delivery import write_delivery_record, DELIVERY_PROVENANCE


class _ShaRunner(_FakeRunner):
    """Answers `git rev-parse HEAD` with a real sha and `git merge-base --is-ancestor` with rc 1
    (the sha is AHEAD of base = a genuine pending delivery), so deliver() writes the record. Set
    `already_merged=True` to answer is-ancestor rc 0 (HEAD already on base -> no record)."""
    def __init__(self, *, current_branch="", already_merged=False):
        super().__init__(current_branch=current_branch)
        self.already_merged = already_merged

    def run(self, argv, cwd):
        import subprocess as _sp
        if argv == ["git", "rev-parse", "HEAD"]:
            self.calls.append(list(argv))
            return _sp.CompletedProcess(argv, 0, stdout="a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2\n", stderr="")
        if argv[:3] == ["git", "merge-base", "--is-ancestor"]:
            self.calls.append(list(argv))
            return _sp.CompletedProcess(argv, 0 if self.already_merged else 1, stdout="", stderr="")
        return super().run(argv, cwd)


def test_write_delivery_record_carries_the_host_marker_and_real_commit(tmp_path):
    spec_dir = tmp_path / ".builder" / "specs" / "demo-spec"
    spec_dir.mkdir(parents=True)
    path = write_delivery_record(tmp_path, "demo-spec", "builder/demo-spec",
                                 "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2", base="main",
                                 pr_url="https://example.test/pr/1")
    text = path.read_text()
    assert f"recorded_by: {DELIVERY_PROVENANCE}" in text
    assert "commit: a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2" in text
    assert "branch: builder/demo-spec" in text


def test_write_delivery_record_refuses_junk(tmp_path):
    (tmp_path / ".builder" / "specs" / "demo").mkdir(parents=True)
    # non-sha commit, path-traversal spec id, and a missing spec dir all yield no record
    assert write_delivery_record(tmp_path, "demo", "b", "not-a-sha") is None
    assert write_delivery_record(tmp_path, "../evil", "b", "a1b2c3d") is None
    assert write_delivery_record(tmp_path, "ghost", "b", "a1b2c3d") is None


def test_deliver_writes_a_record_when_the_sha_is_known(tmp_path):
    (tmp_path / ".builder" / "specs" / "demo-spec").mkdir(parents=True)
    runner = _ShaRunner()
    result = deliver(tmp_path, "demo-spec", {"auto_merge": False, "base": "main"}, runner=runner)
    assert result.ok
    rec = (tmp_path / ".builder" / "specs" / "demo-spec" / "delivery.yaml").read_text()
    assert f"recorded_by: {DELIVERY_PROVENANCE}" in rec and "a1b2c3d4e5f6" in rec


def test_deliver_without_a_sha_writes_no_record_backwards_compatible(tmp_path):
    # The existing _FakeRunner returns empty for `git rev-parse HEAD` -> no record, delivery still ok.
    (tmp_path / ".builder" / "specs" / "demo-spec").mkdir(parents=True)
    runner = _FakeRunner()
    result = deliver(tmp_path, "demo-spec", {"auto_merge": False, "base": "main"}, runner=runner)
    assert result.ok
    assert not (tmp_path / ".builder" / "specs" / "demo-spec" / "delivery.yaml").exists()


def test_deliver_records_nothing_when_head_is_already_on_the_base(tmp_path):
    # THE review attack: deliver tolerates a no-op commit (re-delivery / existing branch), then HEAD
    # could be an already-merged commit. Recording it would forge `merged` for work not delivered
    # this run. is-ancestor rc 0 (already on the default) -> no record written; delivery succeeds.
    (tmp_path / ".builder" / "specs" / "demo-spec").mkdir(parents=True)
    runner = _ShaRunner(already_merged=True)
    result = deliver(tmp_path, "demo-spec", {"auto_merge": False, "base": "main"}, runner=runner)
    assert result.ok
    assert not (tmp_path / ".builder" / "specs" / "demo-spec" / "delivery.yaml").exists()


def test_deliver_guard_checks_origin_default_not_the_config_base(tmp_path):
    # Final-round attack: a stale/manipulated `base` behind the default branch would make an
    # already-merged commit look "ahead". The guard checks origin/<default>, NOT the config base,
    # so an already-merged HEAD is refused regardless of what `base` is set to.
    (tmp_path / ".builder" / "specs" / "demo-spec").mkdir(parents=True)
    runner = _ShaRunner(already_merged=True)  # HEAD IS an ancestor of origin/default
    result = deliver(tmp_path, "demo-spec",
                     {"auto_merge": False, "base": "some-stale-ancient-ref"}, runner=runner)
    assert result.ok
    assert not (tmp_path / ".builder" / "specs" / "demo-spec" / "delivery.yaml").exists()
    # and the guard probed origin/<default>, never the config base
    mb = [c for c in runner.calls if c[:3] == ["git", "merge-base", "--is-ancestor"]]
    assert mb and all(c[-1].startswith("origin/") for c in mb)
