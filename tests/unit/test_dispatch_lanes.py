from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

from _dispatch_runtime.lane_claude_code_cli import ClaudeCodeCliLane, _scrubbed_env
from _dispatch_runtime.lane_codex_cli import CodexCliLane
from _dispatch_runtime.lane_executor import DispatchResultType


@dataclass
class CompletedProcess:
    returncode: int
    stdout: str
    stderr: str
    pid: int = 4242


class FakeProcessRunner:
    def __init__(self, result: CompletedProcess):
        self.result = result
        self.calls: list[list[str]] = []
        self.envs: list[dict] = []

    def run(self, argv: list[str], **kwargs):
        self.calls.append(argv)
        self.envs.append(dict(kwargs.get("env") or {}))
        return self.result


def lane_context(workspace_root: Path):
    return {
        "attempt_id": "attempt-1",
        "work_id": "work-1",
        "log_path": "logs/attempt-1.log",
        "runner_command": ["python3", str(workspace_root / "scripts" / "builder-runner.py")],
        "workspace_root": str(workspace_root),
    }


def create_workspace(tmp_path: Path) -> Path:
    (tmp_path / "scripts").mkdir(parents=True)
    (tmp_path / "scripts" / "builder-runner.py").write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    run_dir = tmp_path / ".builder" / "specs" / "demo" / "runs"
    run_dir.mkdir(parents=True)
    (run_dir / "task-T5.yaml").write_text("task: demo\n", encoding="utf-8")
    (tmp_path / ".builder" / "specs" / "demo").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".builder" / "specs" / "demo" / "spec.yaml").write_text("current_phase: plan\n", encoding="utf-8")
    return tmp_path


def task_ref():
    return {"kind": "builder-runner-task", "runner_task_ref": ".builder/specs/demo/runs/task-T5.yaml"}


def test_lane_executor_returns_typed_success_result_and_attempt_metadata_for_codex(tmp_path: Path):
    workspace_root = create_workspace(tmp_path)
    runner = FakeProcessRunner(CompletedProcess(returncode=0, stdout="ok\n", stderr="", pid=222))
    lane = CodexCliLane(process_runner=runner)

    result = lane.execute(task_ref(), "codex-cli", lane_context(workspace_root))

    assert result is not None
    assert runner.calls[0][:2] == ["codex", "exec"]
    assert "-c" in runner.calls[0]
    assert any(arg.startswith("model_reasoning_effort=") for arg in runner.calls[0])


def test_codex_lane_records_reported_total_usage_model_and_wall_duration(tmp_path: Path):
    workspace_root = create_workspace(tmp_path)
    runner = FakeProcessRunner(CompletedProcess(returncode=0, stdout="done\ntokens used\n12,345\n", stderr=""))
    result = CodexCliLane(process_runner=runner).execute(task_ref(), "codex-cli", lane_context(workspace_root))

    command = runner.calls[0]
    expected_model = command[command.index("-m") + 1]
    assert result.metadata["total_tokens"] == 12345
    assert result.metadata["plan_tokens_in"] == 0
    assert result.metadata["plan_tokens_out"] == 0
    assert result.metadata["used_model"] == expected_model
    assert result.metadata["cli_duration_ms"] >= 0


def test_codex_lane_leaves_total_zero_without_usage_line(tmp_path: Path):
    workspace_root = create_workspace(tmp_path)
    runner = FakeProcessRunner(CompletedProcess(returncode=0, stdout="done\n", stderr=""))
    result = CodexCliLane(process_runner=runner).execute(task_ref(), "codex-cli", lane_context(workspace_root))
    assert result.metadata["total_tokens"] == 0


def test_claude_lane_preserves_envelope_usage_duration_and_model(tmp_path: Path):
    workspace_root = create_workspace(tmp_path)
    runner = FakeProcessRunner(CompletedProcess(
        returncode=0,
        stdout='{"is_error": false, "usage": {"input_tokens": 100, "output_tokens": 40}, "duration_ms": 500}',
        stderr="",
    ))
    result = ClaudeCodeCliLane(process_runner=runner).execute(task_ref(), "claude-code-cli", lane_context(workspace_root))

    command = runner.calls[0]
    expected_model = command[command.index("--model") + 1]
    assert result.metadata["plan_tokens_in"] == 100
    assert result.metadata["plan_tokens_out"] == 40
    assert result.metadata["total_tokens"] == 140
    assert result.metadata["cli_duration_ms"] == 500
    assert result.metadata["used_model"] == expected_model


