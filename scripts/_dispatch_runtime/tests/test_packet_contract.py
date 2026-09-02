"""P0.1 — the self-contained runner-packet contract.

The runner packet is the implementer's EXCLUSIVE runtime interface, so it must carry a
normative description of WHAT to build (objective / steps / done_when / allowed_change_files
+ ids), copied VERBATIM from the approved task by the EMITTER. Under
BUILDER_PACKET_CONTRACT=enforce a packet missing that contract is REJECTED; default off is a
strict no-op (unchanged shape + behavior).

Shim-safe: no pytest.raises / monkeypatch; env via pop/restore; data is injected on disk or
built as plain dicts.
"""

from __future__ import annotations

import os

from _dispatch_runtime.lane_common import SessionState, Work, _packet_contract_gate
from _dispatch_runtime.packet_contract import (
    REQUIRED_CONTRACT_FIELDS,
    apply_contract,
    contract_fields_from_task,
    links_from_traceability,
    missing_contract_fields,
    validate_packet_contract,
)
from _dispatch_runtime.phase_runtime import _format_task_section, build_phase_goal

_ENV = "BUILDER_PACKET_CONTRACT"


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


def _work(tmp_path, *, runner_task_ref=None, spec_id="demo", phase="implement"):
    return Work(
        work_id="w1", spec_id=spec_id, phase=phase,
        project_dir=tmp_path, specs_dir=tmp_path / ".builder" / "specs",
        runner_task_ref=runner_task_ref, capability_class=None,
        queue_root=tmp_path / ".builder" / "dispatch-queue",
        log_path=tmp_path / "log",
    )


def _approved_task():
    return {
        "id": "T1",
        "title": "Add a foo guard to bar()",
        "repo": "demo",
        "files": ["src/bar.py", "tests/test_bar.py"],
        "tdd": {"mode": "required"},
        "steps": [
            {"text": "Write a failing test for the guard"},
            {"text": "Implement the guard"},
        ],
        "verify": [{"command": "pytest -q tests/test_bar.py", "proves": ["AC-R1-1"]}],
        "done_when": "bar() rejects a foo input with ValueError",
        "depends_on": [],
        "parallel_with": [],
        "proves": ["AC-R1-1"],
    }


# --- (1) the emitter populates the contract fields from a task --------------

def test_emitter_populates_contract_fields_verbatim():
    fields = contract_fields_from_task(_approved_task())
    assert fields["objective"] == "Add a foo guard to bar()"           # <- task title
    assert fields["steps"] == [                                         # <- task steps[].text
        "Write a failing test for the guard",
        "Implement the guard",
    ]
    assert fields["done_when"] == ["bar() rejects a foo input with ValueError"]  # string -> 1-list
    assert fields["allowed_change_files"] == ["src/bar.py", "tests/test_bar.py"]  # <- task files
    assert fields["required_diff_classes"] == ["production", "test"]    # <- inferred from tdd
    assert fields["acceptance_ids"] == ["AC-R1-1"]                      # <- task proves


def test_emitter_infers_diff_classes_from_tdd_mode():
    # A behavior task -> [production, test]; the finer packet tdd_mode narrows exempt cases.
    assert contract_fields_from_task(
        {"title": "x", "files": ["a.py"], "tdd": {"mode": "required"}}
    )["required_diff_classes"] == ["production", "test"]
    packet = {"task_id": "T2", "tdd_mode": "exempt_config_only"}
    task = {"title": "x", "files": ["cfg.yaml"], "tdd": {"mode": "exempt"},
            "steps": [{"text": "edit cfg"}], "done_when": "cfg updated"}
    assert apply_contract(packet, task)["required_diff_classes"] == ["config"]


def test_emitter_reads_ids_from_traceability_links():
    trace = {
        "design_links": [{"design_id": "D1", "task_ids": ["T1"]}],
        "requirement_links": [{"requirement_id": "R1", "design_ids": ["D1"]}],
    }
    links = links_from_traceability(trace, "T1")
    assert links == {"requirement_ids": ["R1"], "design_ids": ["D1"]}
    fields = contract_fields_from_task(_approved_task(), links=links)
    assert fields["requirement_ids"] == ["R1"]
    assert fields["design_ids"] == ["D1"]


def test_apply_contract_fills_absent_but_preserves_authored():
    task = _approved_task()
    packet = {"task_id": "T1", "tdd_mode": "required", "objective": "Authored objective"}
    filled = apply_contract(packet, task)
    assert filled["objective"] == "Authored objective"                 # authored value WINS
    assert filled["steps"] == ["Write a failing test for the guard", "Implement the guard"]
    assert filled["done_when"] == ["bar() rejects a foo input with ValueError"]
    assert filled["allowed_change_files"] == ["src/bar.py", "tests/test_bar.py"]
    assert "steps" not in packet                                       # source packet not mutated


