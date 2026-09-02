import sys
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from _builder_project_model.live_command import live_command_builder
from _builder_project_model.governor import BuilderGovernor
from _builder_project_model.repo_controller import Candidate


def test_live_command_builder_maps_candidate_to_exact_attempt_runner(tmp_path):
    (tmp_path / "alpha" / ".builder").mkdir(parents=True)
    candidate = Candidate(
        repo_id="alpha",
        repo_root=tmp_path / "alpha",
        work_id="work-a",
        spec_id="spec-a",
        provider="codex-cli",
        lane_name="codex",
        priority=1,
        enqueued_at="2026-01-01T00:00:00Z",
    )
    script = tmp_path / "isanna.py"
    assert live_command_builder(candidate, isanna_script=script) == [
        sys.executable,
        str(script.resolve()),
        "dispatch",
        "--attempt",
        "work-a",
        "--config",
        str((candidate.repo_root / ".builder" / "dispatch.yaml").resolve()),
    ]


def test_governor_launch_records_real_group_identity_and_reaps(tmp_path):
    home = SimpleNamespace(
        root=tmp_path / ".builder-home",
        policy=SimpleNamespace(providers={"codex-cli": SimpleNamespace(max_sessions=1)}),
    )
    governor = BuilderGovernor(home, daemon_instance_id="synthetic-daemon")
    reservation = governor.reserve_slot(
        provider="codex-cli",
        repo_id="alpha",
        queue_root=(tmp_path / "alpha" / ".builder" / "dispatch-queue").resolve(),
        work_id="work-a",
        attempt_id="attempt-a",
        lane="codex",
        project_attribution="standalone:alpha",
        release_name=None,
    )
    proc = governor.launch(
        reservation,
        command=[sys.executable, "-c", "import time; time.sleep(0.15)"],
    )
    record = governor.store.load_session(reservation.slot_id)
    assert record["state"] == "active"
    assert record["pgid"] == proc.pid
    assert record["pgid_leader_start_ticks"] is not None
    assert record["executable"] == sys.executable
    assert str(record["command_digest"]).startswith("sha256:")
    proc.wait(timeout=2)
    action = governor.reap_session(reservation.slot_id)
    assert action.action == "closed"
    assert not governor.store.paths.session_path(reservation.slot_id).exists()
