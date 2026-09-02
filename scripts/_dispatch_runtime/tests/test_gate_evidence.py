from __future__ import annotations

import json
import os
import sys
import time
import types
from pathlib import Path

from _yaml import yaml

from _dispatch_runtime import gate_evidence
from _dispatch_runtime.lane_common import (
    SessionState,
    Work,
    _host_verify_gate,
    _packet_contract_gate,
    _red_baseline_gate,
    _run_verify_commands_bounded,
    _run_verify_commands_detailed,
    _source_diff_gate,
    finalize_turn,
)
from _dispatch_runtime.packet_contract import validate_packet_contract
from _dispatch_runtime.phase_runtime import capture_spec_snapshot, validate_phase_completion


def _work(tmp_path, *, runner_task_ref=None, spec_id="demo", phase="implement", control_root=None):
    root = control_root or tmp_path
    return Work(
        work_id="w1", spec_id=spec_id, phase=phase,
        project_dir=tmp_path, specs_dir=root / ".builder" / "specs",
        runner_task_ref=runner_task_ref, capability_class=None,
        queue_root=root / ".builder" / "dispatch-queue",
        log_path=root / ".builder" / "dispatch-queue" / "queue" / "attempts" / "attempt-a.log",
    )


def _with_envs(values, fn):
    saved = {k: os.environ.get(k) for k in values}
    for k, v in values.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    try:
        return fn()
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _packet(tmp_path, commands=None, *, body_extra="", task_id="T1"):
    ref = ".builder/specs/demo/runs/task-1.yaml"
    p = tmp_path / ref
    p.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"task_id: {task_id}\n"]
    if commands is not None:
        lines.append("verify_commands:\n")
        lines += [f"  - {c}\n" for c in commands]
    if body_extra:
        lines.append(body_extra)
    p.write_text("".join(lines), encoding="utf-8")
    return ref


def _runner(rc_map):
    return lambda cmd, cwd: rc_map.get(cmd, 0)


def _git(porcelain="", *, head="h0\n", diff="", numstat="", patch=""):
    def run(args, cwd):
        sub = args[0] if args else ""
        if sub == "status":
            return porcelain
        if sub == "rev-parse":
            return head
        if sub == "diff" and "--numstat" in args:
            return numstat
        if sub == "diff" and len(args) >= 2 and args[1] == "HEAD":
            return patch
        if sub == "diff":
            return diff
        return ""
    return run


def _impl_spec(tmp_path, *, control_root=None):
    root = control_root or tmp_path
    specs_dir = root / ".builder" / "specs"
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
    return specs_dir, spec_dir


def _bad_impl_spec(tmp_path):
    specs_dir = tmp_path / ".builder" / "specs"
    spec_dir = specs_dir / "demo"
    spec_dir.mkdir(parents=True, exist_ok=True)
    (spec_dir / "spec.yaml").write_text('name: "demo"\nstatus: "implementing"\n', encoding="utf-8")
    return specs_dir, spec_dir


def _finalize(tmp_path, work, *, verify_runner=None, git_runner=None, pre_source_paths=None,
              pre_head=None, control_root=None):
    pre_snap = capture_spec_snapshot(work.specs_dir, work.spec_id, work.phase)
    pre_val = validate_phase_completion(work.specs_dir, work.spec_id, work.phase)
    exec_result = {"status": "interrupted", "stdout": "", "stderr": "", "returncode": 0, "session_id": "s"}
    from _dispatch_runtime import run_ledger

    saved_ledger = run_ledger.write_run_ledger
    run_ledger.write_run_ledger = lambda *a, **k: False
    try:
        return finalize_turn(
            work, ["claude", "-p", "goal"], exec_result, pre_snap, pre_val, SessionState(),
            lane_name="claude-code-cli", verify_runner=verify_runner, git_runner=git_runner,
            pre_source_paths=pre_source_paths, pre_head=pre_head, control_root=control_root,
        )
    finally:
        run_ledger.write_run_ledger = saved_ledger


def _bundle(spec_dir, rel):
    path = rel["path"] if isinstance(rel, dict) else rel
    return yaml.safe_load((spec_dir / path).read_text(encoding="utf-8"))


def _evidence_path(spec_dir, entry):
    return spec_dir / (entry["path"] if isinstance(entry, dict) else entry)


def test_evidence_off_no_metadata_keys(tmp_path):
    specs_dir, spec_dir = _impl_spec(tmp_path)
    ref = _packet(tmp_path, ["pytest"])
    work = _work(tmp_path, runner_task_ref=ref)
    env = {"BUILDER_GATE_EVIDENCE": "off", "BUILDER_HOST_VERIFY": "enforce"}
    result = _with_envs(env, lambda: _finalize(
        tmp_path, work, verify_runner=_runner({"pytest": 1}), git_runner=_git(), pre_source_paths=None))
    assert "gates" not in result.metadata and "gate_evidence" not in result.metadata
    assert not (spec_dir / "gate-evidence").exists()
    assert result.metadata.get("host_verify") == "host verify failed (1/1): pytest"


def test_evidence_on_by_default_writes_a_bundle(tmp_path):
    # Unset -> evidence is WRITTEN. The recorder can only show colour for what the host proves,
    # so an unconfigured install must still leave the proof behind.
    specs_dir, spec_dir = _impl_spec(tmp_path)
    ref = _packet(tmp_path, ["pytest"])
    work = _work(tmp_path, runner_task_ref=ref)
    env = {"BUILDER_GATE_EVIDENCE": None, "BUILDER_HOST_VERIFY": "enforce"}
    result = _with_envs(env, lambda: _finalize(
        tmp_path, work, verify_runner=_runner({"pytest": 1}), git_runner=_git(), pre_source_paths=None))
    assert result.metadata["gates"], "gate outcomes must be recorded by default"
    assert (spec_dir / "gate-evidence").is_dir()


def test_evidence_typo_does_not_silently_disable(tmp_path):
    specs_dir, spec_dir = _impl_spec(tmp_path)
    ref = _packet(tmp_path, ["pytest"])
    work = _work(tmp_path, runner_task_ref=ref)
    env = {"BUILDER_GATE_EVIDENCE": "onn", "BUILDER_HOST_VERIFY": "enforce"}
    result = _with_envs(env, lambda: _finalize(
        tmp_path, work, verify_runner=_runner({"pytest": 1}), git_runner=_git(), pre_source_paths=None))
    assert result.metadata["gates"] and (spec_dir / "gate-evidence").is_dir()


def test_bounded_wrapper_unchanged(tmp_path):
    assert _run_verify_commands_bounded(["true"], str(tmp_path)) == []
    assert _run_verify_commands_bounded(["exit 3"], str(tmp_path)) == ["exit 3"]


def test_detailed_capture_false_uses_devnull(tmp_path):
    res = _run_verify_commands_detailed(["echo hidden", "exit 4"], str(tmp_path), capture=False)
    assert [r.ok for r in res] == [True, False]
    assert [r.stdout_tail for r in res] == ["", ""]
    assert [r.stderr_tail for r in res] == ["", ""]


