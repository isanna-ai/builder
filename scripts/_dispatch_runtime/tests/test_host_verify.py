"""R1: host-executed verification gate. A phase is 'complete' only when the spec's verify
commands actually pass host-side — not merely because the agent wrote SUCCEEDED. Staged
via BUILDER_HOST_VERIFY (off default / warn / enforce). Shim-safe: no pytest.raises /
monkeypatch; env via pop/restore; the subprocess is an injected runner."""

from __future__ import annotations

import os
import types
from pathlib import Path

from _dispatch_runtime.lane_common import (
    SessionState,
    Work,
    _collect_verify_commands,
    _combine_host_gates,
    _git_committed_source_paths,
    _git_head,
    _git_source_paths,
    _host_verify_gate,
    _looks_like_test,
    _source_diff_gate,
    finalize_turn,
)
from _dispatch_runtime.phase_runtime import (
    SpecSnapshot,
    ValidationResult,
    build_phase_goal,
    capture_spec_snapshot,
    decide_post_turn,
    validate_phase_completion,
)

_ENV = "BUILDER_HOST_VERIFY"


def _work(tmp_path, *, runner_task_ref=None, spec_id="demo", phase="verify"):
    return Work(
        work_id="w1", spec_id=spec_id, phase=phase,
        project_dir=tmp_path, specs_dir=tmp_path / ".builder" / "specs",
        runner_task_ref=runner_task_ref, capability_class=None,
        queue_root=tmp_path / ".builder" / "dispatch-queue",
        log_path=tmp_path / "log",
    )


def _with_env(value, fn):
    saved = os.environ.get(_ENV)
    if value is None:
        os.environ.pop(_ENV, None)
    else:
        os.environ[_ENV] = value
    try:
        return fn()
    finally:
        if saved is None:
            os.environ.pop(_ENV, None)
        else:
            os.environ[_ENV] = saved


# --- command collection -----------------------------------------------------

def test_collect_from_task_packet(tmp_path):
    ref = ".builder/specs/demo/runs/task-1.yaml"
    p = tmp_path / ref
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("task_id: t1\nverify_commands:\n  - echo ok\n  - pytest -q\n", encoding="utf-8")
    assert _collect_verify_commands(_work(tmp_path, runner_task_ref=ref)) == ["echo ok", "pytest -q"]


def test_collect_from_setup_decisions(tmp_path):
    sd = tmp_path / ".builder" / "setup-decisions.yaml"
    sd.parent.mkdir(parents=True, exist_ok=True)
    sd.write_text("commands:\n  default:\n    test: npm test\n    check: npm run lint\n", encoding="utf-8")
    assert _collect_verify_commands(_work(tmp_path)) == ["npm test", "npm run lint"]


def test_collect_unions_per_spec_and_project_setup_decisions(tmp_path):
    # REGRESSION: a per-spec setup-decisions.yaml used to SHADOW the project-level command
    # map entirely (first-found-wins) -- a spec that declared only a narrow per-spec command
    # silently dropped the project's real verify commands instead of adding to them. Both
    # must now be read; order is packet, per-spec, project; dedup drops any overlap.
    project_sd = tmp_path / ".builder" / "setup-decisions.yaml"
    project_sd.parent.mkdir(parents=True, exist_ok=True)
    project_sd.write_text("commands:\n  default:\n    test: npm test\n    check: npm run lint\n", encoding="utf-8")
    spec_sd = tmp_path / ".builder" / "specs" / "demo" / "setup-decisions.yaml"
    spec_sd.parent.mkdir(parents=True, exist_ok=True)
    spec_sd.write_text("commands:\n  default:\n    test: pytest -q\n", encoding="utf-8")
    assert _collect_verify_commands(_work(tmp_path, spec_id="demo")) == [
        "pytest -q", "npm test", "npm run lint",
    ]


def test_collect_unions_dedupes_overlap(tmp_path):
    project_sd = tmp_path / ".builder" / "setup-decisions.yaml"
    project_sd.parent.mkdir(parents=True, exist_ok=True)
    project_sd.write_text("commands:\n  default:\n    test: pytest -q\n", encoding="utf-8")
    spec_sd = tmp_path / ".builder" / "specs" / "demo" / "setup-decisions.yaml"
    spec_sd.parent.mkdir(parents=True, exist_ok=True)
    spec_sd.write_text("commands:\n  default:\n    test: pytest -q\n", encoding="utf-8")
    assert _collect_verify_commands(_work(tmp_path, spec_id="demo")) == ["pytest -q"]


def test_collect_is_shape_safe(tmp_path):
    # A malformed `verify_commands: pytest` (string, not list) must be ONE command,
    # never iterated char-by-char; a non-mapping packet must not raise.
    ref = ".builder/specs/demo/runs/task-1.yaml"
    p = tmp_path / ref
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("task_id: t1\nverify_commands: pytest -q\n", encoding="utf-8")
    assert _collect_verify_commands(_work(tmp_path, runner_task_ref=ref)) == ["pytest -q"]
    # a bare-list / non-dict setup-decisions must not raise either
    sd = tmp_path / ".builder" / "setup-decisions.yaml"
    sd.write_text("commands: not-a-mapping\n", encoding="utf-8")
    assert _collect_verify_commands(_work(tmp_path, runner_task_ref=ref)) == ["pytest -q"]


