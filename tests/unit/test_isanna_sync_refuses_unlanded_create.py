from __future__ import annotations

from pathlib import Path

import isanna as isanna_cli
from tests.unit.sync_evidence_support import write_host_scope


def _scaffold(tmp_path: Path, *, ssot_has_ghost: bool, spec_status: str = "verified") -> None:
    (tmp_path / ".builder" / "specs" / "demo").mkdir(parents=True)
    (tmp_path / ".builder" / "specs" / "demo" / "spec.yaml").write_text(
        f"status: {spec_status}\ncurrent_phase: sync\n", encoding="utf-8"
    )
    (tmp_path / ".builder" / "specs" / "demo" / "ssot-delta.yaml").write_text(
        "capabilities: []\n"
        "behaviors:\n"
        "  - target: ghost\n"
        "    change: create\n"
        "journeys: []\n",
        encoding="utf-8",
    )
    (tmp_path / "tests" / "unit").mkdir(parents=True)
    (tmp_path / "tests" / "unit" / "test_demo.py").write_text(
        "def test_real():\n    pass\n", encoding="utf-8"
    )
    (tmp_path / "Makefile").write_text(
        "gate:\n\tpytest tests/unit/test_demo.py -q\n", encoding="utf-8"
    )
    (tmp_path / "docs").mkdir()
    filler = (
        "  - id: filler\n"
        "    area: x\n"
        "    behavior: y\n"
        "    invariant: z\n"
        "    breaks_when: never\n"
        "    guarding_tests:\n"
        "      - tests/unit/test_demo.py::test_real\n"
    )
    ghost = (
        "  - id: ghost\n"
        "    area: x\n"
        "    behavior: y\n"
        "    invariant: z\n"
        "    breaks_when: never\n"
        "    guarding_tests:\n"
        "      - tests/unit/test_demo.py::test_real\n"
    )
    # Always keep at least one landed, guarded behavior (`filler`) so an absent `ghost` id
    # exercises the unlanded-create gate specifically, not the unrelated drift check (which
    # would otherwise fire on an empty behaviors list).
    behaviors = filler + (ghost if ssot_has_ghost else "")
    (tmp_path / "docs" / "system-behaviors.yaml").write_text(
        "schema: system-behaviors/v1\nbehaviors:\n" + behaviors,
        encoding="utf-8",
    )
    (tmp_path / ".builder" / "sync-adapter.yaml").write_text(
        "artifact: sync-adapter\nmappings: []\n", encoding="utf-8"
    )
    write_host_scope(tmp_path, "demo")


def _run_sync(tmp_path: Path) -> int:
    return isanna_cli.main(
        [
            "sync",
            "--root",
            str(tmp_path),
            "--spec",
            "demo",
            "--scope-evidence",
            str(tmp_path / ".builder" / "specs" / "demo" / "sync-scope.yaml"),
        ]
    )


def test_sync_refuses_unlanded_declared_create(tmp_path: Path):
    """A ssot-delta that declares `behaviors: create(ghost)` but never lands `id: ghost` in the
    curated SSOT must fail-closed, not sync green -- the delta is a contract, not a wish list."""
    _scaffold(tmp_path, ssot_has_ghost=False)
    exit_code = _run_sync(tmp_path)
    assert exit_code == 1
    sync_result_path = tmp_path / ".builder" / "specs" / "demo" / "sync-result.yaml"
    payload = sync_result_path.read_text(encoding="utf-8")
    assert "result: hook_failed" in payload
    assert not (tmp_path / ".builder" / "model" / "system-model.yaml").exists()


def test_sync_accepts_landed_declared_create(tmp_path: Path):
    """The sibling case: once `id: ghost` is actually landed in the curated SSOT, the same
    declared delta syncs clean."""
    _scaffold(tmp_path, ssot_has_ghost=True)
    exit_code = _run_sync(tmp_path)
    assert exit_code == 0
    sync_result_path = tmp_path / ".builder" / "specs" / "demo" / "sync-result.yaml"
    payload = sync_result_path.read_text(encoding="utf-8")
    assert "result: synced" in payload
    assert (tmp_path / ".builder" / "model" / "system-model.yaml").exists()


def test_sync_grandfathers_already_synced_spec(tmp_path: Path):
    """Forward-only: a spec whose spec.yaml already says `status: synced` must NOT be demoted to
    hook_failed on re-sync just because its (already-accepted) declared create was never landed --
    only not-yet-synced specs are held to the unlanded-create gate."""
    _scaffold(tmp_path, ssot_has_ghost=False, spec_status="synced")
    exit_code = _run_sync(tmp_path)
    assert exit_code == 0
    sync_result_path = tmp_path / ".builder" / "specs" / "demo" / "sync-result.yaml"
    payload = sync_result_path.read_text(encoding="utf-8")
    assert "result: synced" in payload
    assert (tmp_path / ".builder" / "model" / "system-model.yaml").exists()