def test_enum_host_verify_abstains(tmp_path):
    ref = _packet(tmp_path, [])
    sink = []
    assert _with_envs({"BUILDER_HOST_VERIFY": "off"}, lambda: _host_verify_gate(
        _work(tmp_path, runner_task_ref=ref), "verify", outcome_sink=sink)) == (None, "")
    assert sink[-1].enum_string() == "abstain:off"
    assert _with_envs({"BUILDER_HOST_VERIFY": "enforce"}, lambda: _host_verify_gate(
        _work(tmp_path, runner_task_ref=ref), "plan", outcome_sink=sink)) == (None, "")
    assert sink[-1].enum_string() == "abstain:non_gated_phase"
    # REQUIRE_COMMANDS defaults to '1' (B5b) -- explicit '0' opt-out to exercise the abstain path.
    assert _with_envs({"BUILDER_HOST_VERIFY": "enforce", "BUILDER_HOST_VERIFY_REQUIRE_COMMANDS": "0"},
                      lambda: _host_verify_gate(
        _work(tmp_path), "verify", outcome_sink=sink)) == (None, "")
    assert sink[-1].enum_string() == "abstain:no_commands"
    class BrokenWork:
        @property
        def runner_task_ref(self):
            raise RuntimeError("reader failed")

    assert _with_envs({"BUILDER_HOST_VERIFY": "enforce"}, lambda: _host_verify_gate(
        BrokenWork(), "verify", outcome_sink=sink)) == (None, "")
    assert sink[-1].enum_string() == "abstain:error"


def test_directory_runner_ref_keeps_setup_fallback_enforced(tmp_path):
    ref = ".builder/specs/demo/runs/packet.yaml"
    (tmp_path / ref).mkdir(parents=True)
    setup = tmp_path / ".builder" / "specs" / "demo" / "setup-decisions.yaml"
    setup.parent.mkdir(parents=True, exist_ok=True)
    setup.write_text("commands:\n  default:\n    test: pytest -q\n", encoding="utf-8")
    sink = []
    result = _with_envs({"BUILDER_HOST_VERIFY": "enforce"}, lambda: _host_verify_gate(
        _work(tmp_path, runner_task_ref=ref), "verify",
        verify_runner=_runner({"pytest -q": 1}), outcome_sink=sink))
    assert result == (False, "host verify failed (1/1): pytest -q")
    assert sink[-1].enum_string() == "fail:assertion_failure"


def test_enum_warn_pass_is_pass_not_abstain(tmp_path):
    ref = _packet(tmp_path, ["pytest"])
    sink = []
    ret = _with_envs({"BUILDER_HOST_VERIFY": "warn"}, lambda: _host_verify_gate(
        _work(tmp_path, runner_task_ref=ref), "verify", verify_runner=_runner({"pytest": 0}), outcome_sink=sink))
    assert ret == (None, "")
    assert sink[-1].enum_string() == "pass" and sink[-1].mode == "warn" and sink[-1].blocking is False


def test_enum_warn_fail(tmp_path):
    ref = _packet(tmp_path, ["pytest"])
    sink = []
    ret = _with_envs({"BUILDER_HOST_VERIFY": "warn"}, lambda: _host_verify_gate(
        _work(tmp_path, runner_task_ref=ref), "verify", verify_runner=_runner({"pytest": 1}), outcome_sink=sink))
    assert ret[0] is None and ret[1].startswith("[warn]")
    assert sink[-1].enum_string() == "fail:assertion_failure"


def test_enum_enforce_fail_class_from_tails(tmp_path):
    # Real pytest exit codes drive classification (see classify_failure): 2 is the
    # collection-error code (needle read from stdout), a non-1/126/127 code carries
    # the infra needle on stderr, and 1 is the plain "assertions failed" code that
    # is classified outright (no text can overturn it).
    ref = _packet(tmp_path, ["pytest"])
    cases = [
        (2, "errors during collection\nModuleNotFoundError: x", "", "fail:collection_error"),
        (3, "", "sh: 1: nope: command not found", "fail:infrastructure"),
        (1, "", "assert 1 == 2", "fail:assertion_failure"),
    ]
    for returncode, stdout, stderr, expected in cases:
        sink = []
        runner = lambda cmd, cwd, _rc=returncode, _o=stdout, _s=stderr: types.SimpleNamespace(
            returncode=_rc, stdout=_o, stderr=_s)
        _with_envs({"BUILDER_HOST_VERIFY": "enforce"}, lambda: _host_verify_gate(
            _work(tmp_path, runner_task_ref=ref), "verify", verify_runner=runner, outcome_sink=sink))
        assert sink[-1].enum_string() == expected


def test_injected_bytes_stdout_does_not_change_verdict(tmp_path):
    ref = _packet(tmp_path, ["pytest"])
    work = _work(tmp_path, runner_task_ref=ref)
    runner = lambda cmd, cwd: types.SimpleNamespace(returncode=0, stdout=b"bytes", stderr=b"")
    without_evidence = _with_envs(
        {"BUILDER_HOST_VERIFY": "enforce"},
        lambda: _host_verify_gate(work, "verify", verify_runner=runner),
    )
    sink = []
    with_evidence = _with_envs(
        {"BUILDER_HOST_VERIFY": "enforce"},
        lambda: _host_verify_gate(work, "verify", verify_runner=runner, outcome_sink=sink),
    )
    assert with_evidence == without_evidence == (True, "")
    assert sink[-1].enum_string() == "pass"


def test_enum_source_diff_all_sites(tmp_path):
    sink = []
    env = {"BUILDER_HOST_VERIFY": "enforce"}
    w = _work(tmp_path)
    _with_envs(env, lambda: _source_diff_gate(w, "implement", pre_source_paths=None, outcome_sink=sink))
    assert sink[-1].enum_string() == "abstain:no_baseline"
    _with_envs(env, lambda: _source_diff_gate(w, "implement", pre_source_paths=set(),
                                              git_runner=lambda a, c: types.SimpleNamespace(stdout="", returncode=128),
                                              outcome_sink=sink))
    assert sink[-1].enum_string() == "abstain:git_unavailable"
    def committed_bad(args, cwd):
        if args[0] == "status":
            return ""
        if args[0] == "rev-parse":
            return "bbb\n"
        return types.SimpleNamespace(stdout="", returncode=128)
    _with_envs(env, lambda: _source_diff_gate(w, "implement", pre_source_paths=set(), pre_head="aaa",
                                              git_runner=committed_bad, outcome_sink=sink))
    assert sink[-1].enum_string() == "abstain:committed_diff_unavailable"
    _with_envs(env, lambda: _source_diff_gate(w, "implement", pre_source_paths=set(), pre_head=None,
                                              git_runner=_git("", head=""), outcome_sink=sink))
    assert sink[-1].enum_string() == "abstain:unborn_head"
    _with_envs(env, lambda: _source_diff_gate(w, "implement", pre_source_paths=set(), pre_head="h0",
                                              git_runner=_git("", head="h0\n"), outcome_sink=sink))
    assert sink[-1].enum_string() == "fail:no_source_change"
    ref = _packet(tmp_path, [], body_extra="tdd_mode: required\n")
    w2 = _work(tmp_path, runner_task_ref=ref)
    _with_envs(env, lambda: _source_diff_gate(w2, "implement", pre_source_paths=set(), pre_head="h0",
                                               git_runner=_git(" M src/app.py\n", head="h0\n"), outcome_sink=sink))
    assert sink[-1].enum_string() == "fail:no_test_change"
    _with_envs(env, lambda: _source_diff_gate(w, "implement", pre_source_paths=set(), pre_head="h0",
                                              git_runner=_git(" M src/app.py\n", head="h0\n"), outcome_sink=sink))
    assert sink[-1].enum_string() == "pass"


