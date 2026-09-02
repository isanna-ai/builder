"""The dispatcher never enforced ssot-delta.yaml, so forward-only sync could not work.

Measured on a real spec in a curated repo: it
advanced spec -> spec-review -> plan and reached `status: planned` with NO ssot-delta.yaml.
The requirement in `_validators/sync_artifacts.py` (_SYNC_PHASE_REQUIRED_STATUSES) only fires
when `validate-spec.py` is run, and the pipeline never runs it as an advancement gate —
running it afterwards on that spec reported `ssot-delta.yaml: required for spec status
planned` plus 114 other errors.

The break chain, which is why this matters more than tidiness:

    no ssot-delta
      -> sync_isolated false (attempt_runner.py)
      -> no per-spec worktree
      -> implementation-baseline.yaml written with worktree_isolated: false
      -> validate_scope_evidence rejects ("worktree_isolated must be true")
      -> sync REFUSES, permanently

That is the real reason a fully curated repo can carry 1 ssot-delta across 49 specs and never sync,
in a repo whose adapter and behavioral SSOT are fully curated. Not "those specs predate the
gate" — there is no gate.

Staged like every other gate in this repo (BUILDER_TRACE_COVERAGE, BUILDER_VERIFY_LINT,
BUILDER_SPEC_BOOKKEEPING): `warn` is the DEFAULT and never blocks, `enforce` refuses, and an
unrecognized value stays at warn rather than silently enforcing. Default must be warn because
enforcing today would stall advancement in every repo at once.
"""

from __future__ import annotations

import os
from pathlib import Path

from _dispatch_runtime.phase_runtime import validate_phase_completion

_ENV = "BUILDER_REQUIRE_SSOT_DELTA"


def _planned_spec(tmp_path: Path, *, delta: bool) -> Path:
    """A spec that has legitimately completed `plan` — every other gate satisfied — so the
    only variable under test is the presence of ssot-delta.yaml."""
    sd = tmp_path / "specs" / "demo"
    sd.mkdir(parents=True, exist_ok=True)
    (sd / "spec.yaml").write_text("name: demo\nstatus: planned\ncurrent_phase: implement\n")
    (sd / "tasks.yaml").write_text("tasks: []\n")
    (sd / "handoff.yaml").write_text(
        "next_phase: implement\nspec: demo\nready: true\ncompleted_phase: plan\n"
    )
    (sd / "phase-log.yaml").write_text(
        'phases:\n  - phase: plan\n    completed: "2026-06-10T00:00:00Z"\n    outcome: SUCCEEDED\n'
    )
    if delta:
        (sd / "ssot-delta.yaml").write_text("capabilities: []\nbehaviors: []\njourneys: []\n")
    return tmp_path / "specs"


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


# --- per-repo enforcement ------------------------------------------------------
#
# BUILDER_REQUIRE_SSOT_DELTA was env-only, so "enforce in these two repos" was not
# expressible: the setting followed whoever's shell ran the dispatcher, not the repo whose spec
# was advancing. Enforcement has to travel with the REPO, since the 22 wired repos are at
# different stages of SSOT backfill.
#
# Same contract as pipeline.archive_require_sync (f2056b9), and deliberately the SAME resolver
# underneath rather than a second copy: env wins, an EMPTY env value counts as unset, a typo
# stays at warn, a malformed dispatch.yaml degrades to warn.


def _repo_layout_spec(tmp_path: Path, *, delta: bool) -> Path:
    """Same spec as _planned_spec but in the REAL tree shape: <repo>/.builder/specs/demo.

    The flat `tmp_path/specs` fixture used by the older tests here cannot exercise per-repo
    config at all — the repo root is derived from the specs dir, so the layout has to match
    production or the derivation is untestable."""
    specs = tmp_path / ".builder" / "specs"
    sd = specs / "demo"
    sd.mkdir(parents=True, exist_ok=True)
    (sd / "spec.yaml").write_text("name: demo\nstatus: planned\ncurrent_phase: implement\n")
    (sd / "tasks.yaml").write_text("tasks: []\n")
    (sd / "handoff.yaml").write_text(
        "next_phase: implement\nspec: demo\nready: true\ncompleted_phase: plan\n"
    )
    (sd / "phase-log.yaml").write_text(
        'phases:\n  - phase: plan\n    completed: "2026-06-10T00:00:00Z"\n    outcome: SUCCEEDED\n'
    )
    if delta:
        (sd / "ssot-delta.yaml").write_text("capabilities: []\nbehaviors: []\njourneys: []\n")
    return specs


def _dispatch_yaml(tmp_path: Path, value: str) -> None:
    """dispatch.yaml sits beside specs/, i.e. at <repo>/.builder/dispatch.yaml."""
    d = tmp_path / ".builder"
    d.mkdir(parents=True, exist_ok=True)
    (d / "dispatch.yaml").write_text(f"pipeline:\n  require_ssot_delta: {value}\n",
                                     encoding="utf-8")


def test_the_repo_key_alone_can_enforce(tmp_path):
    specs = _repo_layout_spec(tmp_path, delta=False)
    _dispatch_yaml(tmp_path, "enforce")
    r = _with_env(None, lambda: validate_phase_completion(specs, "demo", "plan"))
    assert not r.passed and "ssot-delta.yaml" in r.reason