# --- the spec's OWN acceptance commands -------------------------------------
#
# A spec that has been planned but never dispatched has no runner packet, so packet
# collection yields nothing and the caller falls through to the project-wide
# setup-decisions defaults -- commands IDENTICAL for every spec in the repo. Anything
# built on that can only make claims about the repository, never about one spec. The
# spec's real acceptance commands have existed since plan time in `tasks.yaml`; these
# tests cover reading them WITHOUT moving what the live gate collects.


def _tasks_yaml(tmp_path, body, spec_id="demo"):
    p = tmp_path / ".builder" / "specs" / spec_id / "tasks.yaml"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    return p


def test_collect_default_ignores_the_specs_own_tasks_yaml(tmp_path):
    # THE INVARIANT THAT PROTECTS THE LIVE GATE: every dispatcher call site uses the
    # default argument, so the in-flight collection must be exactly what it was before
    # tasks.yaml became readable at all. If this test ever goes green by returning the
    # tasks.yaml command too, the change has silently moved what passes mid-flight.
    sd = tmp_path / ".builder" / "setup-decisions.yaml"
    sd.parent.mkdir(parents=True, exist_ok=True)
    sd.write_text("commands:\n  default:\n    test: npm test\n", encoding="utf-8")
    _tasks_yaml(tmp_path, "tasks:\n  - id: T1\n    verify:\n      - command: pytest tests/spec_only.py\n")
    assert _collect_verify_commands(_work(tmp_path)) == ["npm test"]


def test_collect_reads_tasks_yaml_only_when_the_caller_opts_in(tmp_path):
    sd = tmp_path / ".builder" / "setup-decisions.yaml"
    sd.parent.mkdir(parents=True, exist_ok=True)
    sd.write_text("commands:\n  default:\n    test: npm test\n", encoding="utf-8")
    _tasks_yaml(
        tmp_path,
        "tasks:\n"
        "  - id: T1\n    verify:\n      - command: pytest a.py\n      - command: pytest b.py\n"
        "  - id: T2\n    verify:\n      - command: pytest c.py\n",
    )
    assert _collect_verify_commands(_work(tmp_path), include_spec_tasks=True) == [
        "pytest a.py", "pytest b.py", "pytest c.py", "npm test",
    ]


def test_spec_scoped_commands_exclude_the_project_defaults(tmp_path):
    # The whole point of the spec-scoped set: a caller asking about ONE spec must be able
    # to tell whether it has any evidence of its own. Folding the project defaults in here
    # would make that question unanswerable -- every spec would look like it had commands.
    from _dispatch_runtime.lane_common import _spec_scoped_verify_commands

    sd = tmp_path / ".builder" / "setup-decisions.yaml"
    sd.parent.mkdir(parents=True, exist_ok=True)
    sd.write_text("commands:\n  default:\n    test: npm test\n    check: npm run lint\n", encoding="utf-8")
    _tasks_yaml(tmp_path, "tasks:\n  - id: T1\n    verify:\n      - command: pytest a.py\n")
    assert _spec_scoped_verify_commands(_work(tmp_path)) == ["pytest a.py"]


def test_spec_scoped_commands_are_empty_when_the_spec_declares_none(tmp_path):
    from _dispatch_runtime.lane_common import _spec_scoped_verify_commands

    sd = tmp_path / ".builder" / "setup-decisions.yaml"
    sd.parent.mkdir(parents=True, exist_ok=True)
    sd.write_text("commands:\n  default:\n    test: npm test\n", encoding="utf-8")
    (tmp_path / ".builder" / "specs" / "demo").mkdir(parents=True, exist_ok=True)
    assert _spec_scoped_verify_commands(_work(tmp_path)) == []


def test_spec_scoped_commands_prefer_a_bound_packet_over_tasks_yaml(tmp_path):
    # A turn in flight is judged against the packet it was dispatched with, never against
    # plan-time commands that may have been re-planned since. The packet stays the single
    # in-flight truth; tasks.yaml is the pre-dispatch fallback and nothing more.
    from _dispatch_runtime.lane_common import _spec_scoped_verify_commands

    ref = _packet(tmp_path, ["echo from-packet"])
    _tasks_yaml(tmp_path, "tasks:\n  - id: T1\n    verify:\n      - command: echo from-tasks-yaml\n")
    assert _spec_scoped_verify_commands(_work(tmp_path, runner_task_ref=ref)) == ["echo from-packet"]
    assert _collect_verify_commands(_work(tmp_path, runner_task_ref=ref), include_spec_tasks=True) == [
        "echo from-packet",
    ]


def test_a_bound_but_empty_packet_does_not_fall_back_to_tasks_yaml(tmp_path):
    # Found by independent adversarial review. `_packet_verify_commands(work) or
    # _tasks_yaml_verify_commands(work)` treated a BOUND packet that declares no commands as
    # if no packet were bound, and silently judged the turn against plan-time tasks.yaml --
    # commands it was never dispatched with. Boundness is now asked directly.
    from _dispatch_runtime.lane_common import _spec_scoped_verify_commands

    ref = _packet(tmp_path, [])  # a real packet, bound to the turn, declaring nothing
    _tasks_yaml(tmp_path, "tasks:\n  - id: T1\n    verify:\n      - command: echo from-tasks-yaml\n")
    work = _work(tmp_path, runner_task_ref=ref)
    assert _spec_scoped_verify_commands(work) == []
    assert _collect_verify_commands(work, include_spec_tasks=True) == []


