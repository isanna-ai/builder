"""P0-1: non-probative verify-command denylist in `validate_tasks`.

The live host-verify gate judges each verify command by its EXIT CODE ONLY —
stdout/stderr and `#` comments are discarded. A command that cannot encode a
result in its exit code (`true`, `exit 0`, bare `echo`/`ls`/`cat`) or that
inverts under the gate (a bare zero-hits `grep`, which exits 1 on zero matches
and so FAILS exactly when a deletion/refactor succeeded) is non-probative and
must be flagged. Exercises `validate_tasks` directly (the same function
`tasks.py::run` delegates to via `validate_canonical_artifact`'s
`extra_validation` hook). Shim-safe: no pytest.raises/monkeypatch.
"""

from __future__ import annotations

import os

from scripts._validators.tasks import validate_tasks


def _task(command: str) -> dict:
    return {
        "id": "T1",
        "verify": [{"command": command}],
        "depends_on": [],
        "parallel_with": [],
    }


def _validate_enforced(data: dict) -> list[str]:
    """The denylist is warn-staged by default; force BUILDER_VERIFY_LINT=enforce so a
    non-probative command surfaces as a hard error. Shim-safe env set/restore."""
    prior = os.environ.get("BUILDER_VERIFY_LINT")
    os.environ["BUILDER_VERIFY_LINT"] = "enforce"
    try:
        return validate_tasks(data, "tasks.yaml")
    finally:
        if prior is None:
            os.environ.pop("BUILDER_VERIFY_LINT", None)
        else:
            os.environ["BUILDER_VERIFY_LINT"] = prior


def _verify_errors(command: str) -> list[str]:
    """Only the verify-lint errors for the single task under test (enforce mode)."""
    errors = _validate_enforced({"tasks": [_task(command)]})
    return [e for e in errors if ".verify[" in e]


# --- flagged: non-probative commands -----------------------------------------


def test_true_is_flagged() -> None:
    assert _verify_errors("true"), "`true` always exits 0 — proves nothing"


def test_exit_zero_is_flagged() -> None:
    assert _verify_errors("exit 0")


def test_bare_echo_is_flagged() -> None:
    assert _verify_errors("echo done")


def test_bare_zero_hits_grep_is_flagged() -> None:
    # The instruction's `bare grep 'x' src -> flagged` case, made faithful to the
    # denylist wording ("a bare grep used for a ZERO-HITS assertion"): a bare grep
    # that reads as an absence sweep. It inverts under the exit-code gate and must
    # be negated (`! grep`).
    errors = _verify_errors("grep -r 'OldSymbol' src/ tests/  # must return zero hits")
    assert errors
    assert any("! grep" in e for e in errors)


# --- ok: probative commands ---------------------------------------------------


def test_negated_grep_is_ok() -> None:
    assert not _verify_errors("! grep -r 'OldSymbol' src/ tests/")


def test_real_focused_test_command_is_ok() -> None:
    assert not _verify_errors("pytest tests/test_x.py")


def test_bare_presence_grep_is_ok() -> None:
    # A bare grep with no zero-hits intent is a PRESENCE assertion: it exits 0 when
    # the string is found, which is a valid exit-code result. (Matches the live
    # `structured` fixture's `grep -n "tasks.yaml" <file>` verify commands.)
    assert not _verify_errors('grep -n "tasks.yaml" /abs/path/contract.md')


def test_piped_grep_q_output_assertion_is_ok() -> None:
    assert not _verify_errors("cat report.txt | grep -q PASS")


def test_chained_real_command_is_ok() -> None:
    assert not _verify_errors("cd /path/to/project && deno task check && deno task test:unit")


# --- empty verify -------------------------------------------------------------


def test_empty_verify_list_is_flagged() -> None:
    data = {"tasks": [{"id": "T1", "verify": [], "depends_on": [], "parallel_with": []}]}
    errors = _validate_enforced(data)
    assert any("verify must include at least one command" in e for e in errors)


def test_empty_command_string_is_flagged() -> None:
    assert _verify_errors("   ")


# --- composition: failure-swallowing tails must be flagged (H2 false-pass fix) ---


def test_or_true_tail_is_flagged() -> None:
    assert _verify_errors("pytest tests/test_x.py || true")


def test_semicolon_exit_zero_tail_is_flagged() -> None:
    assert _verify_errors("pytest tests/test_x.py; exit 0")


def test_pipe_to_true_swallows_failure() -> None:
    assert _verify_errors("pytest tests/ | tee log; true")


# --- false-block fixes: legitimate commands must NOT be flagged (H3/H4) ----------


def test_ls_with_path_is_ok() -> None:
    # `ls <path>` encodes existence in exit code (non-zero if missing) — probative.
    assert not _verify_errors("ls dist/build.js")


def test_cat_with_path_is_ok() -> None:
    assert not _verify_errors("cat artifacts/report.json")


def test_presence_grep_with_marker_in_quoted_pattern_is_ok() -> None:
    # The marker word lives inside the SEARCHED-FOR string (quoted), so this is a
    # presence assertion, not a zero-hits sweep — must not be flagged/inverted.
    assert not _verify_errors("grep -q 'must not be called twice' src/guard.py")


def test_human_gated_operator_sentinel_is_ok() -> None:
    # Operator-gated tasks legitimately use echo sentinels as non-machine evidence.
    data = {"tasks": [{"id": "T1", "human_gate": True,
                       "verify": [{"command": "echo 'OPERATOR-GATED: live dry-run'"}],
                       "depends_on": [], "parallel_with": []}]}
    assert not [e for e in _validate_enforced(data) if ".verify[" in e]


