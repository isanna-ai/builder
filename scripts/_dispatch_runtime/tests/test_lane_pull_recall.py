"""Item 4 — pull-mode recall in the Claude Code CLI lane.

Covers two seams the lane owns:

  * execute(): in pull mode (MEMORY_RECALL_MODE=="pull" + hivemind endpoint, plan
    phase) the command gains a transient --mcp-config and an --allowedTools entry
    granting ONLY mcp__hive__hive_search_memories; the recall stats parsed from the
    agent's tool-call records are threaded into last_plan_recall_stats(). The
    DEFAULT push path (flag unset) adds NEITHER flag — byte-identical to today.
  * _classify(): parses --output-format json tool-call records into
    recall_calls/recall_hits/decisions_reused, and emits zeros (never raises) when
    the records are absent or unparseable.

Vendored-shim rules: bare test_* functions, plain asserts, tmp_path the only
fixture, env via the os.environ pop/restore idiom, mock.patch as a context manager.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from _dispatch_runtime import lane_claude_code_cli as lane
from _dispatch_runtime.lane_claude_code_cli import ClaudeCodeCliLane, _classify
from _dispatch_runtime.phase_runtime import last_plan_recall_stats

_RECALL_ENV_KEYS = ("MEMORY_RECALL_MODE", "HIVEMIND_MCP_URL", "HIVEMIND_API_KEY")


def _set_env(values: dict[str, str | None]) -> dict[str, str | None]:
    saved: dict[str, str | None] = {k: os.environ.get(k) for k in _RECALL_ENV_KEYS}
    for key, val in values.items():
        if val is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = val
    return saved


def _restore_env(saved: dict[str, str | None]) -> None:
    for key, val in saved.items():
        if val is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = val


def _make_plan_spec(tmp_path: Path) -> None:
    spec_dir = tmp_path / ".builder" / "specs" / "demo"
    spec_dir.mkdir(parents=True, exist_ok=True)
    (spec_dir / "spec.yaml").write_text(
        'name: "demo"\nstatus: "planned"\ncurrent_phase: "plan"\nsummary: "pull intent"\n',
        encoding="utf-8",
    )


class _CapturingRunner:
    """Stands in for the lane's process_runner: records the command and returns a
    canned (returncode, stdout, stderr, timed_out) tuple."""

    def __init__(self, stdout: str):
        self.stdout = stdout
        self.commands: list[list[str]] = []

    def run(self, command, *, cwd, env, timeout):
        self.commands.append(list(command))
        return (0, self.stdout, "", False)


def _json_with_recall(call_count: int, rows_per_call: int) -> str:
    """A plausible --output-format json result carrying `call_count`
    hive_search_memories tool_use blocks, each answered by a tool_result whose
    text encodes `rows_per_call` memory rows."""
    content: list[dict] = []
    for i in range(call_count):
        tuid = f"toolu_{i}"
        content.append({
            "type": "tool_use",
            "id": tuid,
            "name": "mcp__hive__hive_search_memories",
            "input": {"query": "pull intent"},
        })
        rows = [{"content": f"m{j}", "type": "decision"} for j in range(rows_per_call)]
        content.append({
            "type": "tool_result",
            "tool_use_id": tuid,
            "content": [{"type": "text", "text": json.dumps({"results": rows})}],
        })
    return json.dumps({
        "session_id": "sess-1",
        "is_error": False,
        "result": "ok",
        "duration_ms": 1234,
        "usage": {"input_tokens": 10, "output_tokens": 5},
        "messages": [{"role": "assistant", "message": {"content": content}}],
    })


def _stream_json_lines() -> str:
    """A synthetic --output-format stream-json stdout: one JSON object PER LINE in
    order, mirroring the real live shape — a couple of system lines, an assistant
    tool_use for mcp__hive__hive_search_memories (id toolu_X), a user tool_result
    answering toolu_X with two rows, and a final type:result envelope (== today's
    json envelope on the last line). A ToolSearch tool_use is interleaved and must
    NOT be counted as a recall."""
    events = [
        {"type": "system", "subtype": "init", "session_id": "s1"},
        {"type": "system", "subtype": "meta"},
        {"type": "rate_limit_event", "status": "ok"},
        {"type": "assistant", "message": {"content": [
            {"type": "thinking", "thinking": "let me look"}]}},
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "toolu_search", "name": "ToolSearch",
             "input": {"query": "hive"}}]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "toolu_search",
             "content": [{"type": "text", "text": "found mcp__hive__hive_search_memories"}]}]}},
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "toolu_X",
             "name": "mcp__hive__hive_search_memories",
             "input": {"query": "pull intent"}}]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "toolu_X",
             "content": [{"type": "text",
                          "text": json.dumps({"results": [{"id": "m1"}, {"id": "m2"}]})}]}]}},
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "final answer"}]}},
        {"type": "result", "is_error": False, "result": "ok", "session_id": "s1",
         "duration_ms": 10, "usage": {"input_tokens": 5, "output_tokens": 7}},
    ]
    return "\n".join(json.dumps(e) for e in events)


# --- execute(): pull adds mcp-config + allowlist ----------------------------
def test_pull_mode_adds_mcp_config_and_allowlist(tmp_path):
    _make_plan_spec(tmp_path)
    runner = _CapturingRunner(_json_with_recall(call_count=2, rows_per_call=3))
    saved = _set_env({
        "MEMORY_RECALL_MODE": "pull",
        "HIVEMIND_MCP_URL": "http://memory.example.invalid:8000",
        "HIVEMIND_API_KEY": "test-key",
    })
    try:
        lane_obj = ClaudeCodeCliLane(process_runner=runner)
        lane_obj.execute(
            {"spec_id": "demo", "phase": "plan"},
            "claude-code-cli",
            {"workspace_root": str(tmp_path)},
        )
    finally:
        _restore_env(saved)

    assert len(runner.commands) == 1
    cmd = runner.commands[0]
    assert "--mcp-config" in cmd
    assert "--allowedTools" in cmd
    # The allowlist value is EXACTLY the recall tool (recall-only, no write/delete).
    assert cmd[cmd.index("--allowedTools") + 1] == "mcp__hive__hive_search_memories"
    # The --mcp-config argument is a path to a JSON file that wires the hive HTTP
    # server with a Bearer header from HIVEMIND_API_KEY (file is cleaned up after).
    cfg_arg = cmd[cmd.index("--mcp-config") + 1]
    assert not os.path.exists(cfg_arg)  # temp config unlinked in the finally
    # Recovered recall stats threaded into the plan stash: 2 calls, 2 hits, 6 rows.
    stats = last_plan_recall_stats()
    assert stats["recall_calls"] == 2
    assert stats["recall_hits"] == 2
    assert stats["decisions_reused"] == 6


def test_pull_recall_active_true_for_hybrid_mode():
    # The lane gate activates for hybrid too (hybrid = injected push block + pull
    # tool); push stays inactive (no pull tool granted).
    from _dispatch_runtime.lane_claude_code_cli import _pull_recall_active

    saved = _set_env({
        "MEMORY_RECALL_MODE": "hybrid",
        "HIVEMIND_MCP_URL": "http://memory.example.invalid:8000",
        "HIVEMIND_API_KEY": "k",
    })
    try:
        assert _pull_recall_active("plan") is True
    finally:
        _restore_env(saved)

    saved = _set_env({
        "MEMORY_RECALL_MODE": "push",
        "HIVEMIND_MCP_URL": "http://memory.example.invalid:8000",
        "HIVEMIND_API_KEY": "k",
    })
    try:
        assert _pull_recall_active("plan") is False
    finally:
        _restore_env(saved)


def test_push_default_does_not_add_pull_flags(tmp_path):
    # MEMORY_RECALL_MODE unset => push: NO --mcp-config and NO --allowedTools.
    _make_plan_spec(tmp_path)
    runner = _CapturingRunner(json.dumps({
        "session_id": "s", "is_error": False, "result": "ok", "duration_ms": 1,
        "usage": {"input_tokens": 0, "output_tokens": 0},
    }))
    saved = _set_env({
        "MEMORY_RECALL_MODE": None,
        "HIVEMIND_MCP_URL": "http://memory.example.invalid:8000",
        "HIVEMIND_API_KEY": "test-key",
    })
    try:
        ClaudeCodeCliLane(process_runner=runner).execute(
            {"spec_id": "demo", "phase": "plan"},
            "claude-code-cli",
            {"workspace_root": str(tmp_path)},
        )
    finally:
        _restore_env(saved)

    cmd = runner.commands[0]
    assert "--mcp-config" not in cmd
    assert "--allowedTools" not in cmd


def test_pull_flags_not_added_on_non_plan_phase(tmp_path):
    # Pull recall is plan-only; an implement phase must not get the MCP flags.
    spec_dir = tmp_path / ".builder" / "specs" / "demo"
    spec_dir.mkdir(parents=True, exist_ok=True)
    (spec_dir / "spec.yaml").write_text(
        'name: "demo"\nstatus: "implementing"\ncurrent_phase: "implement"\nsummary: "x"\n',
        encoding="utf-8",
    )
    runner = _CapturingRunner(json.dumps({
        "session_id": "s", "is_error": False, "result": "ok", "duration_ms": 1,
        "usage": {},
    }))
    saved = _set_env({
        "MEMORY_RECALL_MODE": "pull",
        "HIVEMIND_MCP_URL": "http://memory.example.invalid:8000",
        "HIVEMIND_API_KEY": "test-key",
    })
    try:
        ClaudeCodeCliLane(process_runner=runner).execute(
            {"spec_id": "demo", "phase": "implement"},
            "claude-code-cli",
            {"workspace_root": str(tmp_path)},
        )
    finally:
        _restore_env(saved)

    cmd = runner.commands[0]
    assert "--mcp-config" not in cmd
    assert "--allowedTools" not in cmd


def test_pull_flags_not_added_when_endpoint_missing(tmp_path):
    # MEMORY_RECALL_MODE=pull but no hivemind endpoint => no flags (forced off).
    _make_plan_spec(tmp_path)
    runner = _CapturingRunner(json.dumps({
        "session_id": "s", "is_error": False, "result": "ok", "duration_ms": 1, "usage": {},
    }))
    saved = _set_env({
        "MEMORY_RECALL_MODE": "pull",
        "HIVEMIND_MCP_URL": None,
        "HIVEMIND_API_KEY": None,
    })
    try:
        ClaudeCodeCliLane(process_runner=runner).execute(
            {"spec_id": "demo", "phase": "plan"},
            "claude-code-cli",
            {"workspace_root": str(tmp_path)},
        )
    finally:
        _restore_env(saved)

    cmd = runner.commands[0]
    assert "--mcp-config" not in cmd
    assert "--allowedTools" not in cmd


# --- _classify(): parse tool usage; zeros when absent -----------------------
def test_classify_parses_recall_tool_usage():
    stdout = _json_with_recall(call_count=3, rows_per_call=2)
    result = _classify(0, stdout, "")
    assert result["recall_calls"] == 3
    assert result["recall_hits"] == 3
    assert result["decisions_reused"] == 6


def test_classify_zero_recall_when_no_tool_records():
    # A clean json result with no tool_use blocks => zero recall stats (never raise).
    stdout = json.dumps({
        "session_id": "s", "is_error": False, "result": "done", "duration_ms": 2,
        "usage": {"input_tokens": 1, "output_tokens": 1},
    })
    result = _classify(0, stdout, "")
    assert result["recall_calls"] == 0
    assert result["recall_hits"] == 0
    assert result["decisions_reused"] == 0


def test_classify_zero_recall_on_unparseable_output():
    # Non-JSON text output (the text-pattern fallback path) emits zeros, never raises.
    result = _classify(0, "not json at all", "")
    assert result["recall_calls"] == 0
    assert result["recall_hits"] == 0
    assert result["decisions_reused"] == 0


def test_classify_tool_call_with_empty_results_is_call_not_hit():
    # A recall call returning zero rows counts as a call but NOT a hit.
    content = [
        {"type": "tool_use", "id": "t0", "name": "mcp__hive__hive_search_memories",
         "input": {"query": "q"}},
        {"type": "tool_result", "tool_use_id": "t0",
         "content": [{"type": "text", "text": json.dumps({"results": []})}]},
    ]
    stdout = json.dumps({
        "session_id": "s", "is_error": False, "result": "ok", "duration_ms": 1,
        "usage": {}, "messages": [{"message": {"content": content}}],
    })
    result = _classify(0, stdout, "")
    assert result["recall_calls"] == 1
    assert result["recall_hits"] == 0
    assert result["decisions_reused"] == 0


def test_classify_ignores_non_hive_tool_calls():
    # Tool calls to other tools (e.g. Write) must not be counted as recall.
    content = [
        {"type": "tool_use", "id": "w0", "name": "Write", "input": {}},
        {"type": "tool_result", "tool_use_id": "w0",
         "content": [{"type": "text", "text": "ok"}]},
    ]
    stdout = json.dumps({
        "session_id": "s", "is_error": False, "result": "ok", "duration_ms": 1,
        "usage": {}, "messages": [{"message": {"content": content}}],
    })
    result = _classify(0, stdout, "")
    assert result["recall_calls"] == 0


# --- _classify(): stream-json (pull) carries the per-message records ---------
def test_classify_parses_stream_json_recall_records():
    # A multi-line stream-json stdout: the result envelope (last line) drives
    # status/session_id/usage/duration, and the streamed tool_use/tool_result
    # records yield the recall counts.
    stdout = _stream_json_lines()
    result = _classify(0, stdout, "")
    # status interpreted from the (non-error) result envelope
    assert result["status"] == "interrupted"
    assert result["session_id"] == "s1"
    assert result["input_tokens"] == 5
    assert result["output_tokens"] == 7
    assert result["cli_duration_ms"] == 10
    # exactly one hive recall call (the ToolSearch tool_use is NOT counted), one
    # hit, two rows reused.
    assert result["recall_calls"] == 1
    assert result["recall_hits"] == 1
    assert result["decisions_reused"] == 2


def test_classify_stream_json_does_not_count_toolsearch():
    # Even without any hive call, a stream that only used ToolSearch yields zero
    # recall calls.
    events = [
        {"type": "system", "subtype": "init"},
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "toolu_s", "name": "ToolSearch", "input": {}}]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "toolu_s",
             "content": [{"type": "text", "text": "ok"}]}]}},
        {"type": "result", "is_error": False, "result": "ok", "session_id": "s2",
         "duration_ms": 3, "usage": {"input_tokens": 1, "output_tokens": 1}},
    ]
    stdout = "\n".join(json.dumps(e) for e in events)
    result = _classify(0, stdout, "")
    assert result["recall_calls"] == 0
    assert result["recall_hits"] == 0
    assert result["decisions_reused"] == 0


def test_classify_single_line_json_envelope_still_zeros_and_reads_usage():
    # A single-line --output-format json envelope (push) reads usage but the
    # per-message records are absent => recall stats stay zero.
    stdout = json.dumps({
        "session_id": "p1", "is_error": False, "result": "ok", "duration_ms": 9,
        "usage": {"input_tokens": 4, "output_tokens": 8},
    })
    result = _classify(0, stdout, "")
    assert result["input_tokens"] == 4
    assert result["output_tokens"] == 8
    assert result["recall_calls"] == 0
    assert result["recall_hits"] == 0
    assert result["decisions_reused"] == 0


# --- execute(): output-format matches the recall mode ------------------------
def test_pull_mode_command_uses_stream_json():
    # Pull plan turn must run stream-json --verbose AND carry the mcp/allowlist flags.
    import tempfile

    tmp = Path(tempfile.mkdtemp())
    _make_plan_spec(tmp)
    runner = _CapturingRunner(_stream_json_lines())
    saved = _set_env({
        "MEMORY_RECALL_MODE": "pull",
        "HIVEMIND_MCP_URL": "http://memory.example.invalid:8000",
        "HIVEMIND_API_KEY": "test-key",
    })
    try:
        ClaudeCodeCliLane(process_runner=runner).execute(
            {"spec_id": "demo", "phase": "plan"},
            "claude-code-cli",
            {"workspace_root": str(tmp)},
        )
    finally:
        _restore_env(saved)

    cmd = runner.commands[0]
    assert "--output-format" in cmd
    assert cmd[cmd.index("--output-format") + 1] == "stream-json"
    assert "--verbose" in cmd
    assert "--mcp-config" in cmd
    assert "--allowedTools" in cmd
    # json must NOT be the output format on a pull turn.
    assert "json" not in [cmd[i + 1] for i, a in enumerate(cmd[:-1]) if a == "--output-format"]


def test_push_mode_command_uses_plain_json():
    # Push (default) plan turn keeps --output-format json and NONE of the pull flags.
    import tempfile

    tmp = Path(tempfile.mkdtemp())
    _make_plan_spec(tmp)
    runner = _CapturingRunner(json.dumps({
        "session_id": "s", "is_error": False, "result": "ok", "duration_ms": 1,
        "usage": {"input_tokens": 0, "output_tokens": 0},
    }))
    saved = _set_env({
        "MEMORY_RECALL_MODE": None,
        "HIVEMIND_MCP_URL": "http://memory.example.invalid:8000",
        "HIVEMIND_API_KEY": "test-key",
    })
    try:
        ClaudeCodeCliLane(process_runner=runner).execute(
            {"spec_id": "demo", "phase": "plan"},
            "claude-code-cli",
            {"workspace_root": str(tmp)},
        )
    finally:
        _restore_env(saved)

    cmd = runner.commands[0]
    assert "--output-format" in cmd
    assert cmd[cmd.index("--output-format") + 1] == "json"
    assert "--verbose" not in cmd
    assert "--mcp-config" not in cmd
    assert "--allowedTools" not in cmd


def test_write_pull_mcp_config_shape(tmp_path):
    # The generated config wires an HTTP hive server with the Bearer auth header.
    saved = _set_env({
        "MEMORY_RECALL_MODE": "pull",
        "HIVEMIND_MCP_URL": "http://memory.example.invalid:8000",
        "HIVEMIND_API_KEY": "secret-key",
    })
    try:
        path = lane._write_pull_mcp_config()
        assert path is not None
        try:
            cfg = json.loads(Path(path).read_text(encoding="utf-8"))
        finally:
            os.unlink(path)
    finally:
        _restore_env(saved)

    server = cfg["mcpServers"]["hive"]
    assert server["type"] == "http"
    assert server["url"].endswith("/mcp")
    assert server["headers"]["Authorization"] == "Bearer secret-key"


def test_write_pull_mcp_config_none_without_endpoint():
    saved = _set_env({"HIVEMIND_MCP_URL": None, "HIVEMIND_API_KEY": None})
    try:
        assert lane._write_pull_mcp_config() is None
    finally:
        _restore_env(saved)