def test_enum_source_diff_remaining_abstains(tmp_path):
    sink = []
    work = _work(tmp_path)
    _with_envs({"BUILDER_HOST_VERIFY": "off"}, lambda: _source_diff_gate(
        work, "implement", pre_source_paths=set(), outcome_sink=sink))
    assert sink[-1].enum_string() == "abstain:off"
    _with_envs({"BUILDER_HOST_VERIFY": "enforce"}, lambda: _source_diff_gate(
        work, "plan", pre_source_paths=set(), outcome_sink=sink))
    assert sink[-1].enum_string() == "abstain:non_gated_phase"

    class BrokenWork:
        @property
        def project_dir(self):
            raise RuntimeError("git cwd failed")

    _with_envs({"BUILDER_HOST_VERIFY": "enforce"}, lambda: _source_diff_gate(
        BrokenWork(), "implement", pre_source_paths=set(), outcome_sink=sink))
    assert sink[-1].enum_string() == "abstain:error"


def test_enum_red_baseline(tmp_path):
    tasks = [{"id": "T1", "tdd": {"mode": "required"}, "verify": [{"command": "pytest"}]}]
    sink = []
    env = {"BUILDER_RED_BASELINE": "enforce"}
    _with_envs(env, lambda: _red_baseline_gate(_work(tmp_path), tasks, phase="implement", outcome_sink=sink))
    assert sink[-1].enum_string() == "abstain:non_gated_phase"
    _with_envs(env, lambda: _red_baseline_gate(_work(tmp_path, phase="plan"), [{"id": "T2"}],
                                               phase="plan", outcome_sink=sink))
    assert sink[-1].enum_string() == "abstain:no_tdd_tasks"
    _with_envs(env, lambda: _red_baseline_gate(_work(tmp_path, phase="plan"), tasks, phase="plan",
                                               verify_runner=_runner({"pytest": 1}), outcome_sink=sink))
    assert sink[-1].enum_string() == "pass"
    _with_envs(env, lambda: _red_baseline_gate(_work(tmp_path, phase="plan"), tasks, phase="plan",
                                               verify_runner=_runner({"pytest": 0}), outcome_sink=sink))
    assert sink[-1].enum_string() == "fail:non_probative"


def test_enum_red_baseline_remaining_abstains(tmp_path):
    sink = []
    work = _work(tmp_path, phase="plan")
    _with_envs({"BUILDER_RED_BASELINE": "off"}, lambda: _red_baseline_gate(
        work, [], phase="plan", outcome_sink=sink))
    assert sink[-1].enum_string() == "abstain:off"
    _with_envs({"BUILDER_RED_BASELINE": "enforce"}, lambda: _red_baseline_gate(
        work, [], phase="plan", outcome_sink=sink))
    assert sink[-1].enum_string() == "abstain:no_tasks"
    tasks = [{"id": "T1", "tdd": {"mode": "required"}, "verify": [{"command": "pytest"}]}]

    class BrokenWork:
        @property
        def project_dir(self):
            raise RuntimeError("runner cwd failed")

    _with_envs({"BUILDER_RED_BASELINE": "enforce"}, lambda: _red_baseline_gate(
        BrokenWork(), tasks, phase="plan", verify_runner=_runner({}), outcome_sink=sink))
    assert sink[-1].enum_string() == "abstain:error"


def test_enum_packet_contract(tmp_path):
    sink = []
    _with_envs({"BUILDER_PACKET_CONTRACT": "off"}, lambda: _packet_contract_gate(
        _work(tmp_path), "implement", outcome_sink=sink))
    assert sink[-1].enum_string() == "abstain:off"
    _with_envs({"BUILDER_PACKET_CONTRACT": "enforce"}, lambda: _packet_contract_gate(
        _work(tmp_path), "implement", outcome_sink=sink))
    assert sink[-1].enum_string() == "abstain:no_packet"
    ref = _packet(tmp_path, [], body_extra="objective: o\n")
    _with_envs({"BUILDER_PACKET_CONTRACT": "enforce"}, lambda: _packet_contract_gate(
        _work(tmp_path, runner_task_ref=ref), "implement", outcome_sink=sink))
    assert sink[-1].enum_string() == "fail:contract_missing_fields"
    ref2 = _packet(tmp_path, [], body_extra="objective: o\nsteps: [s]\ndone_when: [d]\nallowed_change_files: [src/a.py]\n")
    _with_envs({"BUILDER_PACKET_CONTRACT": "enforce"}, lambda: _packet_contract_gate(
        _work(tmp_path, runner_task_ref=ref2), "implement", outcome_sink=sink))
    assert sink[-1].enum_string() == "pass"


def test_enum_packet_contract_remaining_abstains_and_partial_batch(tmp_path):
    sink = []
    env = {"BUILDER_PACKET_CONTRACT": "enforce"}
    _with_envs(env, lambda: _packet_contract_gate(_work(tmp_path), "plan", outcome_sink=sink))
    assert sink[-1].enum_string() == "abstain:non_gated_phase"

    unreadable_ref = ".builder/specs/demo/runs/unreadable.yaml"
    (tmp_path / unreadable_ref).mkdir(parents=True)
    _with_envs(env, lambda: _packet_contract_gate(
        _work(tmp_path, runner_task_ref=unreadable_ref), "implement", outcome_sink=sink))
    assert sink[-1].enum_string() == "abstain:error"

    valid_ref = ".builder/specs/demo/runs/valid.json"
    valid = tmp_path / valid_ref
    valid.parent.mkdir(parents=True, exist_ok=True)
    valid.write_text(json.dumps({
        "task_id": "T1", "objective": "o", "steps": ["s"], "done_when": ["d"],
        "allowed_change_files": ["src/a.py"],
    }), encoding="utf-8")
    batch_ref = ".builder/specs/demo/runs/batch.json"
    (tmp_path / batch_ref).write_text(json.dumps({"tasks": [
        {"task_ref": valid_ref},
        {"task_ref": ".builder/specs/demo/runs/missing.json"},
    ]}), encoding="utf-8")
    _with_envs(env, lambda: _packet_contract_gate(
        _work(tmp_path, runner_task_ref=batch_ref), "implement", outcome_sink=sink))
    assert sink[-1].enum_string() == "abstain:error"