# --- (2) enforce rejects a missing contract; off is a strict no-op ----------

def test_validate_off_is_noop_enforce_rejects_missing():
    complete = {"task_id": "T1", "objective": "o", "steps": ["s"],
                "done_when": ["d"], "allowed_change_files": ["f"]}
    missing = {"task_id": "T1", "objective": "o"}  # steps / done_when / allowed_change_files absent

    # explicit off -> (None, "") for BOTH shapes: no inspection, no rejection.
    assert _with_env("off", lambda: validate_packet_contract(missing)) == (None, "")
    assert _with_env("off", lambda: validate_packet_contract(complete)) == (None, "")
    # DEFAULT (unset) now enforces: the underinformed packet is rejected, the complete one passes.
    passed, reason = _with_env(None, lambda: validate_packet_contract(missing))
    assert passed is False and "steps" in reason
    assert _with_env(None, lambda: validate_packet_contract(complete)) == (True, "")
    # An unrecognized value no longer fails to OFF -- that would let a typo silently strip the
    # gate. It falls back to the DEFAULT (enforce) and warns.
    passed, reason = _with_env("banana", lambda: validate_packet_contract(missing))
    assert passed is False and "steps" in reason

    def _enforce():
        v, reason = validate_packet_contract(missing)
        assert v is False
        assert "T1" in reason
        for f in ("steps", "done_when", "allowed_change_files"):
            assert f in reason
        assert validate_packet_contract(complete) == (True, "")

    _with_env("enforce", _enforce)


def test_missing_contract_fields_is_shape_safe():
    assert missing_contract_fields({"objective": "o"}) == ["steps", "done_when", "allowed_change_files"]
    assert missing_contract_fields({}) == list(REQUIRED_CONTRACT_FIELDS)
    assert missing_contract_fields("not-a-dict") == list(REQUIRED_CONTRACT_FIELDS)


def test_declaration_path_guard_rejects_canonical_paths():
    packet = {
        "task_id": "T1",
        "objective": "o",
        "steps": ["s"],
        "done_when": ["d"],
        "allowed_change_files": [
            "builder.yaml",
            ".builder-home/projects/portfolio/product.yaml",
            "projects/portfolio/releases/wave-1.yaml",
        ],
    }
    passed, reason = _with_env("enforce", lambda: validate_packet_contract(packet))
    assert passed is False
    assert "Builder Home declaration path" in reason


def test_declaration_path_guard_accepts_clean_allowed_files():
    packet = {
        "task_id": "T1",
        "objective": "o",
        "steps": ["s"],
        "done_when": ["d"],
        "allowed_change_files": ["src/bar.py", "tests/test_bar.py", ".builder/specs/demo/spec.yaml"],
    }
    assert _with_env("enforce", lambda: validate_packet_contract(packet)) == (True, "")


def _seed_batch_and_packet(tmp_path, *, packet_body, with_tasks=False):
    """Write a degenerate one-task phase-batch + its per-task packet; optionally the approved
    tasks.yaml + traceability.yaml the emitter fills from. Returns the batch runner_task_ref."""
    runs = tmp_path / ".builder" / "specs" / "demo" / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    (runs / "phase-implement.yaml").write_text(
        "phase_id: implement\nspec: demo\ntasks:\n  - task_ref: .builder/specs/demo/runs/task-T1.yaml\n",
        encoding="utf-8",
    )
    (runs / "task-T1.yaml").write_text(packet_body, encoding="utf-8")
    if with_tasks:
        spec_dir = tmp_path / ".builder" / "specs" / "demo"
        spec_dir.joinpath("tasks.yaml").write_text(
            "artifact: tasks\ntitle: Demo\nspec: demo\ntasks:\n"
            "  - id: T1\n"
            "    title: Add a foo guard to bar()\n"
            "    repo: demo\n"
            "    files:\n      - src/bar.py\n      - tests/test_bar.py\n"
            "    tdd:\n      mode: required\n"
            "    steps:\n      - text: Write a failing test for the guard\n"
            "      - text: Implement the guard\n"
            "    verify:\n      - command: pytest -q tests/test_bar.py\n        proves:\n          - AC-R1-1\n"
            "    done_when: bar() rejects a foo input with ValueError\n"
            "    depends_on: []\n    parallel_with: []\n    proves:\n      - AC-R1-1\n",
            encoding="utf-8",
        )
        spec_dir.joinpath("traceability.yaml").write_text(
            "artifact: traceability\nspec: demo\n"
            "requirement_links:\n  - requirement_id: R1\n    design_ids: [D1]\n"
            "design_links:\n  - design_id: D1\n    task_ids: [T1]\n"
            "task_links:\n  - task_id: T1\n    files:\n      - path: src/bar.py\n        relevance: primary\n    evidence_ids: [E1]\n",
            encoding="utf-8",
        )
    return ".builder/specs/demo/runs/phase-implement.yaml"