def test_spec_scoped_commands_are_shape_safe(tmp_path):
    # Malformed input must degrade to "no evidence" -- which the caller turns into a
    # refusal -- and never to a traceback or, worse, a green.
    from _dispatch_runtime.lane_common import _spec_scoped_verify_commands

    (tmp_path / ".builder" / "specs" / "demo").mkdir(parents=True, exist_ok=True)
    assert _spec_scoped_verify_commands(_work(tmp_path)) == []
    _tasks_yaml(tmp_path, "tasks: not-a-list\n")
    assert _spec_scoped_verify_commands(_work(tmp_path)) == []
    _tasks_yaml(tmp_path, "tasks:\n  - id: T1\n    verify: pytest -q\n")
    assert _spec_scoped_verify_commands(_work(tmp_path)) == ["pytest -q"]
    _tasks_yaml(tmp_path, "tasks:\n  - id: T1\n    verify:\n      - command: [1, 2]\n")
    assert _spec_scoped_verify_commands(_work(tmp_path)) == []
    _tasks_yaml(tmp_path, "tasks:\n  - id: T1\n    verify:\n      - command: pytest a.py\n      - command: pytest a.py\n")
    assert _spec_scoped_verify_commands(_work(tmp_path)) == ["pytest a.py"]


# --- R4: the live gate stays unchanged, mechanically ---------------------------
#
# Phase-3 review finding F4 (RED). R4 claims "the live dispatcher gate is bit-for-bit
# unchanged", but the guard for it pinned the FUNCTION DEFAULT of `_collect_verify_commands`
# — not the CALL SITES. `include_spec_tasks` is passed only from isanna.py (the spec-scoped
# CLI) and from these tests, which is correct today. But if a future edit wired
# `include_spec_tasks=True` into a dispatcher path, the live in-flight gate would silently
# start collecting plan-time tasks.yaml commands and EVERY existing test would still pass:
# they all exercise the function directly, never the callers.
#
# The original commit asserted "no dispatcher call site passes it" as though guaranteed, when
# it was only true by inspection at one moment. This makes it mechanical.


_CALL_RE = r"_collect_verify_commands\s*\([^)]*include_spec_tasks"
_DEF_RE = r"def\s+_collect_verify_commands\s*\([^)]*\)"


def _scan_source_for_optin(text: str) -> list[int]:
    """1-based line numbers of CALLS to _collect_verify_commands passing the opt-in keyword.

    Scans the whole file text rather than line by line. Independent review demonstrated the
    line-by-line version was defeated by a call the formatter had wrapped:

        _collect_verify_commands(
            work, include_spec_tasks=True)

    -- neither line matches on its own, so a purely cosmetic line-wrap disabled the guard.
    `[^)]*` already spans newlines; only the per-line iteration prevented it from matching.

    The definition's own signature has the same shape as a keyword call and is not a call site,
    so it is blanked out FIRST -- with equal-length spaces, so reported line numbers stay true."""
    import re as _re

    text = _re.sub(_DEF_RE, lambda m: " " * len(m.group(0)), text)
    return [text.count("\n", 0, m.start()) + 1 for m in _re.finditer(_CALL_RE, text)]


def _dispatcher_call_sites_passing_the_flag() -> list[str]:
    """Places in dispatcher RUNTIME modules that CALL _collect_verify_commands with the opt-in
    keyword. The definition is not a call site, and the tests directory legitimately passes it,
    so both are excluded. Recursive: a future subpackage under _dispatch_runtime is dispatcher
    runtime too, and the old non-recursive glob would have been blind to it."""
    from pathlib import Path as _Path

    runtime_dir = _Path(__file__).resolve().parent.parent
    hits: list[str] = []
    for path in sorted(runtime_dir.rglob("*.py")):
        if "tests" in path.relative_to(runtime_dir).parts:
            continue
        text = path.read_text(encoding="utf-8")
        lines = text.splitlines()
        for lineno in _scan_source_for_optin(text):
            hits.append(f"{path.name}:{lineno}: {lines[lineno - 1].strip()}")
    return hits


def test_the_optin_is_keyword_only_so_a_positional_call_cannot_reach_it():
    # The source-level guard below matches the KEYWORD form. Independent review demonstrated
    # that `_collect_verify_commands(work, True)` opts the live gate in while reading as an
    # ordinary call, and no regex over the call text can tell it from a legitimate one.
    # Keyword-only makes that shape a TypeError instead -- enforced by the language, not by a
    # pattern that has to anticipate every spelling.
    import inspect

    from _dispatch_runtime.lane_common import _collect_verify_commands

    # The SIGNATURE is the whole assertion, deliberately. An earlier version of this test also
    # called `_collect_verify_commands(work, True)` and asserted TypeError -- which independent
    # review showed was VACUOUS: with the parameter regressed to positional, that call still
    # raises TypeError, just from inside the body (a stub `work` whose paths are None). It would
    # have passed either way. A test that cannot fail for the reason it names is worse than no
    # test, because it reads like coverage.
    param = inspect.signature(_collect_verify_commands).parameters["include_spec_tasks"]
    assert param.kind is inspect.Parameter.KEYWORD_ONLY, (
        "include_spec_tasks became positional; `_collect_verify_commands(work, True)` can now "
        "opt the live gate in while reading as an ordinary call, and no source guard sees it"
    )


def test_no_dispatcher_call_site_opts_into_spec_task_commands():
    # If this fails, the live host-verify gate has started collecting a spec's plan-time
    # tasks.yaml commands mid-flight — which is exactly what R4 promises never happens.
    hits = _dispatcher_call_sites_passing_the_flag()
    assert hits == [], (
        "a dispatcher module now opts into spec task commands, changing the live gate:\n  "
        + "\n  ".join(hits)
    )