def test_no_commands_defaults_to_blocking(tmp_path):
    # B5b FLIP: BUILDER_HOST_VERIFY_REQUIRE_COMMANDS now defaults to '1' (fail-closed) — an
    # unset env, same as an explicit "1", must block on zero verify commands rather than
    # silently abstaining. See test_no_commands_opt_out_env_0 for the explicit per-repo opt-out.
    specs_dir, spec_dir = _impl_spec(tmp_path)
    work = _work(tmp_path)
    env = {"BUILDER_GATE_EVIDENCE": "on", "BUILDER_HOST_VERIFY": "enforce",
           "BUILDER_HOST_VERIFY_REQUIRE_COMMANDS": None}
    result = _with_envs(env, lambda: _finalize(tmp_path, work, git_runner=_git(), pre_source_paths=None))
    assert result.metadata["gates"]["host_verify"] == "fail:unverifiable"
    assert result.metadata["decision"] != "phase-complete"


def test_no_commands_opt_out_env_0(tmp_path):
    # The explicit per-repo opt-out: BUILDER_HOST_VERIFY_REQUIRE_COMMANDS=0 restores the old
    # abstain:no_commands (non-blocking) behavior for a repo that genuinely has no command
    # map yet.
    specs_dir, spec_dir = _impl_spec(tmp_path)
    work = _work(tmp_path)
    env = {"BUILDER_GATE_EVIDENCE": "on", "BUILDER_HOST_VERIFY": "enforce",
           "BUILDER_HOST_VERIFY_REQUIRE_COMMANDS": "0"}
    result = _with_envs(env, lambda: _finalize(tmp_path, work, git_runner=_git(), pre_source_paths=None))
    assert result.metadata["gates"]["host_verify"] == "abstain:no_commands"
    assert result.metadata["decision"] == "phase-complete"
    assert "gate_evidence" not in result.metadata


def test_require_commands_blocks(tmp_path):
    specs_dir, spec_dir = _impl_spec(tmp_path)
    work = _work(tmp_path)
    env = {"BUILDER_GATE_EVIDENCE": "on", "BUILDER_HOST_VERIFY": "enforce",
           "BUILDER_HOST_VERIFY_REQUIRE_COMMANDS": "1"}
    result = _with_envs(env, lambda: _finalize(tmp_path, work, git_runner=_git(), pre_source_paths=None))
    assert result.metadata["gates"]["host_verify"] == "fail:unverifiable"
    assert result.metadata["decision"] != "phase-complete"
    b = _bundle(spec_dir, result.metadata["gate_evidence"][0])
    assert b["commands"] == [] and b["verdict"] == "fail"
    sink = []
    ret = _with_envs({"BUILDER_HOST_VERIFY": "warn", "BUILDER_HOST_VERIFY_REQUIRE_COMMANDS": "1"},
                     lambda: _host_verify_gate(work, "implement", outcome_sink=sink))
    assert ret[0] is None and ret[1].startswith("[warn]") and sink[-1].enum_string() == "fail:unverifiable"


def test_fail_bundle_full_content(tmp_path):
    specs_dir, spec_dir = _impl_spec(tmp_path)
    ref = _packet(tmp_path, ["pytest"])
    work = _work(tmp_path, runner_task_ref=ref)
    runner = lambda cmd, cwd: types.SimpleNamespace(returncode=1, stdout="out-tail", stderr="err-tail")
    git = _git("?? new.py\n", head="abc123\n", numstat="120\t8\tsrc/a.py\n", patch="diff --git a/src/a.py b/src/a.py\n")
    env = {"BUILDER_GATE_EVIDENCE": "on", "BUILDER_HOST_VERIFY": "enforce", "SECRET_TOKEN": "dont-record"}
    result = _with_envs(env, lambda: _finalize(tmp_path, work, verify_runner=runner, git_runner=git, pre_source_paths=None))
    b = _bundle(spec_dir, result.metadata["gate_evidence"][0])
    assert b["command"] == ["/bin/sh", "-c", "pytest"] and b["cwd"] == str(tmp_path)
    assert "SECRET_TOKEN" in b["env_fingerprint"] and all("=" not in x and x != "dont-record" for x in b["env_fingerprint"])
    assert b["exit_code"] == 1 and b["stdout_tail"] == "out-tail" and b["stderr_tail"] == "err-tail"
    assert b["stdout_bytes_total"] == len(b"out-tail") and b["stderr_bytes_total"] == len(b"err-tail")
    assert b["duration_ms"] >= 0 and b["git_head_sha"] == "abc123"
    assert b["diff_stat"]["files"] == ["src/a.py", "new.py"] and b["diff_patch_tail"]
    assert b["verdict"] == "fail" and b["failure_class"] == "assertion_failure"
    assert b["blocking"] is True and b["mode"] == "enforce"


def test_pass_bundle_written_with_empty_patch(tmp_path):
    specs_dir, spec_dir = _impl_spec(tmp_path)
    ref = _packet(tmp_path, ["pytest"])
    work = _work(tmp_path, runner_task_ref=ref)
    env = {"BUILDER_GATE_EVIDENCE": "on", "BUILDER_HOST_VERIFY": "enforce"}
    result = _with_envs(env, lambda: _finalize(
        tmp_path, work, verify_runner=_runner({"pytest": 0}), git_runner=_git(numstat="1\t0\tsrc/a.py\n"),
        pre_source_paths=None))
    b = _bundle(spec_dir, result.metadata["gate_evidence"][0])
    assert b["verdict"] == "pass" and b["failure_class"] is None and b["diff_patch_tail"] == ""


def test_tail_truncation_real_subprocess(tmp_path):
    cmd = f"{sys.executable} -c \"import sys; sys.stdout.write('x' * {gate_evidence.TAIL_BYTES + 10})\""
    res = _run_verify_commands_detailed([cmd], str(tmp_path), capture=True)[0]
    assert res.stdout_tail == "x" * gate_evidence.TAIL_BYTES
    assert res.truncated is True and res.stdout_bytes_total == gate_evidence.TAIL_BYTES + 10
    entry = _command_entry_for_test(res)
    assert len(entry["stdout_tail"]) <= gate_evidence.CMD_TAIL_BYTES


def test_capture_never_throttles_the_child(tmp_path):
    """REGRESSION: capture must not change a verdict.

    An earlier fix bounded capture with RLIMIT_FSIZE on the child. RLIMIT_FSIZE bounds EVERY file the
    child writes -- not just our capture -- so a PASSING verify command that emits a >cap artifact
    (coverage db, build output, snapshot fixture) was killed with SIGXFSZ and recorded as a gate
    FAILURE. Reproduced: exit 0 with capture off, exit 1 with capture on.
    """
    artifact = 5 * 1024 * 1024  # > the old 4 MiB cap
    cmd = (f"{sys.executable} -c \"open('artifact.bin','wb').write(b'x'*{artifact})\""
           " && echo TESTS-PASSED")
    off = _run_verify_commands_detailed([cmd], str(tmp_path), capture=False)[0]
    on = _run_verify_commands_detailed([cmd], str(tmp_path), capture=True)[0]
    assert off.ok is True, "the command passes with capture off"
    assert on.ok is True, "capture must NOT turn a passing command into a gate failure"
    assert on.exit_code == off.exit_code == 0
    assert "TESTS-PASSED" in on.stdout_tail


