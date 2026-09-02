"""The CLI is the product surface, so these tests are about what it REFUSES to say.

The two failures that would matter:
  * `isanna verify` printing a green exit 0 for a project it never actually checked;
  * `isanna demo` "passing" while the gate quietly stopped catching the liar.
Both are laundering -- an unearned green -- and both are asserted against here.

No pytest fixtures beyond tmp_path: this repo runs its own minimal pytest shim, so env and
stdout are handled by hand (the same discipline as the dispatcher suites).
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import sys
from pathlib import Path

from _yaml import yaml

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

_GATES_ON = {
    "BUILDER_HOST_VERIFY": "enforce",
    "BUILDER_RED_BASELINE": "enforce",
    "BUILDER_GATE_EVIDENCE": "off",
}
_AMBIENT_DISCOVERY_ENV = ("ISANNA_PROJECTS_ROOT",)


def _load(script: str, name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


isanna = _load("isanna.py", "isanna_cli_under_test")
demo = _load("demo.py", "isanna_demo_under_test")
from tests.unit.sync_evidence_support import write_host_scope


def _run(fn):
    """Call fn() with the gates pinned on, capturing stdout. Returns (exit_code, output)."""
    isolated = (*_GATES_ON, *_AMBIENT_DISCOVERY_ENV)
    saved = {k: os.environ.get(k) for k in isolated}
    os.environ.update(_GATES_ON)
    for key in _AMBIENT_DISCOVERY_ENV:
        os.environ.pop(key, None)
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            code = fn()
        return code, buf.getvalue()
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _project(tmp_path: Path, test_cmd: str | None) -> Path:
    spec = tmp_path / ".builder" / "specs" / "s1"
    spec.mkdir(parents=True)
    if test_cmd is not None:
        (spec / "setup-decisions.yaml").write_text(
            f'commands:\n  default:\n    test: "{test_cmd}"\n', encoding="utf-8")
    return tmp_path


# ----------------------------------------------------------------- isanna sync (SSOT stays honest)

def _sync_repo(tmp_path: Path, guard_test: str) -> Path:
    spec = tmp_path / ".builder" / "specs" / "demo"
    spec.mkdir(parents=True)
    (spec / "spec.yaml").write_text("status: verified\ncurrent_phase: sync\n", encoding="utf-8")
    (spec / "ssot-delta.yaml").write_text(
        "capabilities: []\nbehaviors: []\njourneys: []\n", encoding="utf-8"
    )
    (tmp_path / "tests" / "unit").mkdir(parents=True)
    (tmp_path / "tests" / "unit" / "test_z.py").write_text("def test_real():\n    pass\n", encoding="utf-8")
    (tmp_path / "Makefile").write_text("gate:\n\tpytest tests/unit/test_z.py -q\n", encoding="utf-8")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "system-behaviors.yaml").write_text(
        "schema: system-behaviors/v1\nbehaviors:\n  - id: a\n    area: x\n    behavior: b\n"
        "    invariant: i\n    breaks_when: w\n    guarding_tests:\n"
        f"      - tests/unit/test_z.py::{guard_test}\n", encoding="utf-8")
    (tmp_path / ".builder" / "sync-adapter.yaml").write_text(
        "artifact: sync-adapter\nmappings: []\n", encoding="utf-8")
    write_host_scope(tmp_path, "demo")
    return tmp_path


def test_sync_is_green_when_the_behavioral_ssot_is_honest(tmp_path):
    root = _sync_repo(tmp_path, "test_real")  # points at a test that exists and is gated
    code, _ = _run(lambda: isanna.main([
        "sync", "--root", str(root), "--spec", "demo", "--scope-evidence",
        str(root / ".builder" / "specs" / "demo" / "sync-scope.yaml"),
    ]))
    assert code == 0


def test_sync_fails_when_the_ssot_names_a_test_that_is_not_there(tmp_path):
    root = _sync_repo(tmp_path, "test_missing")  # documented behavior with no live test = drift
    code, out = _run(lambda: isanna.main([
        "sync", "--root", str(root), "--spec", "demo", "--scope-evidence",
        str(root / ".builder" / "specs" / "demo" / "sync-scope.yaml"),
    ]))
    assert code == 1
    assert "DRIFT" in out and "test_missing" in out


def test_sync_returns_the_divergence_exit_code_it_persists(tmp_path):
    spec = tmp_path / ".builder" / "specs" / "demo"
    spec.mkdir(parents=True)
    (spec / "spec.yaml").write_text("status: verified\ncurrent_phase: sync\n", encoding="utf-8")
    (spec / "ssot-delta.yaml").write_text(
        "capabilities: []\nbehaviors: []\njourneys: []\n", encoding="utf-8"
    )
    (tmp_path / ".builder" / "sync-adapter.yaml").write_text(
        "artifact: sync-adapter\nmappings:\n  - paths: [scripts/*.py]\n    tuples:\n      - category: capabilities\n        target: mapped\n        change: enrich\n",
        encoding="utf-8",
    )
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "system-behaviors.yaml").write_text(
        "schema: system-behaviors/v1\nbehaviors: []\n", encoding="utf-8"
    )
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "demo.py").write_text("print('demo')\n", encoding="utf-8")
    write_host_scope(tmp_path, "demo", ["scripts/demo.py"])

    code, out = _run(lambda: isanna.main([
        "sync", "--root", str(tmp_path), "--spec", "demo", "--scope-evidence",
        str(spec / "sync-scope.yaml"),
    ]))

    result = yaml.safe_load((spec / "sync-result.yaml").read_text(encoding="utf-8"))
    assert code == 2
    assert result["result"] == "divergence"
    assert result["hook_exit_code"] == 2
    assert "divergence" in out


def test_sync_repairs_legacy_scope_transaction_binding_before_validation(tmp_path):
    root = _sync_repo(tmp_path, "test_real")
    spec = root / ".builder" / "specs" / "demo"
    baseline = yaml.safe_load((spec / "implementation-baseline.yaml").read_text(encoding="utf-8"))
    scope = yaml.safe_load((spec / "sync-scope.yaml").read_text(encoding="utf-8"))
    baseline.pop("transaction_id", None)
    scope.pop("transaction_id", None)
    (spec / "implementation-baseline.yaml").write_text(
        yaml.safe_dump(baseline, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )
    (spec / "sync-scope.yaml").write_text(
        yaml.safe_dump(scope, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )

    code, _ = _run(lambda: isanna.main([
        "sync", "--root", str(root), "--spec", "demo", "--scope-evidence",
        str(spec / "sync-scope.yaml"),
    ]))

    repaired_baseline = yaml.safe_load((spec / "implementation-baseline.yaml").read_text(encoding="utf-8"))
    repaired_scope = yaml.safe_load((spec / "sync-scope.yaml").read_text(encoding="utf-8"))
    assert code == 0
    assert repaired_baseline["transaction_id"] == repaired_scope["transaction_id"]


def test_sync_treats_a_missing_ssot_as_explicit_bootstrap_required(tmp_path):
    spec = tmp_path / ".builder" / "specs" / "demo"
    spec.mkdir(parents=True)
    (spec / "spec.yaml").write_text("status: verified\ncurrent_phase: sync\n", encoding="utf-8")
    (spec / "ssot-delta.yaml").write_text(
        "capabilities: []\nbehaviors: []\njourneys: []\n", encoding="utf-8"
    )
    write_host_scope(tmp_path, "demo")
    code, out = _run(lambda: isanna.main([
        "sync", "--root", str(tmp_path), "--spec", "demo", "--scope-evidence",
        str(spec / "sync-scope.yaml"),
    ]))
    assert code == 2
    assert "isanna sync" in out


# ----------------------------------------------------------------- isanna verify


def test_verify_fails_when_the_command_fails(tmp_path):
    root = _project(tmp_path, "python3 -c 'raise SystemExit(1)'")
    code, out = _run(lambda: isanna.main(["verify", str(root)]))
    assert code == 1 and "REJECTED" in out


def test_verify_passes_only_on_a_real_zero_exit(tmp_path):
    root = _project(tmp_path, "python3 -c 'raise SystemExit(0)'")
    code, out = _run(lambda: isanna.main(["verify", str(root)]))
    assert code == 0 and "VERIFIED" in out


def test_verify_refuses_to_report_success_when_it_checked_NOTHING(tmp_path):
    # A spec with no verify commands has proven nothing. Exiting 0 here would hand a green CI
    # badge to a project the gate never looked at -- the exact unearned green this tool exists
    # to refuse. Blindness is not success.
    root = _project(tmp_path, None)
    code, out = _run(lambda: isanna.main(["verify", str(root)]))
    assert code == 1 and "UNVERIFIABLE" in out


def test_verify_uses_the_gates_own_command_collection(tmp_path):
    # If the CLI grew its own idea of "the verify commands" it could pass what the dispatcher
    # fails. It must read the SAME setup-decisions the gate reads.
    from _dispatch_runtime.lane_common import _collect_verify_commands

    root = _project(tmp_path, "pytest -q")
    assert _collect_verify_commands(isanna._StandaloneWork(root, "s1")) == ["pytest -q"]


# ----------------------------------------------------------------- isanna verify --spec
#
# `--spec X` makes the output a claim about X. Before this, the CLI collected verify commands
# only from a runner packet, and a spec that has been planned but never dispatched has no
# packet -- so it fell through to the project-wide setup-decisions defaults, ran the same two
# commands it would run for every other spec in the repo, and printed "VERIFIED n/n passed,
# host-executed". That is the unearned green wearing a spec's name.


def _spec_project(tmp_path: Path, *, status: str | None = None, task_cmd: str | None = None,
                  project_cmd: str | None = "python3 -c 'raise SystemExit(0)'") -> Path:
    spec = tmp_path / ".builder" / "specs" / "s1"
    spec.mkdir(parents=True)
    if project_cmd is not None:
        (tmp_path / ".builder" / "setup-decisions.yaml").write_text(
            f'commands:\n  default:\n    test: "{project_cmd}"\n', encoding="utf-8")
    if status is not None:
        (spec / "spec.yaml").write_text(
            f"name: s1\nstatus: {status}\ncurrent_phase: plan\n", encoding="utf-8")
    if task_cmd is not None:
        (spec / "tasks.yaml").write_text(
            f'artifact: tasks\ntasks:\n  - id: T1\n    verify:\n      - command: "{task_cmd}"\n',
            encoding="utf-8")
    return tmp_path


def test_verify_spec_scope_refuses_a_green_built_only_on_project_defaults(tmp_path):
    # The project default command PASSES here. Exiting 0 would print VERIFIED under a flag that
    # names s1, on evidence that says nothing whatsoever about s1 -- it is the repo's generic
    # suite, identical for every spec. Blindness about THIS spec is not success either.
    root = _spec_project(tmp_path, status="planned")
    code, out = _run(lambda: isanna.main(["verify", str(root), "--spec", "s1"]))
    assert code == 1 and "UNVERIFIABLE" in out and "s1" in out


def test_verify_spec_scope_actually_runs_the_specs_own_tasks_yaml_commands(tmp_path):
    # Proven by side effect, not by counting: the command must really have executed.
    root = _spec_project(tmp_path, status="planned",
                         task_cmd="python3 -c \\\"open('ran.txt','w').close()\\\"")
    code, out = _run(lambda: isanna.main(["verify", str(root), "--spec", "s1"]))
    assert (root / "ran.txt").exists(), out
    assert code == 0 and "VERIFIED" in out


def test_verify_spec_scope_names_the_spec_and_its_own_command_count(tmp_path):
    root = _spec_project(tmp_path, status="planned", task_cmd="python3 -c 'raise SystemExit(0)'")
    code, out = _run(lambda: isanna.main(["verify", str(root), "--spec", "s1"]))
    assert code == 0 and "s1" in out and "1 from the spec" in out


def test_verify_reports_already_shipped_when_an_unimplemented_specs_commands_pass(tmp_path):
    # THE SIGNAL. A spec nobody has implemented, whose own acceptance commands already pass,
    # is describing something that already exists. Saying only "VERIFIED" here is how a shipped
    # deliverable gets rebuilt from a spec that still reads `planned`.
    root = _spec_project(tmp_path, status="planned", task_cmd="python3 -c 'raise SystemExit(0)'")
    code, out = _run(lambda: isanna.main(["verify", str(root), "--spec", "s1"]))
    assert code == 0 and "VERIFIED" in out
    assert "ALREADY-SHIPPED" in out and "planned" in out


def test_verify_stays_quiet_about_already_shipped_once_a_spec_is_implemented(tmp_path):
    # Past `implementing`, a green is the EXPECTED outcome and carries no information. Printing
    # the advisory on every passing verify would train the reader to skip it.
    root = _spec_project(tmp_path, status="verified", task_cmd="python3 -c 'raise SystemExit(0)'")
    code, out = _run(lambda: isanna.main(["verify", str(root), "--spec", "s1"]))
    assert code == 0 and "VERIFIED" in out and "ALREADY-SHIPPED" not in out


def test_verify_says_nothing_about_already_shipped_when_every_own_command_failed(tmp_path):
    root = _spec_project(tmp_path, status="planned", task_cmd="python3 -c 'raise SystemExit(1)'")
    code, out = _run(lambda: isanna.main(["verify", str(root), "--spec", "s1"]))
    assert code == 1 and "REJECTED" in out and "SHIPPED" not in out


def test_already_shipped_still_fires_when_only_a_project_default_failed(tmp_path):
    # Raised by independent review (F-C): AC-R3-1 read "passes every command", but the advisory
    # has always keyed on the spec's OWN ratio. Pinning the behavior the criterion now states.
    # Suppressing the advisory because the repo's generic suite happened to be red would hide
    # the signal in exactly the case the reader most needs it -- the spec's own evidence is
    # green, so the deliverable probably exists, whatever the repo-wide suite is doing.
    root = _spec_project(tmp_path, status="planned",
                         task_cmd="python3 -c 'raise SystemExit(0)'",
                         project_cmd="python3 -c 'raise SystemExit(1)'")
    code, out = _run(lambda: isanna.main(["verify", str(root), "--spec", "s1"]))
    assert code == 1 and "REJECTED" in out
    assert "ALREADY-SHIPPED" in out and "planned" in out


def test_verify_spec_scope_names_an_unknown_spec_rather_than_blaming_its_tasks_yaml(tmp_path):
    # Raised by independent review (F-E): a typo used to be reported as "declares no verify
    # commands of its own", sending the reader to audit a tasks.yaml that does not exist.
    # Exit 2 and stderr, matching the sibling "no specs under <dir>" case: a bad argument is an
    # operator error, not a verdict about a spec. _run only captures stdout, so stderr is
    # redirected here explicitly.
    root = _spec_project(tmp_path, status="planned", task_cmd="python3 -c 'raise SystemExit(0)'")
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        code, out = _run(lambda: isanna.main(["verify", str(root), "--spec", "s-1"]))
    assert code == 2, out
    assert "no spec" in err.getvalue() and "s-1" in err.getvalue()
    assert "declares no verify commands" not in err.getvalue() + out


def test_verify_refuses_an_empty_spec_name_instead_of_mixing_scopes(tmp_path):
    # Raised by independent review (N1): `--spec ""` was falsy for spec SELECTION (so every
    # spec's commands were collected) but not-None for the SCOPE flag (so the output claimed to
    # be about one spec and read the status of a spec named ""). Two scopes in one run.
    root = _spec_project(tmp_path, status="planned", task_cmd="python3 -c 'raise SystemExit(0)'")
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        code, out = _run(lambda: isanna.main(["verify", str(root), "--spec", ""]))
    assert code == 2, out
    assert "empty name" in err.getvalue()
    assert "VERIFIED" not in out and "from the spec itself" not in out


# --- the partial case ---------------------------------------------------------
#
# The clean-sweep threshold missed the case that matters most. Measured on the real spec
# a real planned spec: 15 of its 20 own
# acceptance commands PASSED -- beta setup, agent-value onboarding, the install matrix,
# the Playwright suite -- and the 5 failures were environmental (a repo that no longer
# exists, a container that wasn't running). It printed REJECTED and nothing else.
# A spec that is three-quarters built is the most expensive thing to hand an implementer,
# and it was exactly the shape the advisory stayed silent about.


def _multi_task_spec(tmp_path: Path, *, status: str, passing: int, failing: int) -> Path:
    spec = tmp_path / ".builder" / "specs" / "s1"
    spec.mkdir(parents=True)
    (tmp_path / ".builder" / "setup-decisions.yaml").write_text(
        "commands:\n  default:\n    test: \"python3 -c 'raise SystemExit(0)'\"\n", encoding="utf-8")
    (spec / "spec.yaml").write_text(f"name: s1\nstatus: {status}\ncurrent_phase: plan\n", encoding="utf-8")
    lines = ["artifact: tasks", "tasks:"]
    for i in range(passing):
        lines += [f"  - id: T{i + 1}", "    verify:",
                  f"      - command: \"python3 -c 'import sys; sys.exit(0)' # pass{i}\""]
    for i in range(failing):
        lines += [f"  - id: F{i + 1}", "    verify:",
                  f"      - command: \"python3 -c 'import sys; sys.exit(1)' # fail{i}\""]
    (spec / "tasks.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return tmp_path


def test_verify_flags_a_majority_pass_on_an_unimplemented_spec(tmp_path):
    # 3 of 4 of its OWN commands pass on a spec still marked `planned`. Silence here is
    # how a three-quarters-built spec gets handed to someone to build a second time.
    root = _multi_task_spec(tmp_path, status="planned", passing=3, failing=1)
    code, out = _run(lambda: isanna.main(["verify", str(root), "--spec", "s1"]))
    assert code == 1 and "REJECTED" in out
    assert "PARTIALLY-SHIPPED" in out and "3 of 4" in out


def test_verify_stays_quiet_when_only_a_minority_passes(tmp_path):
    # Noise control. Spec tasks routinely carry generic checks (`deno fmt --check`,
    # typecheck) that pass in ANY repo, shipped or not. A low ratio is not evidence, and
    # an advisory that fired on it would be trained away within a week.
    root = _multi_task_spec(tmp_path, status="planned", passing=1, failing=3)
    code, out = _run(lambda: isanna.main(["verify", str(root), "--spec", "s1"]))
    assert code == 1 and "SHIPPED" not in out


def test_verify_advisory_counts_only_the_specs_own_commands(tmp_path):
    # The project default command passes here and must NOT count toward the ratio.
    # Folding it in inflates every spec's score by the repo's generic suite -- the same
    # confusion between project evidence and spec evidence this surface exists to refuse.
    # 1 of 3 OWN commands pass, so this stays silent despite 2 of 4 passing overall.
    root = _multi_task_spec(tmp_path, status="planned", passing=1, failing=2)
    code, out = _run(lambda: isanna.main(["verify", str(root), "--spec", "s1"]))
    assert code == 1 and "SHIPPED" not in out


def test_verify_partial_advisory_respects_an_implemented_status(tmp_path):
    root = _multi_task_spec(tmp_path, status="verified", passing=3, failing=1)
    code, out = _run(lambda: isanna.main(["verify", str(root), "--spec", "s1"]))
    assert code == 1 and "SHIPPED" not in out


def test_verify_whole_project_scope_never_consults_tasks_yaml(tmp_path):
    # Whole-project verify is the headline verb and its meaning -- run this repo's gate -- is
    # correct as it stands. A spec's acceptance commands are environment-bound and heavy; pulling
    # every spec's into every invocation would redefine the verb for everyone. The failing
    # tasks.yaml command below must not be reached.
    root = _spec_project(tmp_path, status="planned", task_cmd="python3 -c 'raise SystemExit(1)'")
    code, out = _run(lambda: isanna.main(["verify", str(root)]))
    assert code == 0 and "VERIFIED" in out


def test_verify_on_a_missing_directory_is_an_error_not_a_pass(tmp_path):
    code, _ = _run(lambda: isanna.main(["verify", str(tmp_path / "nope")]))
    assert code == 2


def test_sync_readmit_missing_target_fails_closed(tmp_path):
    code, _ = _run(lambda: isanna.main([
        "sync-readmit", "--root", str(tmp_path), "--spec", "missing",
    ]))
    assert code == 2


def test_release_delegation_runs(tmp_path):
    # `isanna release status` delegates to planning.py (a dataclass-bearing module). With no
    # releases/ dir it must report the empty case cleanly, not crash.
    (tmp_path / ".builder").mkdir()
    code, out = _run(lambda: isanna.main([
        "release", "status", "--root", str(tmp_path), "--projects-root", str(tmp_path),
    ]))
    assert "Traceback" not in out and code == 0
    assert "no releases" in out.lower()


def test_release_backlog_summary_delegates(tmp_path):
    (tmp_path / ".builder" / "releases").mkdir(parents=True)
    (tmp_path / ".builder" / "intents" / "a").mkdir(parents=True)
    (tmp_path / ".builder" / "releases" / "demo.yaml").write_text(
        "release: demo\nproduct: demo\ntitle: demo\nstatus: active\nintents:\n  - a\n",
        encoding="utf-8",
    )
    (tmp_path / ".builder" / "intents" / "a" / "intent.yaml").write_text(
        "artifact: intent-object\nintent: a\ntitle: A\nstatus: accepted\nproblem: p\nwhy: w\n"
        "success_criteria:\n  - id: sc-1\n    statement: s\nnon_goals:\n  - n\n"
        "ssot_delta:\n  capabilities:\n    - target: capability.alpha\n      change: create\n  behaviors: []\n  journeys: []\n"
        "specs: []\n",
        encoding="utf-8",
    )
    code, out = _run(lambda: isanna.main([
        "release", "backlog-summary", "--root", str(tmp_path), "--projects-root", str(tmp_path),
    ]))
    assert code == 0
    assert "capability.alpha: a [demo] accepted create" in out


def test_model_delegation_does_not_crash_on_dataclass_load(tmp_path):
    # `isanna model` loads model.py via importlib. model.py defines @dataclass types, which look
    # themselves up in sys.modules during class creation -- so the loaded module MUST be registered
    # before exec, or it dies with `NoneType has no attribute __dict__` before doing any work.
    # This asserts the delegation runs far enough to emit model's own fail-closed message.
    code, out = _run(lambda: isanna.main(["model", "verify", "--root", str(tmp_path)]))
    assert "Traceback" not in out
    assert code != 0  # no built model -> fails closed, never a cheerful 0/0


# ----------------------------------------------------------------- isanna demo (gate regression)


def test_demo_catches_the_liar_and_accepts_honest_work():
    # THE regression test for the whole thesis. main() returns 0 only when BOTH acts land: the
    # agent that changed nothing is rejected, and the agent that did the work is verified.
    code, _ = _run(lambda: demo.main([]))
    assert code == 0


def test_demo_fails_loudly_if_the_gate_lets_the_lie_through():
    # Mutation-audit gap (C11): the coarse exit-0 test and the BOTH-gates test both survive a
    # `caught = True` hardcode in main() — neither proves main's verdict is DERIVED from the real
    # gate results. If the gate ever PASSED the liar, a hardcoded 'caught' would print REJECTED and
    # return 0, laundering a broken gate as green — the exact lie this product exists to catch.
    # Force the act-1 gates to PASS the liar; main() MUST fail loudly (return 1, not a false green).
    saved = demo.run_act
    calls = {"n": 0}

    def fake_run_act(root, **kw):
        calls["n"] += 1
        if calls["n"] == 1:      # act 1 (the liar) — pretend BOTH host gates passed it
            return (True, True)
        return saved(root, **kw)  # act 2 (honest) — the real gates

    demo.run_act = fake_run_act
    try:
        code, out = _run(lambda: demo.main([]))
    finally:
        demo.run_act = saved
    assert code == 1, "a lie that slips the gate must never yield a green demo"
    assert "THE LIE GOT THROUGH" in out or "gate is broken" in out


def test_demo_liar_is_rejected_by_BOTH_gates(tmp_path):
    # Belt and braces: assert the two independent reasons, not just the final verdict. A demo
    # that "passed" because one gate accidentally carried the other would be false comfort.
    root = demo.build_project(tmp_path)
    head, paths = demo._head(root), demo._source_paths(root)
    hv, sd = _run(lambda: demo.run_act(root, title="t", narrative="n", agent_says="SUCCEEDED",
                                       pre_head=head, pre_paths=paths))[0]
    assert hv is False, "the failing test suite must be caught host-side"
    assert sd is False, "the empty diff must be caught host-side"


def test_demo_baseline_is_the_dirty_set_not_every_file(tmp_path):
    # The bug this pins: seeding the baseline with every .py file in the repo makes the diff gate
    # reject HONEST work (nothing is ever "new"), which would have shipped a demo whose act 2
    # failed. On a clean checkout the pre-turn baseline is empty.
    root = demo.build_project(tmp_path)
    assert demo._source_paths(root) == set()