def test_the_call_site_guard_actually_detects_a_violation():
    # The guard above passes today because the invariant already holds, so on its own it
    # cannot distinguish "invariant held" from "regex never matches anything". This pins the
    # detector itself against planted violations.
    single = "x = 1\ncmds = _collect_verify_commands(work, include_spec_tasks=True)\n"
    assert _scan_source_for_optin(single) == [2], "would not catch a single-line violation"

    # The line-wrapped form that defeated the first version of this detector.
    wrapped = (
        "x = 1\n"
        "cmds = _collect_verify_commands(\n"
        "    work,\n"
        "    include_spec_tasks=True,\n"
        ")\n"
    )
    assert _scan_source_for_optin(wrapped) == [2], "would not catch a wrapped violation"

    # A clean call is not a hit, and neither is the definition -- including when the signature
    # is itself wrapped across lines, which is what the blanking step has to survive.
    assert _scan_source_for_optin("cmds = _collect_verify_commands(work)\n") == []
    assert _scan_source_for_optin(
        "def _collect_verify_commands(\n    work: Work, *, include_spec_tasks: bool = False\n) -> list:\n"
    ) == []


# --- the gate ---------------------------------------------------------------

def _runner(rc_map):
    """verify_runner(cmd, cwd) -> returncode from rc_map (default 0)."""
    return lambda cmd, cwd: rc_map.get(cmd, 0)


def _packet(tmp_path, commands):
    ref = ".builder/specs/demo/runs/task-1.yaml"
    p = tmp_path / ref
    p.parent.mkdir(parents=True, exist_ok=True)
    body = "task_id: t1\nverify_commands:\n" + "".join(f"  - {c}\n" for c in commands)
    p.write_text(body, encoding="utf-8")
    return ref


def test_gate_enforces_by_default(tmp_path):
    # THE DEFAULT IS THE PRODUCT. With the flag unset, a failing verify command BLOCKS the
    # phase -- an unconfigured install must not hand out unearned "verified" stamps.
    ref = _packet(tmp_path, ["pytest"])
    passed, reason = _with_env(None, lambda: _host_verify_gate(
        _work(tmp_path, runner_task_ref=ref), "verify", verify_runner=_runner({"pytest": 1})))
    assert passed is False and reason == "host verify failed (1/1): pytest"


def test_gate_off_is_explicit_opt_out(tmp_path):
    # Turning the gate off is still supported -- but it has to be ASKED for, by name.
    ref = _packet(tmp_path, ["pytest"])
    passed, reason = _with_env("off", lambda: _host_verify_gate(
        _work(tmp_path, runner_task_ref=ref), "verify", verify_runner=_runner({})))
    assert passed is None and reason == ""


def test_gate_typo_does_not_silently_disable(tmp_path):
    # `BUILDER_HOST_VERIFY=enfroce` used to mean NO GATE AT ALL. A misspelling must never be
    # the thing that removes the guarantee: an unrecognized value falls back to the default.
    ref = _packet(tmp_path, ["pytest"])
    passed, reason = _with_env("enfroce", lambda: _host_verify_gate(
        _work(tmp_path, runner_task_ref=ref), "verify", verify_runner=_runner({"pytest": 1})))
    assert passed is False and reason == "host verify failed (1/1): pytest"


def test_gate_enforce_pass(tmp_path):
    ref = _packet(tmp_path, ["pytest"])
    passed, reason = _with_env("enforce", lambda: _host_verify_gate(
        _work(tmp_path, runner_task_ref=ref), "verify", verify_runner=_runner({"pytest": 0})))
    assert passed is True and reason == ""


def test_gate_enforce_fail_blocks(tmp_path):
    ref = _packet(tmp_path, ["pytest", "lint"])
    passed, reason = _with_env("enforce", lambda: _host_verify_gate(
        _work(tmp_path, runner_task_ref=ref), "verify", verify_runner=_runner({"pytest": 1})))
    assert passed is False
    assert "host verify failed" in reason and "pytest" in reason


def test_gate_warn_never_blocks(tmp_path):
    ref = _packet(tmp_path, ["pytest"])
    passed, reason = _with_env("warn", lambda: _host_verify_gate(
        _work(tmp_path, runner_task_ref=ref), "verify", verify_runner=_runner({"pytest": 1})))
    assert passed is None                    # warn never returns False (no block)
    assert reason.startswith("[warn]")


def test_gate_skips_non_gated_phase(tmp_path):
    ref = _packet(tmp_path, ["pytest"])
    passed, reason = _with_env("enforce", lambda: _host_verify_gate(
        _work(tmp_path, runner_task_ref=ref), "plan", verify_runner=_runner({"pytest": 1})))
    assert passed is None and reason == ""   # plan is not a host-verify phase


def test_gate_no_commands_cannot_gate(tmp_path):
    # REQUIRE_COMMANDS defaults to '1' (B5b, fail-closed) -- explicit '0' opt-out here to
    # exercise the "zero commands -> cannot gate" abstain path this test is actually about.
    saved = os.environ.get("BUILDER_HOST_VERIFY_REQUIRE_COMMANDS")
    os.environ["BUILDER_HOST_VERIFY_REQUIRE_COMMANDS"] = "0"
    try:
        passed, reason = _with_env("enforce", lambda: _host_verify_gate(
            _work(tmp_path), "verify", verify_runner=_runner({})))
    finally:
        if saved is None:
            os.environ.pop("BUILDER_HOST_VERIFY_REQUIRE_COMMANDS", None)
        else:
            os.environ["BUILDER_HOST_VERIFY_REQUIRE_COMMANDS"] = saved
    assert passed is None and reason == ""