def test_capture_memory_is_bounded_by_the_ring_buffer(tmp_path):
    """A verify command is agent-authored: `yes` streams GB/s. Memory must stay bounded to the tail,
    while the child runs UNTHROTTLED (we record the true byte count, we do not cap the child)."""
    res = _with_envs(
        {"BUILDER_HOST_VERIFY_TIMEOUT": "2"},
        lambda: _run_verify_commands_detailed(["yes"], str(tmp_path), capture=True)[0],
    )
    assert res.timed_out is True and not res.ok
    assert len(res.stdout_tail.encode("utf-8")) <= gate_evidence.TAIL_BYTES  # bounded retention
    assert res.truncated is True
    # The child was NOT throttled -- it emitted far more than we retained.
    assert res.stdout_bytes_total > gate_evidence.TAIL_BYTES * 100


def test_capture_survives_escaped_descendant_holding_the_pipe(tmp_path):
    """REGRESSION (blocker): a verify command can fork + setsid, exit 0, and leave a grandchild
    OUTSIDE the process group holding the inherited stdout/stderr pipe open. The drain then never
    sees EOF. An earlier version bounded the join but still called stream.close() afterwards, which
    blocked behind the drain's blocking read() -- the gate hung for as long as the grandchild lived
    (measured 30s; unbounded with sleep 999). The drains must poll a stop flag and exit promptly,
    and a still-alive drain's stream must never be closed.
    """
    import shlex
    import threading

    code = "import os,sys,time; pid=os.fork();\nif pid: sys.exit(0);\nos.setsid(); time.sleep(30)"
    cmd = f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}"
    threads_before = threading.active_count()
    t0 = time.monotonic()
    res = _with_envs(
        {"BUILDER_HOST_VERIFY_TIMEOUT": "10"},
        lambda: _run_verify_commands_detailed([cmd], str(tmp_path), capture=True)[0],
    )
    elapsed = time.monotonic() - t0
    assert elapsed < 8, f"gate hung on an escaped pipe-holder: {elapsed:.1f}s"
    assert res.exit_code == 0 and res.ok  # the command itself exited 0 -- verdict unchanged
    for _ in range(20):  # daemon drains must wind down, not accumulate
        if threading.active_count() <= threads_before:
            break
        time.sleep(0.1)
    assert threading.active_count() <= threads_before + 1


def test_drain_start_failure_runs_the_command_exactly_once(tmp_path):
    """REGRESSION: capture must be set up BEFORE the command is spawned.

    Two earlier shapes were both wrong. Spawn-then-start-drains, and on failure (a) drop the drains ->
    the child still has PIPE fds, so a chatty child blocks on a full pipe and is misreported as a
    timeout; or (b) kill and RE-RUN without capture -> the verify command EXECUTES TWICE (proved with
    a counter file that reached 2). A verify command may have side effects; running it twice is not an
    acceptable degrade. We now own the pipes and start the drains first, so the command is launched
    exactly once, always.
    """
    import _dispatch_runtime.lane_common as lane_common

    counter = tmp_path / "counter.txt"
    cmd = (f"{sys.executable} -c \"open('counter.txt','a').write('x')\" && "
           f"{sys.executable} -c \"import sys; sys.stdout.write('x' * 1048576)\" && echo DONE")
    original = lane_common._TailDrain.start

    def refuse(self):
        raise RuntimeError("cannot start thread")

    lane_common._TailDrain.start = refuse
    try:
        t0 = time.monotonic()
        res = _run_verify_commands_detailed([cmd], str(tmp_path), capture=True)[0]
        elapsed = time.monotonic() - t0
    finally:
        lane_common._TailDrain.start = original

    runs = len(counter.read_text()) if counter.exists() else 0
    assert runs == 1, f"the verify command executed {runs} times; side effects would be duplicated"
    assert res.ok is True and res.timed_out is False, "a passing chatty child must not become a failure"
    assert res.exit_code == 0
    assert elapsed < 8, f"undrained pipe blocked the child: {elapsed:.1f}s"


def test_timeout_does_not_burn_the_grace_window(tmp_path):
    """REGRESSION: _reap_group polls killpg(pgid, 0), and an unwaited ZOMBIE direct child still
    counts as a group member -- so the reap burned its whole grace window (a 1s budget took ~6s;
    ~11s when reaped twice). Reaping our own child first makes the aggregate budget a real wall
    clock. Must hold identically with capture on and off."""
    for capture in (False, True):
        t0 = time.monotonic()
        res = _with_envs(
            {"BUILDER_HOST_VERIFY_TIMEOUT": "1"},
            lambda: _run_verify_commands_detailed(["sleep 30"], str(tmp_path), capture=capture)[0],
        )
        elapsed = time.monotonic() - t0
        assert res.timed_out is True
        assert elapsed < 4, f"capture={capture}: timeout took {elapsed:.1f}s for a 1s budget"


def _count_fds():
    for d in ("/dev/fd", "/proc/self/fd"):
        if os.path.isdir(d):
            try:
                return len(os.listdir(d))
            except OSError:
                pass
    return -1


def test_capture_leaks_no_file_descriptors(tmp_path):
    before = _count_fds()
    for _ in range(15):
        _run_verify_commands_detailed(["true"], str(tmp_path), capture=True)
    after = _count_fds()
    assert before < 0 or after <= before + 3, f"fd leak: {before} -> {after}"


def test_spawn_failure_leaks_no_pipe_fds(tmp_path):
    """REGRESSION: we own the capture pipes, so a Popen failure must still close them. The
    spawn-failure path `continue`s, so it never reached the stop/join -- leaving the drains alive
    made the read-fd cleanup bail out and leak the pipe (measured: 10 failed spawns, 4 -> 22 fds)."""
    missing_cwd = str(tmp_path / "does-not-exist")
    before = _count_fds()
    for _ in range(10):
        res = _run_verify_commands_detailed(["true"], missing_cwd, capture=True)[0]
        assert res.spawn_error == "spawn_failed"
    after = _count_fds()
    assert before < 0 or after <= before + 2, f"pipe fd leak on spawn failure: {before} -> {after}"


def _command_entry_for_test(result):
    from _dispatch_runtime.lane_common import _command_entry
    return _command_entry(result, gate_evidence.CMD_TAIL_BYTES)


