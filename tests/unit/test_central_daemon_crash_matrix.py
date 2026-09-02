import sys
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from _builder_project_model.governor import BuilderGovernor


def _governor(tmp_path: Path, cap: int = 1) -> BuilderGovernor:
    home = SimpleNamespace(
        root=tmp_path / ".builder-home",
        policy=SimpleNamespace(providers={"codex-cli": SimpleNamespace(max_sessions=cap)}),
    )
    return BuilderGovernor(home, daemon_instance_id="crash-matrix")


def _reserve(governor: BuilderGovernor, name: str = "a", crash_hook=None):
    return governor.reserve_slot(
        provider="codex-cli",
        repo_id="alpha",
        queue_root=(governor.home.root.parent / "alpha" / ".builder" / "dispatch-queue").resolve(),
        work_id=f"work-{name}",
        attempt_id=f"attempt-{name}",
        lane="codex",
        project_attribution="standalone:alpha",
        release_name=None,
        crash_hook=crash_hook,
    )


def test_reservation_crashes_never_oversubscribe_or_release_written_slot(tmp_path):
    for boundary, expected in (("before_reservation_write", 0), ("after_reservation_write", 1)):
        governor = _governor(tmp_path / boundary)

        def crash(phase, _record):
            if phase == boundary:
                raise RuntimeError(boundary)

        try:
            _reserve(governor, crash_hook=crash)
        except RuntimeError as exc:
            assert boundary in str(exc)
        else:
            raise AssertionError(f"expected injected crash at {boundary}")
        assert len(governor.store.consuming_sessions("codex-cli")) == expected
        if expected:
            try:
                _reserve(governor, "b")
            except RuntimeError as exc:
                assert "no available session slots" in str(exc)
            else:
                raise AssertionError("expected the written slot to continue consuming capacity")


def test_launcher_identity_boundaries_leave_reconcilable_owned_state(tmp_path):
    for phase in ("before-pgid-write", "after-pgid-write"):
        governor = _governor(tmp_path / phase)
        reservation = _reserve(governor)
        proc = governor.launch(
            reservation,
            command=[sys.executable, "-c", "pass"],
            crash_phase=phase,
            timeout_seconds=0.3,
        )
        proc.wait(timeout=2)
        record = governor.store.load_session(reservation.slot_id)
        record["owner_pid"] = 99999999
        record["owner_pid_start_ticks"] = 1
        governor.store.write_session(record)
        restarted = _governor(tmp_path / phase)
        actions = restarted.reconcile_startup()
        assert actions[0].action == "closed"
        assert restarted.store.capacity_remaining("codex-cli", max_sessions=1) == 1


def test_identity_mismatch_is_quarantined_and_never_signalled(tmp_path):
    governor = _governor(tmp_path)
    reservation = _reserve(governor)
    proc = governor.launch(
        reservation,
        command=[sys.executable, "-c", "import time; time.sleep(0.2)"],
    )
    record = governor.store.load_session(reservation.slot_id)
    record["owner_pid"] = 99999999
    record["owner_pid_start_ticks"] = 1
    record["pgid_leader_start_ticks"] = int(record["pgid_leader_start_ticks"]) + 1
    governor.store.write_session(record)
    restarted = _governor(tmp_path)
    action = restarted.reconcile_startup()[0]
    assert action.action == "quarantine"
    assert proc.poll() is None
    proc.wait(timeout=2)
    governor.store.close_session(reservation.slot_id)


def test_starting_slot_is_not_prematurely_released_without_launcher_proof(tmp_path):
    governor = _governor(tmp_path)
    reservation = _reserve(governor)
    record = governor.store.load_session(reservation.slot_id)
    record["owner_pid"] = 99999999
    record["owner_pid_start_ticks"] = 1
    governor.store.write_session(record)
    action = _governor(tmp_path).reconcile_startup()[0]
    assert action.action == "quarantine"
    assert governor.store.paths.session_path(reservation.slot_id).exists()