def test_gate_no_commands_defaults_to_blocking(tmp_path):
    # The B5b flip itself: with REQUIRE_COMMANDS unset (default), zero verify commands on a
    # gated phase must BLOCK (fail:unverifiable), never silently abstain.
    saved = os.environ.pop("BUILDER_HOST_VERIFY_REQUIRE_COMMANDS", None)
    try:
        passed, reason = _with_env("enforce", lambda: _host_verify_gate(
            _work(tmp_path), "verify", verify_runner=_runner({})))
    finally:
        if saved is not None:
            os.environ["BUILDER_HOST_VERIFY_REQUIRE_COMMANDS"] = saved
    assert passed is False
    assert "unverifiable" in reason


def test_bounded_runner_real_subprocess(tmp_path):
    # Exercises the actual production path (Popen + wait + process-group reap) with instant,
    # deterministic shell commands (no injected runner).
    from _dispatch_runtime.lane_common import _run_verify_commands_bounded
    assert _run_verify_commands_bounded(["true", "exit 0"], str(tmp_path)) == []
    assert _run_verify_commands_bounded(["exit 3"], str(tmp_path)) == ["exit 3"]


# --- decide_post_turn host-gate integration ---------------------------------

def _snap(fp="a"):
    return SpecSnapshot(spec_id="s", phase="verify", fingerprint=fp, file_count=1,
                        phase_log_count=1, latest_phase_outcome="SUCCEEDED",
                        spec_status="verified", spec_current_phase="verify")


def _val(passed, outcome="SUCCEEDED", reason="ok"):
    return ValidationResult(passed, outcome, reason)


def test_decide_complete_when_no_host_gate(tmp_path):
    d = decide_post_turn({}, _snap(), _snap(), _val(False), _val(True), 0, 3)
    assert d.outcome == "phase-complete"     # prior behavior preserved (host_gate None)


def test_decide_complete_when_host_gate_passed(tmp_path):
    d = decide_post_turn({}, _snap(), _snap(), _val(False), _val(True), 0, 3, host_gate_passed=True)
    assert d.outcome == "phase-complete"


def test_decide_not_complete_when_host_gate_failed(tmp_path):
    # Artifacts say done, but host verify failed -> the agent cannot self-certify.
    d = decide_post_turn({}, _snap("a"), _snap("b"), _val(False), _val(True), 0, 3,
                         host_gate_passed=False, host_gate_reason="host verify failed: pytest")
    assert d.outcome != "phase-complete"
    assert "host verify failed" in d.reason


def test_decide_host_gate_failed_escalates_at_budget(tmp_path):
    d = decide_post_turn({}, _snap("a"), _snap("a"), _val(True), _val(True), 3, 3,
                         host_gate_passed=False, host_gate_reason="fail")
    assert d.outcome == "stale-escalate"


# --- R2: source-diff gate (pre-turn baseline) -------------------------------

def _git(porcelain, *, head="", diff=""):
    """git_runner(args, cwd) stub dispatching by args[0] (R2 issues multiple git
    subcommands: status/rev-parse/diff — a single ignore-args lambda can no longer serve
    all of them): 'status' -> the porcelain string, 'rev-parse' -> the HEAD sha, 'diff' ->
    a newline-joined path list. Ignores cwd. Defaults reproduce the pre-R2 shape (empty
    head/diff), so callers that only care about the porcelain need not change."""
    def run(args, cwd):
        sub = args[0] if args else ""
        if sub == "status":
            return porcelain
        if sub == "rev-parse":
            return head
        if sub == "diff":
            return diff
        return ""
    return run


def _git_err(stdout="", rc=128):
    return lambda args, cwd: types.SimpleNamespace(stdout=stdout, returncode=rc)


def _tdd_packet(tmp_path):
    ref = ".builder/specs/demo/runs/task-1.yaml"
    p = tmp_path / ref
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("task_id: t1\ntdd_mode: required\n", encoding="utf-8")
    return ref


def test_git_source_paths_filters_specpilot_and_handles_rename(tmp_path):
    porcelain = " M src/app.py\n?? new.py\nR  src/old.py -> src/renamed.py\n M .specpilot/x.yaml\n"
    assert _git_source_paths(tmp_path, git_runner=_git(porcelain)) == {
        "src/app.py", "new.py", "src/old.py", "src/renamed.py"}


def test_git_source_paths_none_on_git_error(tmp_path):
    assert _git_source_paths(tmp_path, git_runner=_git_err(rc=128)) is None


def _sd(tmp_path, *, pre, porcelain, mode="enforce", runner_task_ref=None, pre_head="h0", head="h0"):
    # Default pre_head == current_head ("h0") models a GENUINELY UNCHANGED HEAD, so the fail-open
    # abstain (clean tree + HEAD baseline/current unavailable) does NOT trigger and these tests keep
    # exercising the confident block/pass paths. Tests that want head-advance/unavailable override.
    w = _work(tmp_path, phase="implement", runner_task_ref=runner_task_ref)
    return _with_env(mode, lambda: _source_diff_gate(
        w, "implement", pre_source_paths=pre, pre_head=pre_head, git_runner=_git(porcelain, head=head)))