# A legacy packet: file/verify load plan only, NO contract fields.
_LEGACY_PACKET = (
    "task_id: T1\n"
    "target_model_profile: flagship_commercial\n"
    "tdd_mode: required\n"
    "files:\n  - path: src/bar.py\n    mode: full\n    load_priority: must\n"
    "verify_commands:\n  - pytest -q tests/test_bar.py  # must exit 0\n"
)


def test_gate_off_is_explicit_opt_out(tmp_path):
    ref = _seed_batch_and_packet(tmp_path, packet_body=_LEGACY_PACKET, with_tasks=False)
    work = _work(tmp_path, runner_task_ref=ref)
    # Explicit off -> strict no-op even though the packet has no contract fields.
    assert _with_env("off", lambda: _packet_contract_gate(work, "implement")) == (None, "")


def test_gate_rejects_underinformed_packet_by_default(tmp_path):
    # Flag unset -> the legacy, contract-less packet is REJECTED. An implementer handed no
    # objective and no done-when can pass a task without implementing it; that is the hole.
    ref = _seed_batch_and_packet(tmp_path, packet_body=_LEGACY_PACKET, with_tasks=False)
    work = _work(tmp_path, runner_task_ref=ref)
    passed, reason = _with_env(None, lambda: _packet_contract_gate(work, "implement"))
    assert passed is False and "objective" in reason


def test_gate_enforce_rejects_underinformed_packet(tmp_path):
    # Legacy packet AND no tasks.yaml to fill from -> genuinely underinformed -> rejected.
    ref = _seed_batch_and_packet(tmp_path, packet_body=_LEGACY_PACKET, with_tasks=False)
    work = _work(tmp_path, runner_task_ref=ref)

    def _enforce():
        passed, reason = _packet_contract_gate(work, "implement")
        assert passed is False
        assert "T1" in reason and "objective" in reason
        # Non-implement phase self-guards to no-op even under enforce.
        assert _packet_contract_gate(work, "verify") == (None, "")

    _with_env("enforce", _enforce)


def test_gate_enforce_passes_when_task_supplies_contract(tmp_path):
    # Same legacy packet, but tasks.yaml supplies the contract -> the emitter fills it ->
    # the runner effectively receives a complete contract -> enforce PASSES.
    ref = _seed_batch_and_packet(tmp_path, packet_body=_LEGACY_PACKET, with_tasks=True)
    work = _work(tmp_path, runner_task_ref=ref)
    assert _with_env("enforce", lambda: _packet_contract_gate(work, "implement")) == (True, "")


# --- (3) existing packet generation still works (default off = unchanged) ----

def test_format_task_section_legacy_packet_unchanged():
    # A legacy packet (no contract fields) renders NONE of the new contract lines and keeps
    # its legacy markers — byte-shape unchanged from before P0.1.
    legacy = {
        "task_id": "T1",
        "tdd_mode": "exempt_config_only",
        "files": [{"path": "cfg.yaml", "load_priority": "must"}],
        "summaries": ["Update the config"],
        "verify_commands": ["test -f cfg.yaml"],
    }
    section = _format_task_section(legacy, ".builder/specs/demo/runs/task-T1.yaml")
    for marker in ("Objective:", "Steps (in order):", "Done when", "Allowed change files", "Traceability:"):
        assert marker not in section
    assert "Files to read and edit (load_priority: must):" in section
    assert "Verify commands" in section


def test_build_phase_goal_without_tasks_is_unchanged(tmp_path):
    # No tasks.yaml -> the fill is a no-op -> the goal carries no contract lines (legacy path).
    ref = _seed_batch_and_packet(tmp_path, packet_body=_LEGACY_PACKET, with_tasks=False)
    goal = build_phase_goal(tmp_path, tmp_path / ".builder" / "specs", "demo", "implement", ref)
    assert "=== TASK T1 ===" in goal
    assert "Objective:" not in goal
    assert "Steps (in order):" not in goal


def test_build_phase_goal_fills_contract_from_tasks(tmp_path):
    # tasks.yaml present -> the dispatcher fills the packet's contract VERBATIM and surfaces it.
    ref = _seed_batch_and_packet(tmp_path, packet_body=_LEGACY_PACKET, with_tasks=True)
    goal = build_phase_goal(tmp_path, tmp_path / ".builder" / "specs", "demo", "implement", ref)
    assert "Objective: Add a foo guard to bar()" in goal
    assert "Steps (in order):" in goal
    assert "1. Write a failing test for the guard" in goal
    assert "Done when (ALL of these must hold" in goal
    assert "bar() rejects a foo input with ValueError" in goal
    assert "Allowed change files (modify ONLY these" in goal
    assert "requirements: R1" in goal
    assert "design: D1" in goal
    assert "acceptance: AC-R1-1" in goal
