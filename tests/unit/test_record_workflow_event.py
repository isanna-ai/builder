from __future__ import annotations

import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def run_cmd(*args: str, cwd: Path = ROOT) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, *args], cwd=cwd, text=True, capture_output=True)


def test_help_lists_flag_mode_options() -> None:
    result = run_cmd("scripts/record-workflow-event.py", "--help")
    assert result.returncode == 0
    output = result.stdout + result.stderr
    for flag in ("--phase", "--outcome-category", "--reason-category", "--spec", "--used-model"):
        assert flag in output


def test_invalid_outcome_category_fails() -> None:
    result = run_cmd(
        "scripts/record-workflow-event.py",
        "--phase",
        "3-review",
        "--outcome-category",
        "bogus_value",
        "--spec",
        "test-spec",
    )
    assert result.returncode != 0
    assert "outcome_category" in result.stderr


def test_flag_mode_writes_canonical_event(tmp_path: Path) -> None:
    result = run_cmd(
        "scripts/record-workflow-event.py",
        "--phase",
        "3-review",
        "--outcome-category",
        "completed",
        "--spec",
        "test-spec",
        "--root",
        str(tmp_path),
    )
    assert result.returncode == 0, result.stderr
    written = Path(result.stdout.strip())
    assert written.is_file()
    # Compare RESOLVED paths: on macOS /var is a symlink to /private/var, so the script's
    # resolved output never literally contains the unresolved tmp_path and this assertion
    # failed on the host while passing in the Linux container.
    events = (tmp_path / ".builder" / "telemetry" / "events").resolve()
    assert events in written.resolve().parents
    assert "outcome_category: completed" in written.read_text(encoding="utf-8")


def test_positional_file_mode_ignores_flags(tmp_path: Path) -> None:
    source = tmp_path / "event.yaml"
    source.write_text(
        "\n".join(
            [
                "artifact: workflow-event",
                "event_id: EVT-test-legacy",
                "recorded_at: 2026-05-19T00:00:00Z",
                "command: legacy",
                "mode: lifecycle",
                "used_model: legacy-model",
                "thinking_effort: unknown",
                "capture_source: unavailable",
                "reason_category: phase_progress",
                "intent_summary: legacy event",
                "execution_path: normal_phase",
                "artifacts_read: []",
                "artifacts_written: []",
                "validation_refs: []",
                "outcome_category: completed",
                "next_command: none",
                "redaction:",
                "  sanitized: true",
                "  fields: []",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    result = run_cmd(
        "scripts/record-workflow-event.py",
        str(source),
        "--phase",
        "3-review",
        "--outcome-category",
        "rollback",
        "--root",
        str(tmp_path),
    )
    assert result.returncode == 0, result.stderr
    written = Path(result.stdout.strip())
    text = written.read_text(encoding="utf-8")
    assert "command: legacy" in text
    assert "outcome_category: completed" in text