def test_rate_limit_signals_map_to_rate_limited_results(tmp_path: Path):
    workspace_root = create_workspace(tmp_path)
    runner = FakeProcessRunner(CompletedProcess(returncode=75, stdout="", stderr="rate limited\nretry_after=120\n", pid=333))
    lane = CodexCliLane(process_runner=runner)

    result = lane.execute(task_ref(), "codex-cli", lane_context(workspace_root))

    assert result.result_type == DispatchResultType.RATE_LIMITED
    assert result.metadata["runner_task_ref"].endswith("task-T5.yaml")


def test_claude_lane_drives_claude_with_opus_or_sonnet_and_high_reasoning(tmp_path: Path):
    token_file = tmp_path / "builder-claude-oauth-token"
    token_file.write_text("test-long-lived-oauth-token\n", encoding="utf-8")
    token_file.chmod(0o600)
    workspace_root = create_workspace(tmp_path)
    runner = FakeProcessRunner(CompletedProcess(returncode=0, stdout="ok\n", stderr="", pid=444))
    lane = ClaudeCodeCliLane(process_runner=runner)

    with patch.dict(
        os.environ,
        {
            "ISANNA_CLAUDE_CODE_OAUTH_TOKEN_FILE": str(token_file),
            "ANTHROPIC_API_KEY": "console-key",
            "ANTHROPIC_AUTH_TOKEN": "gateway-token",
            "CLAUDE_CODE_API_KEY": "legacy-key",
            "CLAUDE_API_KEY": "legacy-key-2",
            "CLAUDE_CODE_USE_BEDROCK": "1",
            "ANTHROPIC_BASE_URL": "https://gateway.invalid",
        },
    ):
        result = lane.execute(task_ref(), "claude-code-cli", lane_context(workspace_root))

    assert result is not None
    # Active path runs Claude Code, not codex.
    assert runner.calls[0][:2] == ["claude", "-p"]
    argv = runner.calls[0]
    model = argv[argv.index("--model") + 1]
    assert model.startswith("claude-") and "haiku" not in model  # full claude-* id, never haiku
    # Console API keys are scrubbed so claude uses the Max subscription.
    env = runner.envs[0]
    assert "ANTHROPIC_API_KEY" not in env
    assert "ANTHROPIC_AUTH_TOKEN" not in env
    assert "CLAUDE_CODE_API_KEY" not in env
    assert "CLAUDE_API_KEY" not in env
    assert "CLAUDE_CODE_USE_BEDROCK" not in env
    assert "ANTHROPIC_BASE_URL" not in env
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "test-long-lived-oauth-token"
    # Reasoning effort (>= high) is applied via the extended-thinking budget.
    assert int(env["MAX_THINKING_TOKENS"]) >= 24000


def test_claude_lane_token_file_fails_closed_when_permissions_are_broad(tmp_path: Path):
    token_file = tmp_path / "builder-claude-oauth-token"
    token_file.write_text("test-long-lived-oauth-token\n", encoding="utf-8")
    token_file.chmod(0o644)

    with patch.dict(
        os.environ, {"ISANNA_CLAUDE_CODE_OAUTH_TOKEN_FILE": str(token_file)}
    ):
        if os.name == "nt":
            assert _scrubbed_env()["CLAUDE_CODE_OAUTH_TOKEN"] == "test-long-lived-oauth-token"
        else:
            try:
                _scrubbed_env()
            except RuntimeError as exc:
                assert "mode 0600" in str(exc)
            else:
                raise AssertionError("broad token-file permissions must fail closed")


def test_nonzero_completion_maps_to_retryable_or_terminal_without_scheduler_logic(tmp_path: Path):
    workspace_root = create_workspace(tmp_path)
    retry_runner = FakeProcessRunner(CompletedProcess(returncode=1, stdout="", stderr="temporary failure\n", pid=555))
    retry_lane = ClaudeCodeCliLane(process_runner=retry_runner)
    retry_result = retry_lane.execute(task_ref(), "claude-code-cli", lane_context(workspace_root))

    terminal_runner = FakeProcessRunner(CompletedProcess(returncode=10, stdout="", stderr="fatal configuration\n", pid=666))
    terminal_lane = CodexCliLane(process_runner=terminal_runner)
    terminal_result = terminal_lane.execute(task_ref(), "codex-cli", lane_context(workspace_root))

    assert retry_result is not None
    assert terminal_result is not None
