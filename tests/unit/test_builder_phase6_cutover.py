from __future__ import annotations

import hashlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from _builder_project_model.cutover import (
    CutoverError,
    CutoverOrchestrator,
    LaunchRequest,
    SyntheticCutoverOperator,
    load_cutover_state,
    write_cutover_state,
)


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _state() -> dict:
    return {
        "schema_version": 1,
        "discovery_mode": "legacy",
        "legacy_daemons": {
            "alpha-repo": {"alive": True, "identity_ok": True, "restore_on_rollback": True},
            "beta-repo": {"alive": True, "identity_ok": True, "restore_on_rollback": True},
        },
        "group_runner": {"alive": True, "identity_ok": True, "restore_on_rollback": True},
        "legacy_pgids": [
            {
                "repo_id": "alpha-repo",
                "alive": True,
                "pgid": 4321,
                "pgid_leader_start_ticks": 99,
                "executable": "/bin/alpha",
                "command_digest": "sha256:alpha",
                "identity_ok": True,
            }
        ],
        "central": {"daemon_alive": False, "watchdog_alive": False, "launch_enabled": False},
        "repo_locks": {"alpha-repo": None, "beta-repo": None},
        "sessions": [],
        "providers": {"claude-code-cli": {"max_sessions": 1}, "codex-cli": {"max_sessions": 2}},
        "active_watchdogs": ["legacy:alpha-repo", "legacy:beta-repo", "legacy:group-runner"],
        "rollback_legacy_watchdogs": [],
    }


def _write_fixture(tmp_path: Path) -> Path:
    path = tmp_path / "cutover-state.json"
    write_cutover_state(path, _state())
    return path


def test_cutover_dry_run_writes_nothing(tmp_path):
    state_path = _write_fixture(tmp_path)
    before = _hash(state_path)

    results = CutoverOrchestrator(SyntheticCutoverOperator(state_path)).run_forward()

    assert _hash(state_path) == before
    assert all(result.dry_run for result in results)
    assert all(result.changed is False for result in results)


def test_cutover_apply_requires_confirmation_for_each_selected_step(tmp_path):
    state_path = _write_fixture(tmp_path)
    before = _hash(state_path)
    orchestrator = CutoverOrchestrator(
        SyntheticCutoverOperator(state_path),
        dry_run=False,
        confirmations={"stop_legacy"},
    )

    try:
        orchestrator.run_forward(steps=("stop_legacy", "prove_legacy_gone"))
    except CutoverError as exc:
        assert str(exc) == "missing-confirmation:prove_legacy_gone"
    else:
        raise AssertionError("expected per-step confirmation refusal")

    state = load_cutover_state(state_path)
    assert _hash(state_path) != before
    assert all(row["alive"] is False for row in state["legacy_daemons"].values())
    assert state["group_runner"]["alive"] is False
    assert state["central"]["daemon_alive"] is False


def test_prove_gone_fails_closed_on_identity_mismatch(tmp_path):
    state_path = _write_fixture(tmp_path)
    state = load_cutover_state(state_path)
    state["legacy_daemons"]["alpha-repo"]["identity_ok"] = False
    state["legacy_daemons"]["alpha-repo"]["alive"] = False
    write_cutover_state(state_path, state)

    try:
        CutoverOrchestrator(SyntheticCutoverOperator(state_path)).run_forward(steps=("prove_legacy_gone",))
    except CutoverError as exc:
        assert str(exc) == "identity-mismatch:legacy:alpha-repo"
    else:
        raise AssertionError("expected identity mismatch refusal")


def test_reconcile_legacy_pgids_fails_closed_on_unsafe_identity(tmp_path):
    state_path = _write_fixture(tmp_path)
    state = load_cutover_state(state_path)
    state["legacy_pgids"][0]["identity_ok"] = False
    write_cutover_state(state_path, state)

    try:
        CutoverOrchestrator(SyntheticCutoverOperator(state_path)).run_forward(steps=("reconcile_legacy_pgids",))
    except CutoverError as exc:
        assert str(exc) == "identity-mismatch:legacy-pgid:0"
    else:
        raise AssertionError("expected legacy pgid refusal")


def test_dual_owner_attempt_fails_loudly(tmp_path):
    state_path = _write_fixture(tmp_path)
    state = load_cutover_state(state_path)
    state["legacy_daemons"]["alpha-repo"]["alive"] = False
    state["legacy_daemons"]["beta-repo"]["alive"] = False
    state["group_runner"]["alive"] = False
    state["legacy_pgids"][0]["alive"] = False
    state["central"]["daemon_alive"] = True
    state["repo_locks"]["alpha-repo"] = "central"
    state["sessions"] = [
        {
            "session_id": "legacy-1",
            "repo_id": "alpha-repo",
            "provider": "claude-code-cli",
            "owner": "legacy",
            "state": "active",
            "consuming": True,
            "identity_ok": True,
        }
    ]
    write_cutover_state(state_path, state)

    try:
        CutoverOrchestrator(SyntheticCutoverOperator(state_path)).run_forward(steps=("reconcile_repo_runtime",))
    except CutoverError as exc:
        assert str(exc) == "dual-owner-refused:session:alpha-repo:claude-code-cli"
    else:
        raise AssertionError("expected dual-owner refusal")


