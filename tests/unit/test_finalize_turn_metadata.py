from __future__ import annotations

from pathlib import Path

from _dispatch_runtime.lane_common import SessionState, Work, finalize_turn
from _dispatch_runtime.phase_runtime import (
    capture_spec_snapshot,
    validate_phase_completion,
)


def _work(tmp_path: Path) -> Work:
    specs_dir = tmp_path / ".builder" / "specs"
    (specs_dir / "demo").mkdir(parents=True, exist_ok=True)
    queue_root = tmp_path / ".builder" / "dispatch-queue"
    return Work(
        work_id="w1",
        spec_id="demo",
        phase="plan",
        project_dir=tmp_path,
        specs_dir=specs_dir,
        runner_task_ref=None,
        capability_class=None,
        queue_root=queue_root,
        log_path=queue_root / "queue" / "attempts" / "a.log",
    )


def test_finalize_turn_carries_token_metadata(tmp_path):
    work = _work(tmp_path)
    pre_snapshot = capture_spec_snapshot(work.specs_dir, work.spec_id, work.phase)
    pre_validation = validate_phase_completion(work.specs_dir, work.spec_id, work.phase)
    exec_result = {
        "status": "interrupted",
        "stdout": "",
        "stderr": "",
        "returncode": 0,
        "session_id": "s",
        "input_tokens": 111,
        "output_tokens": 222,
        "cli_duration_ms": 3456,
        "model": "claude-test",
    }
    result = finalize_turn(
        work, ["claude", "-p", "goal"], exec_result, pre_snapshot, pre_validation, SessionState()
    )
    assert result.metadata["plan_tokens_in"] == 111
    assert result.metadata["plan_tokens_out"] == 222
    assert result.metadata["cli_duration_ms"] == 3456
    assert result.metadata["used_model"] == "claude-test"
    assert result.metadata["total_tokens"] == 333


def test_finalize_turn_always_carries_usage_metadata_keys(tmp_path):
    work = _work(tmp_path)
    pre_snapshot = capture_spec_snapshot(work.specs_dir, work.spec_id, work.phase)
    pre_validation = validate_phase_completion(work.specs_dir, work.spec_id, work.phase)
    result = finalize_turn(
        work, ["stub"], {"status": "interrupted", "stdout": "", "stderr": "", "returncode": 0},
        pre_snapshot, pre_validation, SessionState(),
    )
    assert result.metadata["used_model"] == ""
    assert result.metadata["total_tokens"] == 0