def test_denylist_is_warn_by_default_not_blocking() -> None:
    # Staging contract: under the default (BUILDER_VERIFY_LINT unset/warn), a
    # non-probative command is advisory only, not a hard error.
    os.environ.pop("BUILDER_VERIFY_LINT", None)
    errors = validate_tasks({"tasks": [_task("true")]}, "tasks.yaml")
    assert not [e for e in errors if ".verify[" in e]


def test_hermeticity_flags_absolute_repo_paths():
    """A check that cannot be RE-RUN later is a receipt, not a check.

    Derived from data, not taste: `isanna model verify` re-ran every check every spec in this repo
    ever wrote. 134 failed -- and ALL 134 hardcoded an absolute repo path (66 also hardcoded a `cd`).
    They fail in a worktree, in CI, on a laptop, and for anyone who installs this. The accumulated
    corpus of checks therefore could NOT answer "what still works?" -- the SSOT's fuel was
    contaminated at the source.

    The host runs verify commands with cwd=<project root>. Express them relatively.
    """
    from _validators.tasks import _non_hermetic_reason as reason

    # real commands harvested from this repo's own spec history
    assert reason("cd /path/to/project && bash .builder/tests/x.sh") is not None
    assert reason("python3 /path/to/project/scripts/_validators/prompt_budget.py --selftest") is not None
    assert reason("TMP=$(mktemp -d) && sh install.sh") is None

    # portable, re-runnable commands must NOT be flagged (a rule that cries wolf gets muted)
    assert reason("PYTHONPATH=scripts python3 -m pytest tests/ -q") is None
    assert reason("bash tests/test_installer_assets.sh") is None
    assert reason("mktemp -d && ./run.sh") is None
    assert reason("") is None  # emptiness is the denylist's business


def test_non_hermetic_reason_flags_absolute_directory_changes_and_paths():
    from _validators.tasks import _non_hermetic_reason as reason

    for command in ("env -C /tmp pwd", "make -C /opt/x test", "pushd /tmp", "cat /etc/hosts"):
        assert reason(command) is not None

    assert reason("curl https://localhost:8000/health -q") is None
    assert reason("wget http://x/y") is None
    assert reason("make -C subdir test") is None
    assert reason("python3 -m pytest tests/ -q") is None


def test_non_hermetic_reason_does_not_scan_ripgrep_glob_and_pattern_values():
    """A ripgrep exclusion glob (`-g`/`--glob`/`--iglob`) or explicit pattern
    (`-e`/`--regexp`) is not a filesystem path -- `!**/*.test.*` has a `/` in it only
    because it's glob syntax. A real false positive, harvested from a production repo's
    specs: a bare `/`-leading search PATTERN, and repeated
    `-g` exclusion globs, both tripped the absolute-path scan for having a `/` not
    preceded by a word/slash/dot character -- with no filesystem path anywhere in the
    command and no `cd`.
    """
    from _validators.tasks import _non_hermetic_reason as reason

    # minimal repro from the bug report: no path, no absolute anything -- just a glob
    assert reason("rg -n 'needle' apps/web -g '!**/*.test.*'") is None
    assert reason('rg -n -i "needle" --glob="!**/*.test.*"') is None
    assert reason("rg -n 'needle' apps/web --iglob '!**/*.snap'") is None
    assert reason("rg -e '/needle' apps/web") is None
    assert reason("rg --regexp='/needle' apps/web") is None

    # the two real commands this was blocking
    assert reason(
        "rg -n -i \"/\\s?100\\b|ATS\\s*%|ATS percentage|recruiter compatibility\" "
        "-g '!**/*.test.*' -g '!**/*.snap' apps/web/components apps/web/app "
        "packages/ui/src/forms"
    ) is None
    assert reason(
        "rg -n '/100\\b|ATS %|ATS percentage|recruiter compatibility' apps/web "
        "packages/funnel packages/delivery -g '!**/*.test.*'; test $? -eq 1"
    ) is None

    # the guard's real red path must survive: a genuine absolute path, even inside an
    # `rg` invocation, is still a hardcoded location and must still be refused -- only
    # ripgrep's OWN glob/pattern argument values are exempt, never a PATH argument to it.
    assert reason("rg -n 'needle' /etc/passwd") is not None
    assert reason("cd /Users/example/projects/demo-app && pnpm test") is not None  # publish-ok: fabricated home path, red-path fixture for the hardcoded-location guard
    assert reason("pytest /abs/path/test_x.py") is not None
    assert reason("~/scripts/run.sh") is not None
    assert reason("$HOME/x") is not None


def test_hermeticity_is_staged_warn_by_default():
    """The existing corpus violates this rule wholesale, so it must NOT hard-fail out of the box --
    same staged warn->enforce discipline as every other gate in this project."""
    import os
    from _validators.tasks import _hermetic_lint_enforced

    prev = os.environ.pop("BUILDER_VERIFY_HERMETIC", None)
    try:
        assert _hermetic_lint_enforced() is False, "default must be advisory, not blocking"
        os.environ["BUILDER_VERIFY_HERMETIC"] = "enforce"
        assert _hermetic_lint_enforced() is True
    finally:
        os.environ.pop("BUILDER_VERIFY_HERMETIC", None)
        if prev is not None:
            os.environ["BUILDER_VERIFY_HERMETIC"] = prev