def test_full_cutover_then_caps_hold_and_watchdogs_collapse(tmp_path):
    state_path = _write_fixture(tmp_path)
    operator = SyntheticCutoverOperator(state_path)
    confirmations = set(
        [
            "stop_legacy",
            "prove_legacy_gone",
            "reconcile_legacy_pgids",
            "start_central",
            "acquire_repo_locks",
            "reconcile_repo_runtime",
            "replace_watchdogs",
        ]
    )
    orchestrator = CutoverOrchestrator(operator, dry_run=False, confirmations=confirmations)

    results = orchestrator.run_forward()
    state = load_cutover_state(state_path)

    assert [row.step for row in results] == list(
        [
            "stop_legacy",
            "prove_legacy_gone",
            "reconcile_legacy_pgids",
            "start_central",
            "acquire_repo_locks",
            "reconcile_repo_runtime",
            "replace_watchdogs",
        ]
    )
    assert state["central"]["daemon_alive"] is True
    assert state["central"]["watchdog_alive"] is True
    assert state["repo_locks"] == {"alpha-repo": "central", "beta-repo": "central"}
    assert state["active_watchdogs"] == ["central"]
    assert state["discovery_mode"] == "home"

    launch_result = operator.admit_central_launches(
        [
            LaunchRequest(session_id="claude-1", repo_id="alpha-repo", provider="claude-code-cli"),
            LaunchRequest(session_id="claude-2", repo_id="beta-repo", provider="claude-code-cli"),
            LaunchRequest(session_id="codex-1", repo_id="alpha-repo", provider="codex-cli"),
            LaunchRequest(session_id="codex-2", repo_id="beta-repo", provider="codex-cli"),
            LaunchRequest(session_id="codex-3", repo_id="beta-repo", provider="codex-cli"),
        ],
        dry_run=False,
    )
    state = load_cutover_state(state_path)

    assert "admitted:claude-code-cli:claude-1" in launch_result.details
    assert "refused-cap:claude-code-cli:claude-2" in launch_result.details
    assert "admitted:codex-cli:codex-1" in launch_result.details
    assert "admitted:codex-cli:codex-2" in launch_result.details
    assert "refused-cap:codex-cli:codex-3" in launch_result.details
    assert len([row for row in state["sessions"] if row["provider"] == "claude-code-cli" and row["owner"] == "central"]) == 1
    assert len([row for row in state["sessions"] if row["provider"] == "codex-cli" and row["owner"] == "central"]) == 2


def test_cutover_rollback_restores_legacy_ownership_and_keeps_canonical_home_files(tmp_path):
    state_path = _write_fixture(tmp_path)
    home_file = tmp_path / ".builder-home" / "builder.yaml"
    home_file.parent.mkdir(parents=True, exist_ok=True)
    home_file.write_text("schema_version: 1\nhome_id: fixture\n", encoding="utf-8")

    operator = SyntheticCutoverOperator(state_path)
    forward = CutoverOrchestrator(
        operator,
        dry_run=False,
        confirmations=set(
            [
                "stop_legacy",
                "prove_legacy_gone",
                "reconcile_legacy_pgids",
                "start_central",
                "acquire_repo_locks",
                "reconcile_repo_runtime",
                "replace_watchdogs",
            ]
        ),
    )
    forward.run_forward()
    operator.admit_central_launches([LaunchRequest(session_id="codex-1", repo_id="alpha-repo", provider="codex-cli")], dry_run=False)

    rollback = CutoverOrchestrator(
        operator,
        dry_run=False,
        confirmations=set(
            [
                "stop_new_central_launches",
                "reconcile_central_groups",
                "stop_central",
                "release_repo_locks",
                "restore_legacy_watchdogs",
                "restore_legacy_daemons",
                "select_legacy_discovery",
            ]
        ),
    )
    rollback.run_rollback()
    state = load_cutover_state(state_path)

    assert state["central"]["daemon_alive"] is False
    assert state["central"]["watchdog_alive"] is False
    assert state["central"]["launch_enabled"] is False
    assert state["repo_locks"] == {"alpha-repo": None, "beta-repo": None}
    assert state["active_watchdogs"] == ["legacy:alpha-repo", "legacy:beta-repo", "legacy:group-runner"]
    assert state["legacy_daemons"]["alpha-repo"]["alive"] is True
    assert state["legacy_daemons"]["beta-repo"]["alive"] is True
    assert state["group_runner"]["alive"] is True
    assert state["discovery_mode"] == "legacy"
    assert home_file.read_text(encoding="utf-8") == "schema_version: 1\nhome_id: fixture\n"