def test_timeout_reap_with_capture(tmp_path):
    pid_file = tmp_path / "descendant.pid"
    command = (
        "sh -c 'sleep 30 & child=$!; "
        f"echo $child > {pid_file}; "
        'trap "kill $child 2>/dev/null; wait $child 2>/dev/null; exit 0" TERM; wait $child\''
    )
    env = {"BUILDER_HOST_VERIFY_TIMEOUT": "1"}
    start = time.monotonic()
    res = _with_envs(env, lambda: _run_verify_commands_detailed(
        [command], str(tmp_path), capture=True))[0]
    assert time.monotonic() - start < 10
    assert res.timed_out is True
    assert gate_evidence.classify_failure(res) == "timeout"
    descendant = int(pid_file.read_text(encoding="utf-8").strip())
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(descendant, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        raise AssertionError(f"descendant process {descendant} survived gate reap")


def test_classify_failure_exit1_with_infra_words_in_stdout_is_assertion():
    # REGRESSION: exit 1 (the standard test-runner "assertions failed" code) must be
    # assertion_failure outright — a test asserting on a missing file legitimately
    # prints "No such file or directory" without the run itself being infrastructure.
    res = gate_evidence.CommandResult(
        command="pytest", exit_code=1,
        stdout_tail="AssertionError: expected No such file or directory to be raised",
        stderr_tail="",
    )
    assert gate_evidence.classify_failure(res) == "assertion_failure"


def test_classify_failure_exit127_stderr_command_not_found_is_infrastructure():
    res = gate_evidence.CommandResult(
        command="missing-tool", exit_code=127,
        stdout_tail="", stderr_tail="sh: missing-tool: command not found",
    )
    assert gate_evidence.classify_failure(res) == "infrastructure"


def test_classify_failure_exit2_collection_error_from_stdout():
    res = gate_evidence.CommandResult(
        command="pytest", exit_code=2,
        stdout_tail="errors during collection\nImportError: No module named foo",
        stderr_tail="",
    )
    assert gate_evidence.classify_failure(res) == "collection_error"


def test_classify_failure_exit1_infra_words_in_stderr_still_assertion():
    # exit==1 short-circuits to assertion_failure BEFORE the stderr infra-needle scan —
    # text (even on stderr) cannot overturn a clean runner failure into infrastructure.
    res = gate_evidence.CommandResult(
        command="pytest", exit_code=1,
        stdout_tail="", stderr_tail="Permission denied while opening a fixture file",
    )
    assert gate_evidence.classify_failure(res) == "assertion_failure"


def test_classify_failure_other_exit_code_infra_needle_in_stdout_is_ignored():
    # Infra needles are read from STDERR ONLY — a stdout mention (e.g. a test module
    # asserting about ENOENT handling) must not flip an otherwise-plain failure.
    res = gate_evidence.CommandResult(
        command="pytest", exit_code=3,
        stdout_tail="test_handles_enoent asserts ENOENT is raised", stderr_tail="",
    )
    assert gate_evidence.classify_failure(res) == "assertion_failure"


def test_spawn_and_budget_results(tmp_path):
    env = {"BUILDER_HOST_VERIFY_TIMEOUT": "1"}
    res = _with_envs(env, lambda: _run_verify_commands_detailed(
        ["sleep 30", "echo never"], str(tmp_path), capture=True))
    assert len([r for r in res if not r.ok]) == 2
    assert res[1].spawn_error == "aggregate_budget_exhausted" and res[1].timed_out is True


def _body(gate="host_verify", phase="implement", verdict="fail"):
    return {
        "schema": gate_evidence.SCHEMA, "gate": gate, "phase": phase, "spec_id": "demo",
        "verdict": verdict, "prev_bundle_sha256": "", "bundle_sha256": "",
    }


def test_chain_links_and_verifies(tmp_path):
    spec_dir = tmp_path / ".builder" / "specs" / "demo"
    ev = spec_dir / "gate-evidence"
    p1 = gate_evidence.write_bundle(ev, _body())
    p2 = gate_evidence.write_bundle(ev, _body(gate="source_diff"))
    b1 = yaml.safe_load(p1.read_text(encoding="utf-8"))
    b2 = yaml.safe_load(p2.read_text(encoding="utf-8"))
    assert b1["prev_bundle_sha256"] == ""
    assert b2["prev_bundle_sha256"] == b1["bundle_sha256"]
    assert gate_evidence.verify_chain(spec_dir) == []


def test_chain_detects_tamper(tmp_path):
    spec_dir = tmp_path / ".builder" / "specs" / "demo"
    ev = spec_dir / "gate-evidence"
    p1 = gate_evidence.write_bundle(ev, _body())
    gate_evidence.write_bundle(ev, _body(gate="source_diff"))
    b1 = yaml.safe_load(p1.read_text(encoding="utf-8"))
    b1["verdict"] = "pass"
    _rewrite_bundle(p1, b1)
    violations = gate_evidence.verify_chain(spec_dir)
    assert any("sha mismatch" in v for v in violations)
    assert any("prev link mismatch" in v for v in violations)


def _rewrite_bundle(path, body):
    path.write_text(json.dumps(body, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def test_multiline_tail_round_trips_without_key_injection(tmp_path):
    ev = tmp_path / "gate-evidence"
    multiline = "first line\nverdict: pass\ncommands:\n  - injected\nlast line\n"
    body = _body()
    body.update({"stdout_tail": multiline, "stderr_tail": "error\nsecond line\n", "diff_patch_tail": ""})
    path = gate_evidence.write_bundle(ev, body)
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert loaded["verdict"] == "fail"
    assert loaded["stdout_tail"] == multiline
    assert loaded["stderr_tail"] == "error\nsecond line\n"
    assert gate_evidence.bundle_sha(loaded) == loaded["bundle_sha256"]


def test_chain_detects_relinked_rewrite_and_deletions(tmp_path):
    rewritten = tmp_path / "rewritten"
    ev = rewritten / "gate-evidence"
    paths = [
        gate_evidence.write_bundle(ev, _body(gate=gate))
        for gate in ("host_verify", "source_diff", "packet_contract")
    ]
    original_head = yaml.safe_load(paths[-1].read_text(encoding="utf-8"))["bundle_sha256"]
    previous = ""
    for index, path in enumerate(paths):
        body = yaml.safe_load(path.read_text(encoding="utf-8"))
        if index == 0:
            body["verdict"] = "pass"
        body["prev_bundle_sha256"] = previous
        body["bundle_sha256"] = gate_evidence.bundle_sha(body)
        previous = body["bundle_sha256"]
        _rewrite_bundle(path, body)
    assert gate_evidence.verify_chain(rewritten) == []
    assert any("expected head mismatch" in v for v in gate_evidence.verify_chain(rewritten, original_head))

    missing_middle = tmp_path / "missing-middle"
    ev = missing_middle / "gate-evidence"
    middle_paths = [gate_evidence.write_bundle(ev, _body(gate=g)) for g in ("host_verify", "source_diff", "packet_contract")]
    middle_paths[1].unlink()
    assert any("non-contiguous seq" in v for v in gate_evidence.verify_chain(missing_middle))

    missing_tail = tmp_path / "missing-tail"
    ev = missing_tail / "gate-evidence"
    tail_paths = [gate_evidence.write_bundle(ev, _body(gate=g)) for g in ("host_verify", "source_diff", "packet_contract")]
    expected_head = yaml.safe_load(tail_paths[-1].read_text(encoding="utf-8"))["bundle_sha256"]
    tail_paths[-1].unlink()
    assert any("expected head mismatch" in v for v in gate_evidence.verify_chain(missing_tail, expected_head))


def test_chain_detects_duplicate_internal_seq_and_missing_dirs(tmp_path):
    spec_dir = tmp_path / "duplicate"
    ev = spec_dir / "gate-evidence"
    gate_evidence.write_bundle(ev, _body())
    p2 = gate_evidence.write_bundle(ev, _body(gate="source_diff"))
    b2 = yaml.safe_load(p2.read_text(encoding="utf-8"))
    b2["seq"] = 1
    b2["bundle_sha256"] = gate_evidence.bundle_sha(b2)
    _rewrite_bundle(p2, b2)
    assert any("duplicate internal seq" in v for v in gate_evidence.verify_chain(spec_dir))
    assert gate_evidence.verify_chain(tmp_path / "absent") == ["gate-evidence directory missing"]
    empty = tmp_path / "empty"
    (empty / "gate-evidence").mkdir(parents=True)
    assert gate_evidence.verify_chain(empty) == ["gate-evidence directory empty"]


def test_seq_allocation_and_filenames(tmp_path):
    spec_dir = tmp_path / ".builder" / "specs" / "demo"
    ev = spec_dir / "gate-evidence"
    p1 = gate_evidence.write_bundle(ev, _body())
    p2 = gate_evidence.write_bundle(ev, _body(gate="source_diff"))
    assert p1.name.startswith("0001-host_verify-implement")
    assert p2.name.startswith("0002-source_diff-implement")
    b2 = yaml.safe_load(p2.read_text(encoding="utf-8"))
    assert b2["gate_id"] == "demo:implement:source_diff:0002"
    (ev / "0003-red_baseline-plan.yaml").write_text("x: y\n", encoding="utf-8")
    p4 = gate_evidence.write_bundle(ev, _body(gate="packet_contract"))
    assert p4 is not None and p4.name.startswith("0004-")


def test_seq_allocation_continues_past_four_digits(tmp_path):
    ev = tmp_path / "gate-evidence"
    ev.mkdir()
    (ev / "9999-host_verify-implement.yaml").write_text("{}\n", encoding="utf-8")
    p10000 = gate_evidence.write_bundle(ev, _body())
    p10001 = gate_evidence.write_bundle(ev, _body(gate="source_diff"))
    assert p10000.name.startswith("10000-")
    assert p10001.name.startswith("10001-")


def test_red_baseline_bundle_polarity_red(tmp_path):
    specs_dir = tmp_path / ".builder" / "specs"
    spec_dir = specs_dir / "demo"
    spec_dir.mkdir(parents=True, exist_ok=True)
    (spec_dir / "spec.yaml").write_text('name: demo\nstatus: planned\ncurrent_phase: implement\n', encoding="utf-8")
    (spec_dir / "tasks.yaml").write_text(
        "tasks:\n  - id: T1\n    tdd:\n      mode: required\n    verify:\n      - command: pytest\n",
        encoding="utf-8")
    (spec_dir / "handoff.yaml").write_text("next_phase: implement\nspec: demo\nready: true\ncompleted_phase: plan\n", encoding="utf-8")
    (spec_dir / "phase-log.yaml").write_text(
        'phases:\n  - phase: plan\n    completed: "2026-07-10T00:00:00Z"\n    outcome: SUCCEEDED\n',
        encoding="utf-8")
    work = _work(tmp_path, phase="plan")
    env = {"BUILDER_GATE_EVIDENCE": "on", "BUILDER_RED_BASELINE": "enforce"}
    result = _with_envs(env, lambda: _finalize(tmp_path, work, verify_runner=_runner({"pytest": 1}), git_runner=_git()))
    b = _bundle(spec_dir, result.metadata["gate_evidence"][0])
    assert b["polarity"] == "red" and b["commands"][0]["exit_code"] == 1 and b["verdict"] == "pass"


def test_turn_incomplete_synthesized(tmp_path):
    on_root = tmp_path / "on"
    off_root = tmp_path / "off"
    on_root.mkdir()
    off_root.mkdir()
    _bad_impl_spec(on_root)
    _bad_impl_spec(off_root)
    env = {"BUILDER_GATE_EVIDENCE": "on", "BUILDER_HOST_VERIFY": "enforce"}
    result = _with_envs(env, lambda: _finalize(on_root, _work(on_root), git_runner=_git()))
    baseline = _with_envs(
        {"BUILDER_GATE_EVIDENCE": None, "BUILDER_HOST_VERIFY": "enforce"},
        lambda: _finalize(off_root, _work(off_root), git_runner=_git()),
    )
    assert set(result.metadata["gates"].values()) == {"abstain:turn_incomplete"}
    assert "gate_evidence" not in result.metadata
    assert result.metadata["decision"] == baseline.metadata["decision"]


def test_bundles_land_under_control_root(tmp_path):
    main = tmp_path / "main"
    wt = tmp_path / "wt"
    main.mkdir()
    wt.mkdir()
    specs_dir, spec_dir = _impl_spec(wt, control_root=main)
    ref = _packet(wt, ["pytest"])
    work = _work(wt, runner_task_ref=ref, control_root=main)
    env = {"BUILDER_GATE_EVIDENCE": "on", "BUILDER_HOST_VERIFY": "enforce"}
    result = _with_envs(env, lambda: _finalize(
        wt, work, verify_runner=_runner({"pytest": 1}), git_runner=_git(), control_root=main, pre_source_paths=None))
    assert _evidence_path(spec_dir, result.metadata["gate_evidence"][0]).exists()
    assert not (wt / ".builder" / "specs" / "demo" / "gate-evidence").exists()


def test_malformed_packet_still_contained(tmp_path):
    specs_dir, spec_dir = _impl_spec(tmp_path)
    bad_ref = object()
    work = _work(tmp_path, runner_task_ref=bad_ref)
    env = {"BUILDER_GATE_EVIDENCE": "on", "BUILDER_HOST_VERIFY": "enforce"}
    result = _with_envs(env, lambda: _finalize(tmp_path, work, git_runner=_git(), pre_source_paths=None))
    assert result.metadata["gates"]["host_verify"] == "abstain:error"


def test_metadata_gate_evidence_paths_resolve(tmp_path):
    specs_dir, spec_dir = _impl_spec(tmp_path)
    ref = _packet(tmp_path, ["pytest"])
    work = _work(tmp_path, runner_task_ref=ref)
    env = {"BUILDER_GATE_EVIDENCE": "on", "BUILDER_HOST_VERIFY": "enforce"}
    result = _with_envs(env, lambda: _finalize(
        tmp_path, work, verify_runner=_runner({"pytest": 1}), git_runner=_git(), pre_source_paths=None))
    for entry in result.metadata["gate_evidence"]:
        assert set(entry) == {"path", "sha256"}
        assert not Path(entry["path"]).is_absolute()
        path = spec_dir / entry["path"]
        assert path.exists()
        assert yaml.safe_load(path.read_text(encoding="utf-8"))["bundle_sha256"] == entry["sha256"]
    assert gate_evidence.verify_chain(spec_dir, result.metadata["gate_evidence"][-1]["sha256"]) == []


def test_packet_contract_rejects_evidence_path():
    packet = {
        "task_id": "T1", "objective": "o", "steps": ["s"], "done_when": ["d"],
        "allowed_change_files": [".builder/specs/demo/gate-evidence/x.yaml"],
    }
    ret = _with_envs({"BUILDER_PACKET_CONTRACT": "enforce"}, lambda: validate_packet_contract(packet))
    assert ret[0] is False
    assert ret[1].startswith("allowed_change_files names a host-only gate-evidence path:")


def test_emit_never_raises(tmp_path):
    on_root = tmp_path / "on"
    off_root = tmp_path / "off"
    on_root.mkdir()
    off_root.mkdir()
    specs_dir, spec_dir = _impl_spec(on_root)
    _impl_spec(off_root)
    (spec_dir / "gate-evidence").write_text("not a dir", encoding="utf-8")
    ref = _packet(on_root, ["pytest"])
    off_ref = _packet(off_root, ["pytest"])
    env = {"BUILDER_GATE_EVIDENCE": "on", "BUILDER_HOST_VERIFY": "enforce"}
    result = _with_envs(env, lambda: _finalize(
        on_root, _work(on_root, runner_task_ref=ref), verify_runner=_runner({"pytest": 1}),
        git_runner=_git(), pre_source_paths=None))
    baseline = _with_envs(
        {"BUILDER_GATE_EVIDENCE": None, "BUILDER_HOST_VERIFY": "enforce"},
        lambda: _finalize(
            off_root, _work(off_root, runner_task_ref=off_ref), verify_runner=_runner({"pytest": 1}),
            git_runner=_git(), pre_source_paths=None),
    )
    assert result.metadata["decision"] == baseline.metadata["decision"]
    assert "gate_evidence" not in result.metadata


def test_packet_contract_spec_dir_ref_is_no_packet_not_error(tmp_path):
    """REGRESSION: the `draft` flow enqueues the SPEC DIRECTORY as runner_task_ref, so the gate's
    _safe_yaml() could not parse it and it reported `abstain:error` -- a lie. The truth is that the
    autonomous plan directive emits no runs/*.yaml packets at all (the phantom-contract hole), so
    there is nothing to check. `no_packet` leaves the gate visibly UNCOVERED in gate-coverage, which
    is the signal the operator needs. `error` stays reserved for a packet that exists and is corrupt.
    """
    sink = []
    env = {"BUILDER_PACKET_CONTRACT": "enforce"}
    spec_dir = tmp_path / ".builder" / "specs" / "demo"
    spec_dir.mkdir(parents=True)
    ret = _with_envs(env, lambda: _packet_contract_gate(
        _work(tmp_path, runner_task_ref=".builder/specs/demo"), "implement", outcome_sink=sink))
    assert ret == (None, "")
    assert sink[-1].enum_string() == "abstain:no_packet", "a spec-dir ref with no packets is not an error"

    # ...but once the plan DOES emit packets, the gate adjudicates them for real.
    runs = spec_dir / "runs"
    runs.mkdir()
    (runs / "task-T1.yaml").write_text(json.dumps({
        "task_id": "T1", "objective": "o", "steps": ["s"], "done_when": ["d"],
        "allowed_change_files": ["src/a.py"],
    }), encoding="utf-8")
    sink2 = []
    ret2 = _with_envs(env, lambda: _packet_contract_gate(
        _work(tmp_path, runner_task_ref=".builder/specs/demo"), "implement", outcome_sink=sink2))
    assert sink2[-1].enum_string() == "pass", "a contract-bearing packet must adjudicate, not abstain"
    assert ret2[0] is True


def test_verify_repairs_legacy_empty_baseline_into_isolated_sync_scope(tmp_path):
    main = tmp_path / "main"
    wt = tmp_path / "wt"
    main.mkdir()
    wt.mkdir()
    spec_dir = main / ".builder" / "specs" / "demo"
    spec_dir.mkdir(parents=True, exist_ok=True)
    (spec_dir / "spec.yaml").write_text(
        "name: demo\nstatus: syncing\ncurrent_phase: sync\n", encoding="utf-8"
    )
    (spec_dir / "phase-log.yaml").write_text(
        'phases:\n  - phase: verify\n    completed: "2026-07-21T00:00:00Z"\n    outcome: SUCCEEDED\n',
        encoding="utf-8",
    )
    (spec_dir / "handoff.yaml").write_text(
        "next_phase: sync\nspec: demo\nready: true\ncompleted_phase: verify\n", encoding="utf-8"
    )
    (spec_dir / "ssot-delta.yaml").write_text(
        "capabilities: []\nbehaviors: []\njourneys: []\n", encoding="utf-8"
    )
    (spec_dir / "implementation-baseline.yaml").write_text(
        "schema: implementation-baseline/v1\n"
        "spec: demo\n"
        "implementation_baseline: base-1\n"
        "baseline_paths: []\n"
        "baseline_paths_digest: 01ba4719c80b6fe911b091a7c05124b64eeece964e09c058ef8f9805daca546b\n"
        f"worktree_root: {main}\n"
        f"control_root: {main}\n"
        "worktree_isolated: false\n",
        encoding="utf-8",
    )
    ref = _packet(wt, ["pytest"])
    work = _work(wt, phase="verify", runner_task_ref=ref, control_root=main)
    env = {"BUILDER_GATE_EVIDENCE": "on", "BUILDER_HOST_VERIFY": "enforce"}

    result = _with_envs(env, lambda: _finalize(
        wt,
        work,
        verify_runner=_runner({"pytest": 0}),
        git_runner=_git(head="head-1\n"),
        pre_source_paths=None,
        control_root=main,
    ))

    assert result.metadata["gates"]["host_verify"] == "pass"
    repaired = yaml.safe_load((spec_dir / "implementation-baseline.yaml").read_text(encoding="utf-8"))
    scope = yaml.safe_load((spec_dir / "sync-scope.yaml").read_text(encoding="utf-8"))
    assert repaired["worktree_root"] == str(wt)
    assert repaired["control_root"] == str(main)
    assert repaired["worktree_isolated"] is True
    assert scope["verify_gate_bundle"].startswith("gate-evidence/")
    assert scope["worktree_root"] == str(wt)
    assert scope["control_root"] == str(main)
    assert scope["worktree_isolated"] is True