def test_source_diff_no_baseline_returns_none(tmp_path):
    passed, _ = _sd(tmp_path, pre=None, porcelain=" M src/app.py\n")
    assert passed is None  # no baseline captured -> fail safe (no block)


def test_source_diff_pass_on_new_change(tmp_path):
    passed, _ = _sd(tmp_path, pre=set(), porcelain=" M src/app.py\n")
    assert passed is True


def test_source_diff_fail_when_no_new_change_vs_baseline(tmp_path):
    # src/app.py was ALREADY dirty pre-turn -> this turn added no new source.
    passed, reason = _sd(tmp_path, pre={"src/app.py"}, porcelain=" M src/app.py\n")
    assert passed is False and "no NEW source" in reason


def test_source_diff_fail_when_only_specpilot_changed(tmp_path):
    passed, reason = _sd(tmp_path, pre=set(), porcelain=" M .specpilot/specs/demo/phase-log.yaml\n")
    assert passed is False and "no NEW source" in reason


def test_source_diff_git_unavailable_returns_none(tmp_path):
    w = _work(tmp_path, phase="implement")
    passed, _ = _with_env("enforce", lambda: _source_diff_gate(
        w, "implement", pre_source_paths=set(), git_runner=_git_err(rc=128)))
    assert passed is None  # git error -> no false block


def test_source_diff_tdd_requires_a_test_file(tmp_path):
    passed, reason = _sd(tmp_path, pre=set(), porcelain=" M src/app.py\n", runner_task_ref=_tdd_packet(tmp_path))
    assert passed is False and "test file" in reason


def test_source_diff_tdd_satisfied_by_test_change(tmp_path):
    passed, _ = _sd(tmp_path, pre=set(), porcelain=" M src/app.py\n M tests/test_app.py\n",
                    runner_task_ref=_tdd_packet(tmp_path))
    assert passed is True


def test_source_diff_warn_never_blocks(tmp_path):
    passed, reason = _sd(tmp_path, pre=set(), porcelain="", mode="warn")
    assert passed is None and reason.startswith("[warn]")


def test_source_diff_skips_non_implement_phase(tmp_path):
    w = _work(tmp_path, phase="verify")
    passed, _ = _with_env("enforce", lambda: _source_diff_gate(
        w, "verify", pre_source_paths=set(), git_runner=_git("")))
    assert passed is None


# --- R2: HEAD-advance (committed work) --------------------------------------

def test_git_head_returns_sha(tmp_path):
    assert _git_head(tmp_path, git_runner=_git("", head="deadbeef\n")) == "deadbeef"


def test_git_head_none_on_git_error(tmp_path):
    assert _git_head(tmp_path, git_runner=_git_err(rc=128)) is None


def test_git_committed_source_paths_filters_specpilot(tmp_path):
    diff = "src/app.py\n.specpilot/specs/demo/phase-log.yaml\n"
    assert _git_committed_source_paths(tmp_path, "aaa", "bbb", git_runner=_git("", diff=diff)) == {"src/app.py"}


def test_git_committed_source_paths_none_on_git_error(tmp_path):
    # None (distinct "unavailable"), NOT an empty set, so the gate can ABSTAIN (fail open)
    # rather than mistake missing evidence for a definite empty change-set and false-block.
    assert _git_committed_source_paths(tmp_path, "aaa", "bbb", git_runner=_git_err(rc=128)) is None


def test_git_committed_source_paths_empty_set_on_clean_success(tmp_path):
    # A successful diff with no matching (non-.specpilot) paths is an empty SET, not None.
    assert _git_committed_source_paths(tmp_path, "aaa", "bbb", git_runner=_git("", diff="")) == set()


def _sd_head(tmp_path, *, pre_head, head, diff, mode="enforce", runner_task_ref=None):
    w = _work(tmp_path, phase="implement", runner_task_ref=runner_task_ref)
    return _with_env(mode, lambda: _source_diff_gate(
        w, "implement", pre_source_paths=set(), pre_head=pre_head,
        git_runner=_git("", head=head, diff=diff)))


def test_source_diff_head_advanced_with_committed_source_passes(tmp_path):
    # Clean working tree (the agent COMMITTED in-turn) but HEAD moved and the commit
    # touched real source outside .specpilot/ -> must pass, not false-block.
    passed, _ = _sd_head(tmp_path, pre_head="aaa", head="bbb", diff="src/app.py\n")
    assert passed is True


def test_source_diff_head_advanced_but_only_specpilot_committed_fails(tmp_path):
    passed, reason = _sd_head(
        tmp_path, pre_head="aaa", head="bbb", diff=".specpilot/specs/demo/phase-log.yaml\n")
    assert passed is False and "no NEW source" in reason


def test_source_diff_tdd_satisfied_by_committed_test_file(tmp_path):
    ref = _tdd_packet(tmp_path)
    passed, _ = _sd_head(
        tmp_path, pre_head="aaa", head="bbb", diff="src/app.py\ntests/test_app.py\n",
        runner_task_ref=ref)
    assert passed is True


def test_source_diff_no_uncommitted_and_no_head_advance_fails(tmp_path):
    # Clean tree AND HEAD unchanged (pre_head == current_head) -> genuinely no work done.
    passed, reason = _sd_head(tmp_path, pre_head="aaa", head="aaa", diff="src/app.py\n")
    assert passed is False and "no NEW source" in reason