def test_the_repo_key_alone_can_stay_at_warn(tmp_path):
    specs = _repo_layout_spec(tmp_path, delta=False)
    _dispatch_yaml(tmp_path, "warn")
    r = _with_env(None, lambda: validate_phase_completion(specs, "demo", "plan"))
    assert r.passed, r.reason


def test_the_env_var_wins_over_the_repo_key(tmp_path):
    # Env is the narrower, more deliberate scope: override one run without editing a committed
    # file.
    specs = _repo_layout_spec(tmp_path, delta=False)
    _dispatch_yaml(tmp_path, "enforce")
    r = _with_env("warn", lambda: validate_phase_completion(specs, "demo", "plan"))
    assert r.passed, r.reason


def test_an_empty_env_value_falls_through_to_the_repo_key(tmp_path):
    # An empty `export BUILDER_REQUIRE_SSOT_DELTA=` in a profile must not silently disable
    # every repo's committed setting.
    specs = _repo_layout_spec(tmp_path, delta=False)
    _dispatch_yaml(tmp_path, "enforce")
    r = _with_env("", lambda: validate_phase_completion(specs, "demo", "plan"))
    assert not r.passed


def test_a_typo_in_the_repo_key_does_not_silently_enforce(tmp_path):
    specs = _repo_layout_spec(tmp_path, delta=False)
    _dispatch_yaml(tmp_path, "enfroce")
    r = _with_env(None, lambda: validate_phase_completion(specs, "demo", "plan"))
    assert r.passed, r.reason


def test_a_malformed_dispatch_yaml_degrades_to_warn(tmp_path):
    specs = _repo_layout_spec(tmp_path, delta=False)
    (tmp_path / ".builder").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".builder" / "dispatch.yaml").write_text("pipeline: [not, a, mapping\n", encoding="utf-8")
    r = _with_env(None, lambda: validate_phase_completion(specs, "demo", "plan"))
    assert r.passed, r.reason


def test_both_gates_share_one_resolver(tmp_path):
    # Two copies of env-then-repo-then-default would drift, and the drift would be invisible
    # until one gate behaved differently from the other on the same repo.
    from _dispatch_runtime import phase_runtime
    from _dispatch_runtime.staged_gate import staged_gate_enforced
    import _ssot_audit

    assert phase_runtime.staged_gate_enforced is staged_gate_enforced
    assert _ssot_audit.staged_gate_enforced is staged_gate_enforced


def test_enforce_refuses_plan_completion_without_an_ssot_delta(tmp_path):
    specs = _planned_spec(tmp_path, delta=False)
    r = _with_env("enforce", lambda: validate_phase_completion(specs, "demo", "plan"))
    assert not r.passed
    assert "ssot-delta.yaml" in r.reason


def test_enforce_allows_plan_completion_when_the_delta_is_present(tmp_path):
    specs = _planned_spec(tmp_path, delta=True)
    r = _with_env("enforce", lambda: validate_phase_completion(specs, "demo", "plan"))
    assert r.passed, r.reason


def test_warn_is_the_default_and_never_blocks(tmp_path):
    # Most repos would stall on day one if this defaulted to enforce.
    specs = _planned_spec(tmp_path, delta=False)
    r = _with_env(None, lambda: validate_phase_completion(specs, "demo", "plan"))
    assert r.passed, r.reason


def test_an_unrecognized_value_does_not_silently_enforce(tmp_path):
    specs = _planned_spec(tmp_path, delta=False)
    r = _with_env("enfroce", lambda: validate_phase_completion(specs, "demo", "plan"))
    assert r.passed, r.reason


def test_the_refusal_names_the_status_that_requires_the_delta(tmp_path):
    # "required" without saying WHICH status required it sends the reader hunting.
    specs = _planned_spec(tmp_path, delta=False)
    r = _with_env("enforce", lambda: validate_phase_completion(specs, "demo", "plan"))
    assert "planned" in r.reason


def test_the_gate_reuses_the_validator_status_set_rather_than_restating_it(tmp_path):
    # Two copies of this list would drift, and the drift would silently reopen the hole.
    from _dispatch_runtime import phase_runtime
    from _validators.sync_artifacts import _SYNC_PHASE_REQUIRED_STATUSES

    assert phase_runtime.SSOT_DELTA_REQUIRED_STATUSES is _SYNC_PHASE_REQUIRED_STATUSES


def test_a_pre_delta_phase_is_unaffected_by_the_gate(tmp_path):
    # `spec` targets status `specified`, which is NOT in the required set — a spec must be
    # able to reach the plan phase before it can be expected to declare a delta.
    sd = tmp_path / "specs" / "demo"
    sd.mkdir(parents=True, exist_ok=True)
    (sd / "spec.yaml").write_text("name: demo\nstatus: specified\ncurrent_phase: plan\n")
    (sd / "requirements.yaml").write_text("artifact: requirements\n")
    (sd / "design.yaml").write_text("artifact: design\n")
    (sd / "handoff.yaml").write_text(
        "next_phase: plan\nspec: demo\nready: true\ncompleted_phase: spec\n"
    )
    (sd / "phase-log.yaml").write_text(
        'phases:\n  - phase: spec\n    completed: "2026-06-10T00:00:00Z"\n    outcome: SUCCEEDED\n'
    )
    r = _with_env("enforce", lambda: validate_phase_completion(tmp_path / "specs", "demo", "spec"))
    assert r.passed, r.reason