def test_source_diff_head_rev_parse_error_falls_back_to_uncommitted(tmp_path):
    # rev-parse errors (nonzero rc, CompletedProcess-shaped) -> _git_head returns None ->
    # head_advanced is False, but the gate still evaluates the uncommitted delta normally
    # (no crash, no false block) — mirrors _git_source_paths' string-OR-CompletedProcess
    # runner-shape handling.
    def runner(args, cwd):
        if args and args[0] == "status":
            return " M src/app.py\n"
        return types.SimpleNamespace(stdout="", returncode=128)
    w = _work(tmp_path, phase="implement")
    passed, _ = _with_env("enforce", lambda: _source_diff_gate(
        w, "implement", pre_source_paths=set(), pre_head="aaa", git_runner=runner))
    assert passed is True  # uncommitted new_paths alone satisfies the gate


def test_source_diff_committed_diff_unavailable_abstains(tmp_path):
    # Clean tree + HEAD advanced, but the committed-file diff is UNAVAILABLE (errors) while
    # status + rev-parse succeed. The gate must ABSTAIN (fail open) — a genuine committed turn
    # must never false-block just because the diff probe failed.
    def runner(args, cwd):
        sub = args[0] if args else ""
        if sub == "status":
            return ""                                   # clean working tree
        if sub == "rev-parse":
            return "bbb\n"                              # HEAD advanced past pre_head "aaa"
        return types.SimpleNamespace(stdout="", returncode=128)  # diff errors -> unavailable
    w = _work(tmp_path, phase="implement")
    passed, reason = _with_env("enforce", lambda: _source_diff_gate(
        w, "implement", pre_source_paths=set(), pre_head="aaa", git_runner=runner))
    assert passed is None and reason == ""


def test_source_diff_unborn_repo_clean_tree_abstains(tmp_path):
    # Unborn repo / first commit: pre_head is None AND the tree is clean -> no evidence a source
    # change was authored either way -> ABSTAIN (fail open), never a false block.
    w = _work(tmp_path, phase="implement")
    passed, reason = _with_env("enforce", lambda: _source_diff_gate(
        w, "implement", pre_source_paths=set(), pre_head=None, git_runner=_git("", head="")))
    assert passed is None and reason == ""


def test_looks_like_test_is_anchored_not_substring():
    assert _looks_like_test("tests/test_app.py")
    assert _looks_like_test("src/app_test.go")
    assert _looks_like_test("web/app.test.js")
    assert _looks_like_test("pkg/foo.spec.ts")
    assert _looks_like_test("tests/integration.rs")   # test DIR component (no leading slash)
    assert not _looks_like_test("src/latest.py")      # 'test' substring only -> NOT a test
    assert not _looks_like_test("src/special.py")     # 'spec' substring only -> NOT a test
    assert not _looks_like_test("src/app.py")


def test_combine_false_dominates():
    assert _combine_host_gates((True, "x"), (False, "y")) == (False, "y")
    assert _combine_host_gates((None, ""), (None, "")) == (None, "")
    assert _combine_host_gates((True, ""), (None, "")) == (True, "")
    assert _combine_host_gates((None, "note"), (True, "")) == (True, "note")


# --- R7: resume-feedback (persist why the host gate failed; surface it once) -

_RECALL_ENV_KEYS = ("MEMORY_RECALL_MODE", "HIVEMIND_MCP_URL", "HIVEMIND_API_KEY")


def _force_recall_off():
    """Same idiom as test_plan_gate_hold.py's _force_recall_off: build_phase_goal must not
    touch memory_hook / a live hivemind endpoint while these tests build a goal."""
    saved = {k: os.environ.get(k) for k in _RECALL_ENV_KEYS}
    for k in _RECALL_ENV_KEYS:
        os.environ.pop(k, None)
    return saved


def _restore_env(saved):
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def _feedback_spec(tmp_path):
    specs_dir = tmp_path / ".builder" / "specs"
    spec_dir = specs_dir / "demo"
    spec_dir.mkdir(parents=True, exist_ok=True)
    (spec_dir / "spec.yaml").write_text(
        'name: "demo"\nstatus: "implementing"\ncurrent_phase: "implement"\nsummary: "intent"\n',
        encoding="utf-8",
    )
    return specs_dir, spec_dir


def test_build_phase_goal_injects_and_clears_host_verify_feedback(tmp_path):
    specs_dir, spec_dir = _feedback_spec(tmp_path)
    feedback_path = spec_dir / "host-verify-feedback.txt"
    feedback_path.write_text("host verify failed (1/1): pytest -q", encoding="utf-8")

    saved = _force_recall_off()
    try:
        goal = build_phase_goal(tmp_path, specs_dir, "demo", "implement", None)
    finally:
        _restore_env(saved)

    assert "PREVIOUS ATTEMPT FAILED HOST VERIFICATION" in goal
    assert "pytest -q" in goal
    assert not feedback_path.exists()  # one-shot: cleared once read


def test_build_phase_goal_no_feedback_file_injects_nothing(tmp_path):
    specs_dir, _spec_dir = _feedback_spec(tmp_path)
    saved = _force_recall_off()
    try:
        goal = build_phase_goal(tmp_path, specs_dir, "demo", "implement", None)
    finally:
        _restore_env(saved)
    assert "PREVIOUS ATTEMPT FAILED HOST VERIFICATION" not in goal


def test_finalize_turn_persists_feedback_on_host_gate_fail_resume(tmp_path):
    # Wire up a MINIMAL but complete `implement` completion (phase-log + spec.yaml +
    # handoff.yaml + tasks.yaml) so post_validation.passed is True and the host gate
    # actually runs; then fail the source-diff half of it (no new source vs baseline)
    # so decide_post_turn resumes with a host-gate reason, and assert finalize_turn
    # wrote that reason to host-verify-feedback.txt.
    specs_dir = tmp_path / ".builder" / "specs"
    spec_dir = specs_dir / "demo"
    spec_dir.mkdir(parents=True, exist_ok=True)
    (spec_dir / "spec.yaml").write_text(
        'name: "demo"\nstatus: "implementing"\ncurrent_phase: "verify"\n', encoding="utf-8")
    (spec_dir / "tasks.yaml").write_text("tasks: []\n", encoding="utf-8")
    (spec_dir / "handoff.yaml").write_text(
        "next_phase: verify\nspec: demo\nready: true\ncompleted_phase: implement\n", encoding="utf-8")
    (spec_dir / "phase-log.yaml").write_text(
        'phases:\n  - phase: implement\n    completed: "2026-07-10T00:00:00Z"\n    outcome: SUCCEEDED\n',
        encoding="utf-8",
    )

    queue_root = tmp_path / ".builder" / "dispatch-queue"
    work = Work(
        work_id="w-host-fb", spec_id="demo", phase="implement",
        project_dir=tmp_path, specs_dir=specs_dir,
        runner_task_ref=None, capability_class=None,
        queue_root=queue_root, log_path=queue_root / "queue" / "attempts" / "a.log",
    )
    pre_snap = capture_spec_snapshot(work.specs_dir, work.spec_id, work.phase)
    pre_val = validate_phase_completion(work.specs_dir, work.spec_id, work.phase)
    assert pre_val.passed  # sanity: the fixture above IS a valid implement-completion
    exec_result = {"status": "interrupted", "stdout": "", "stderr": "", "returncode": 0, "session_id": "s"}

    from _dispatch_runtime import run_ledger

    saved_ledger = run_ledger.write_run_ledger
    run_ledger.write_run_ledger = lambda *a, **k: False  # never hit the live hivemind wire
    try:
        result = _with_env("enforce", lambda: finalize_turn(
            work, ["claude", "-p", "goal"], exec_result, pre_snap, pre_val, SessionState(),
            lane_name="claude-code-cli",
            # Clean tree + genuinely UNCHANGED HEAD (pre_head == current "h0") -> the gate can
            # confidently block on "no source change" (a None/None HEAD would fail open instead).
            pre_source_paths=set(), pre_head="h0", git_runner=_git("", head="h0"),
        ))
    finally:
        run_ledger.write_run_ledger = saved_ledger

    assert result.metadata["decision"] == "resume-same-session"
    feedback_path = spec_dir / "host-verify-feedback.txt"
    assert feedback_path.exists()
    body = feedback_path.read_text(encoding="utf-8")
    assert body.split("\n", 1)[0] == "implement"   # phase stamped on line 1 (phase-scoping)
    assert "no NEW source" in body


def test_render_feedback_discards_phase_mismatch(tmp_path):
    # Feedback recorded for a DIFFERENT phase (spec advanced/skipped between turns) must be
    # discarded (unlinked) and NOT injected into the phase now running.
    specs_dir, spec_dir = _feedback_spec(tmp_path)
    feedback_path = spec_dir / "host-verify-feedback.txt"
    feedback_path.write_text("verify\nhost verify failed (1/1): pytest -q", encoding="utf-8")

    saved = _force_recall_off()
    try:
        goal = build_phase_goal(tmp_path, specs_dir, "demo", "implement", None)  # running IMPLEMENT
    finally:
        _restore_env(saved)

    assert "PREVIOUS ATTEMPT FAILED HOST VERIFICATION" not in goal  # mismatch -> not injected
    assert not feedback_path.exists()                               # ... but still cleared (one-shot)


def test_render_feedback_injects_on_phase_match_then_clears(tmp_path):
    specs_dir, spec_dir = _feedback_spec(tmp_path)
    feedback_path = spec_dir / "host-verify-feedback.txt"
    feedback_path.write_text("implement\nimplement changed no NEW source file", encoding="utf-8")

    saved = _force_recall_off()
    try:
        goal = build_phase_goal(tmp_path, specs_dir, "demo", "implement", None)
    finally:
        _restore_env(saved)

    assert "PREVIOUS ATTEMPT FAILED HOST VERIFICATION" in goal
    assert "no NEW source" in goal
    # The line-1 phase stamp is consumed, not shown: the injected reason is the post-newline text.
    assert "HOST VERIFICATION: implement changed no NEW source file" in goal
    assert not feedback_path.exists()  # one-shot


def test_build_phase_goal_survives_non_utf8_feedback(tmp_path):
    # A non-UTF-8 feedback file must NOT raise out of build_phase_goal (UnicodeDecodeError is a
    # ValueError; the read guard catches it). The goal still builds, just without the block.
    specs_dir, spec_dir = _feedback_spec(tmp_path)
    feedback_path = spec_dir / "host-verify-feedback.txt"
    feedback_path.write_bytes(b"\xff\xfe\x00bad bytes not utf-8 \x80\x81")

    saved = _force_recall_off()
    try:
        goal = build_phase_goal(tmp_path, specs_dir, "demo", "implement", None)
    finally:
        _restore_env(saved)

    assert isinstance(goal, str) and goal  # built normally, no raise
    assert "PREVIOUS ATTEMPT FAILED HOST VERIFICATION" not in goal
