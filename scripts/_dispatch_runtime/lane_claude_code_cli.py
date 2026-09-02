"""Claude Code CLI lane adapter (the active implementation path).

Drives one Builder phase via headless `claude -p`. Key correctness points
(learned firsthand):
  - `claude -p <goal>` is the valid headless form (NOT `claude code ...`).
  - `--permission-mode bypassPermissions` lets it write files unattended; this
    refuses under root, so the daemon must run as a non-root user.
  - Provider keys/routes are SCRUBBED so claude uses the Max subscription. A
    Builder-only one-year OAuth token file is supported for headless lanes.
  - reasoning effort is applied via MAX_THINKING_TOKENS (claude has no per-call
    reasoning-effort flag); opus/sonnet both run at >= high.
  - exit code is never a completion signal; completion is artifact-gated.
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
import time
from pathlib import Path
from typing import Any

from _dispatch_runtime.lane_common import (
    _control_root,
    _git_head,
    _git_source_paths,
    finalize_turn,
    load_session,
    maybe_env_up,
    resolve_work,
    run_cli_turn,
)
from _dispatch_runtime.lane_executor import DispatchResult, DispatchResultType, resolve_effort
from _dispatch_runtime.phase_routing import capability_for_phase, claude_model_for
from _dispatch_runtime.model_registry import resolve_model, resolve_model_fallbacks
from _dispatch_runtime.phase_runtime import (
    RATE_LIMIT_PATTERN,
    REAL_ERROR_PATTERN,
    SESSION_EXPIRED_PATTERN,
    SESSION_LIMIT_PATTERN,
    MalformedControlFile,
    build_phase_goal,
    capture_spec_snapshot,
    validate_phase_completion,
)

DEFAULT_TIMEOUT = 1800

# Effort level -> extended-thinking token budget passed to Claude Code via the
# MAX_THINKING_TOKENS env var. The active Claude lane only ever resolves `high`
# or `xhigh` (see model_registry.CAPABILITY_EFFORT_MAP); the lower rungs are kept
# for completeness so an explicit override never falls through to "no thinking".
_EFFORT_THINKING_TOKENS: dict[str, int] = {
    "low": 4000,
    "medium": 12000,
    "high": 24000,
    "xhigh": 48000,
    "max": 64000,
}

_SUBSCRIPTION_TOKEN_FILE_ENV = "ISANNA_CLAUDE_CODE_OAUTH_TOKEN_FILE"
_NON_SUBSCRIPTION_AUTH_VARS = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_API_KEY",
    "CLAUDE_API_KEY",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_CUSTOM_HEADERS",
)


# The recall-only hive MCP tool the agent is allowed to call in pull mode. Recall
# only — write/delete tools are intentionally NOT in the allowlist.
_HIVE_RECALL_TOOL = "mcp__hive__hive_search_memories"


def _pull_recall_active(phase: str) -> bool:
    """Pull-mode recall is active only on the plan phase, only when
    MEMORY_RECALL_MODE is "pull" or "hybrid" (hybrid = a small injected block PLUS
    on-demand pull), and only when a hivemind endpoint is configured. Any other state
    reproduces today's behavior (no MCP flags added)."""
    from _dispatch_runtime.phase_runtime import normalize_phase

    if normalize_phase(phase) != "plan":
        return False
    if (os.environ.get("MEMORY_RECALL_MODE") or "push").strip().lower() not in ("pull", "hybrid"):
        return False
    return bool(os.environ.get("HIVEMIND_MCP_URL") and os.environ.get("HIVEMIND_API_KEY"))


def _write_pull_mcp_config() -> str | None:
    """Generate a transient --mcp-config file granting the agent an HTTP hive MCP
    server (recall-only via the allowlist) built from HIVEMIND_MCP_URL /
    HIVEMIND_API_KEY. Returns the temp file path, or None when the endpoint is not
    configured (caller then skips the pull flags). Best-effort: never raises."""
    url = os.environ.get("HIVEMIND_MCP_URL")
    api_key = os.environ.get("HIVEMIND_API_KEY")
    if not url or not api_key:
        return None
    base = url.rstrip("/")
    if not base.endswith("/mcp"):
        base = base + "/mcp"
    config = {
        "mcpServers": {
            "hive": {
                "type": "http",
                "url": base,
                "headers": {"Authorization": f"Bearer {api_key}"},
            }
        }
    }
    try:
        fd, path = tempfile.mkstemp(prefix="builder-hive-mcp-", suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(config, fh)
        return path
    except OSError:
        return None


def _extract_recall_stats(parsed: Any) -> dict[str, int]:
    """Recover pull-mode recall stats from the agent's hive_search_memories tool
    calls. Defensive: any unexpected shape yields zeros (the default-push and
    parse-failure contract — never raise).

      recall_calls    = number of mcp__hive__hive_search_memories tool_use blocks
      recall_hits     = tool calls whose tool_result carried a non-empty result set
      decisions_reused= total memory rows returned across those tool_result blocks

    Pull turns now run `--output-format stream-json --verbose`, so the per-message
    tool_use/tool_result records ARE present: _classify hands this extractor the
    full ordered event list (each line a {type:..., message:{content:[...]}} object,
    consumed by _iter_message_records) and the counts come out non-zero whenever the
    agent called the tool. json-format turns (push) emit only a summary envelope with
    no per-message records, so this still returns zeros there by design — the plan
    token/wall measurement is unaffected (it comes from the envelope `usage`).
    """
    calls = 0
    hits = 0
    reused = 0
    try:
        records = _iter_message_records(parsed)
        # Map tool_use id -> True once we see a hive_search_memories invocation, so
        # the matching tool_result can be attributed back to it.
        recall_ids: set[str] = set()
        for block in records:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "tool_use" and _is_hive_recall_name(block.get("name")):
                calls += 1
                bid = block.get("id")
                if isinstance(bid, str):
                    recall_ids.add(bid)
            elif btype == "tool_result":
                tuid = block.get("tool_use_id")
                if not (isinstance(tuid, str) and tuid in recall_ids):
                    continue
                n = _count_result_rows(block.get("content"))
                if n > 0:
                    hits += 1
                    reused += n
    except Exception:  # noqa: BLE001 - stats recovery must never raise
        return {"recall_calls": 0, "recall_hits": 0, "decisions_reused": 0}
    return {"recall_calls": calls, "recall_hits": hits, "decisions_reused": reused}


def _is_hive_recall_name(name: Any) -> bool:
    if not isinstance(name, str):
        return False
    # Accept the fully-qualified mcp name and any provider-prefixed variant.
    return name == _HIVE_RECALL_TOOL or name.endswith("bia_search_memories")


def _iter_message_records(parsed: Any):
    """Yield content blocks (dicts) from whatever message container the json result
    carries. Tolerates several plausible shapes: a top-level `messages` list, a
    `content` list, or an assistant-message envelope nesting `message.content`."""
    blocks: list[Any] = []

    def _drain(container: Any) -> None:
        if isinstance(container, list):
            for item in container:
                if isinstance(item, dict):
                    inner = item.get("message")
                    if isinstance(inner, dict) and isinstance(inner.get("content"), list):
                        blocks.extend(inner["content"])
                    elif isinstance(item.get("content"), list):
                        blocks.extend(item["content"])
                    else:
                        blocks.append(item)

    if isinstance(parsed, dict):
        _drain(parsed.get("messages"))
        if isinstance(parsed.get("content"), list):
            blocks.extend(parsed["content"])
    elif isinstance(parsed, list):
        _drain(parsed)
    return blocks


def _count_result_rows(content: Any) -> int:
    """Count memory rows in a tool_result content payload. Handles the common MCP
    shapes: a list of {type:text, text:<json>} parts whose text encodes a
    {results:[...]} object, or a direct {results:[...]} / list payload."""
    def _rows_from_obj(obj: Any) -> int:
        if isinstance(obj, dict):
            res = obj.get("results")
            if isinstance(res, list):
                return len(res)
            data = obj.get("json")
            if isinstance(data, dict) and isinstance(data.get("results"), list):
                return len(data["results"])
        if isinstance(obj, list):
            return len(obj)
        return 0

    if isinstance(content, str):
        try:
            return _rows_from_obj(json.loads(content))
        except (json.JSONDecodeError, ValueError):
            return 0
    if isinstance(content, dict):
        return _rows_from_obj(content)
    if isinstance(content, list):
        total = 0
        for part in content:
            if isinstance(part, dict):
                if "json" in part and isinstance(part["json"], dict):
                    total += _rows_from_obj(part["json"])
                    continue
                txt = part.get("text")
                if isinstance(txt, str):
                    try:
                        total += _rows_from_obj(json.loads(txt))
                    except (json.JSONDecodeError, ValueError):
                        continue
            elif isinstance(part, str):
                try:
                    total += _rows_from_obj(json.loads(part))
                except (json.JSONDecodeError, ValueError):
                    continue
        return total
    return 0


def _subscription_token_from_file() -> str | None:
    """Read the optional Builder-only long-lived OAuth token.

    The dispatcher receives only a path. The token itself is injected into the
    Claude child process and never added to the ambient, machine-wide
    environment. When a path is configured, fail closed on a missing, empty, or
    group/world-readable file instead of silently falling back to rotating
    interactive credentials.
    """
    configured = (os.environ.get(_SUBSCRIPTION_TOKEN_FILE_ENV) or "").strip()
    if not configured:
        return None

    path = Path(configured).expanduser()
    try:
        file_stat = path.stat()
    except OSError as exc:
        raise RuntimeError(
            f"{_SUBSCRIPTION_TOKEN_FILE_ENV} is configured but unreadable: {path}"
        ) from exc
    if not stat.S_ISREG(file_stat.st_mode):
        raise RuntimeError(f"{_SUBSCRIPTION_TOKEN_FILE_ENV} must point to a regular file: {path}")
    if os.name != "nt" and stat.S_IMODE(file_stat.st_mode) & 0o077:
        raise RuntimeError(f"{_SUBSCRIPTION_TOKEN_FILE_ENV} must have mode 0600: {path}")

    try:
        token = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise RuntimeError(
            f"{_SUBSCRIPTION_TOKEN_FILE_ENV} is configured but unreadable: {path}"
        ) from exc
    if not token or any(character.isspace() for character in token):
        raise RuntimeError(f"{_SUBSCRIPTION_TOKEN_FILE_ENV} does not contain one valid token")
    return token


def _scrubbed_env(effort: str | None = None) -> dict[str, str]:
    """Build the Claude-lane-only subscription environment.

    Provider keys, gateway routing, and cloud-provider switches are removed so
    they cannot outrank subscription OAuth. A one-year token generated by
    ``claude setup-token`` may be supplied through the Builder-only token-file
    path; otherwise Claude falls back to its normal persisted ``/login`` state.
    """
    env = dict(os.environ)
    for variable in _NON_SUBSCRIPTION_AUTH_VARS:
        env.pop(variable, None)
    subscription_token = _subscription_token_from_file()
    if subscription_token:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = subscription_token
    budget = _EFFORT_THINKING_TOKENS.get(effort or "")
    if budget:
        env["MAX_THINKING_TOKENS"] = str(budget)
    return env


def _classify(returncode: int | None, stdout: str, stderr: str) -> dict[str, Any]:
    session_id: str | None = None
    parsed: dict[str, Any] | None = None
    text = (stdout or "").strip()
    # Collect EVERY parseable per-line JSON object. For stream-json this is the full
    # ordered event list (system/assistant/user/result lines); for plain json it is
    # the single envelope. The envelope used for status/usage/duration is recovered
    # below exactly as before (whole-string parse, else the last {...} line).
    objs: list[Any] = []
    if text:
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            for line in reversed(text.splitlines()):
                s = line.strip()
                if s.startswith("{") and s.endswith("}"):
                    try:
                        parsed = json.loads(s)
                        break
                    except json.JSONDecodeError:
                        continue
        for line in text.splitlines():
            s = line.strip()
            if s.startswith("{") and s.endswith("}"):
                try:
                    objs.append(json.loads(s))
                except json.JSONDecodeError:
                    continue
    if isinstance(parsed, dict):
        session_id = parsed.get("session_id")
        is_error = bool(parsed.get("is_error"))
        low = str(parsed.get("result", "")).lower()
        api_status = parsed.get("api_error_status")
        if is_error:
            if "credit balance is too low" in low:
                status = "failed"
            elif api_status == 429 or RATE_LIMIT_PATTERN.search(low) or SESSION_LIMIT_PATTERN.search(low):
                status = "rate_limited"
            elif SESSION_EXPIRED_PATTERN.search(low):
                status = "session_expired"
            else:
                status = "failed"
        else:
            status = "interrupted"  # ran cleanly; artifact validation decides completion
        usage = parsed.get("usage") or {}
        # stream-json (pull) carries many per-line objects -> feed the event list to
        # the extractor so the tool_use/tool_result records are counted. Plain json
        # (push) is a single envelope with no per-message records -> zeros, as today.
        recall = _extract_recall_stats(objs if len(objs) > 1 else parsed)
        return {"status": status, "stdout": stdout, "stderr": stderr,
                "returncode": returncode, "session_id": session_id,
                "input_tokens": int(usage.get("input_tokens") or 0),
                "output_tokens": int(usage.get("output_tokens") or 0),
                "cli_duration_ms": int(parsed.get("duration_ms") or 0),
                "recall_calls": recall["recall_calls"],
                "recall_hits": recall["recall_hits"],
                "decisions_reused": recall["decisions_reused"]}

    # No parseable JSON — fall back to text-pattern classification. Token usage and
    # tool-call records are unavailable here, so emit zeros (R2: still emit the
    # record, never drop it; pull-mode recall stats degrade to zeros, never raise).
    # A CLEAN exit (returncode 0 or None) is never text-classified as a limit/failure,
    # whatever stdout/stderr say — mirrors lane_codex_cli._classify (see there for the
    # false-positive this guards against: a successful run whose transcript merely
    # quotes rate-limit-ish text). Only a non-clean exit is text-classified, and only
    # from STDERR, never the agent's stdout.
    if returncode not in (0, None):
        if SESSION_EXPIRED_PATTERN.search(stderr or ""):
            status = "session_expired"
        elif RATE_LIMIT_PATTERN.search(stderr or "") or SESSION_LIMIT_PATTERN.search(stderr or ""):
            status = "rate_limited"
        elif REAL_ERROR_PATTERN.search(stderr or ""):
            status = "failed"
        else:
            status = "interrupted"
    else:
        status = "interrupted"
    return {"status": status, "stdout": stdout, "stderr": stderr,
            "returncode": returncode, "session_id": session_id,
            "input_tokens": 0, "output_tokens": 0, "cli_duration_ms": 0,
            "recall_calls": 0, "recall_hits": 0, "decisions_reused": 0}


class ClaudeCodeCliLane:
    def __init__(self, process_runner=None):
        self.process_runner = process_runner

    def execute(self, task_ref: dict[str, Any], lane_name: str, attempt_context: dict[str, Any]) -> DispatchResult:
        try:
            work = resolve_work(task_ref, attempt_context)
        except MalformedControlFile as exc:
            # Fail LOUD: a corrupt control file (incl. the yaml-shim "no resolvable phase"
            # case, raised as MalformedControlFile in resolve_work) blocks for human repair
            # via HUMAN_BLOCK — never a silent re-run (R12). A generic ValueError (bad
            # runner-ref) is NOT caught here: it propagates and _complete_attempt fails it
            # loudly, so both the ref-rejection contract and no-crash-loop hold.
            return DispatchResult(
                result_type=DispatchResultType.HUMAN_BLOCK,
                metadata={"spec_id": str(task_ref.get("spec_id") or "unknown"),
                          "reason": f"malformed control file: {exc.path.name}"},
            )
        # Cross-session presence: mirror this autonomous phase into the shared
        # session_presence table so interactive sessions see it (env-gated, never raises).
        try:
            from _dispatch_runtime import lane_presence
            lane_presence.register_lane(work, lane_name)
        except Exception:  # noqa: BLE001 - presence must never break the lane
            pass
        session = load_session(work)
        goal = build_phase_goal(
            work.project_dir, work.specs_dir, work.spec_id, work.phase, work.runner_task_ref,
            plan_gate=bool(attempt_context.get("plan_gate", False)),
            retry_feedback=str(task_ref.get("retry_feedback") or "") or None,
            lane_provider="claude-code-cli",
        )
        pre_snapshot = capture_spec_snapshot(work.specs_dir, work.spec_id, work.phase)
        pre_validation = validate_phase_completion(work.specs_dir, work.spec_id, work.phase)
        pre_source_paths = _git_source_paths(work.project_dir)  # R2 baseline: tree BEFORE the agent runs
        pre_head = _git_head(work.project_dir)  # R2 baseline: HEAD sha BEFORE the agent runs
        maybe_env_up(work, attempt_context)  # prep project env before implement/verify

        capability = work.capability_class or capability_for_phase(work.phase)
        # Model comes from the pinned claude-code-cli column of the registry (single
        # source of truth); claude_model_for is a defensive fallback if a capability is
        # unmapped. spec (deep_reasoner) runs Fable 5 with an Opus 4.8 fallback.
        primary_model = resolve_model(capability, "claude-code-cli") or claude_model_for(capability)
        model_chain = [primary_model, *resolve_model_fallbacks(capability, "claude-code-cli")]
        effort = resolve_effort(capability, "claude-code-cli")  # Fable/plan/impl high; verify xhigh
        session_flags = ["--resume", session.session_id] if session.session_id else []

        # Pull-mode recall (item 4): on the plan phase with MEMORY_RECALL_MODE=="pull"
        # and a hivemind endpoint configured, grant the agent a transient HTTP hive
        # MCP server and allow ONLY the recall tool, so it can self-serve prior art
        # (build_phase_goal injects the one-line directive). Default push is
        # unaffected: no flags, no temp file. We resolve pull_active + the mcp-config
        # FIRST, because pull turns must run `--output-format stream-json --verbose`
        # so the per-message tool_use/tool_result records survive into stdout for
        # _extract_recall_stats; push (and any pull fallback) keeps plain `json`.
        pull_active = _pull_recall_active(work.phase)
        mcp_config_path: str | None = None
        if pull_active:
            mcp_config_path = _write_pull_mcp_config()
            if not mcp_config_path:
                pull_active = False  # endpoint vanished; fall back to no-pull

        if pull_active:
            output_format_flags = ["--output-format", "stream-json", "--verbose"]
        else:
            output_format_flags = ["--output-format", "json"]
        runner = self.process_runner.run if self.process_runner else run_cli_turn
        # R12: only the REAL run_cli_turn gets the queue-scoped pgid registry (fakes get
        # no extra kwarg); this is what the daemon's startup orphan sweep reads.
        _pgid_kwargs = {} if self.process_runner else {"pgid_dir": work.queue_root / "live-pgids"}
        command: list[str] = []
        exec_result: dict[str, Any] = {}
        try:
            for idx, model in enumerate(model_chain):
                command = [
                    "claude", "-p", goal,
                    "--model", model,
                    "--permission-mode", "bypassPermissions",
                    *output_format_flags,
                    *session_flags,
                ]
                if pull_active and mcp_config_path:
                    command += [
                        "--mcp-config", mcp_config_path,
                        "--allowedTools", _HIVE_RECALL_TOOL,
                    ]
                try:
                    started_at = time.monotonic()
                    cli_result = runner(
                        command, cwd=str(work.project_dir), env=_scrubbed_env(effort), timeout=DEFAULT_TIMEOUT,
                        **_pgid_kwargs,
                    )
                    measured_ms = round((time.monotonic() - started_at) * 1000)
                    if hasattr(cli_result, "returncode"):
                        returncode = int(getattr(cli_result, "returncode"))
                        stdout = str(getattr(cli_result, "stdout", "") or "")
                        stderr = str(getattr(cli_result, "stderr", "") or "")
                        timed_out = False
                    else:
                        returncode, stdout, stderr, timed_out = cli_result
                    if timed_out:
                        exec_result = {"status": "timed_out", "stdout": stdout or "",
                                       "stderr": stderr or "agent CLI timed out",
                                       "returncode": None, "session_id": session.session_id}
                    else:
                        exec_result = _classify(returncode, stdout, stderr)
                except FileNotFoundError:
                    exec_result = {"status": "failed", "stdout": "", "stderr": "claude CLI not found",
                                   "returncode": None, "session_id": None}
                    measured_ms = round((time.monotonic() - started_at) * 1000)
                exec_result["model"] = model or ""
                # Preserve Claude's JSON-envelope duration when supplied; otherwise
                # record real runner wall-clock time.
                exec_result["cli_duration_ms"] = int(exec_result.get("cli_duration_ms") or 0) or measured_ms
                # Only a hard `failed` (model unavailable / refusal) falls through to the
                # next model in the chain. Clean runs (`interrupted`) and transient
                # statuses (rate_limited / session_expired / timed_out — the scheduler
                # retries those on the same model) stop the chain here.
                if exec_result.get("status") != "failed" or idx == len(model_chain) - 1:
                    break

            # In pull mode, thread the recall stats recovered from the agent's
            # tool-call records into the plan recall stash so the memory_eval event
            # reflects them (push mode leaves the stash as build_phase_goal set it).
            if pull_active:
                from _dispatch_runtime.phase_runtime import set_plan_recall_stats

                set_plan_recall_stats({
                    "recall_calls": int(exec_result.get("recall_calls") or 0),
                    "recall_hits": int(exec_result.get("recall_hits") or 0),
                    "decisions_reused": int(exec_result.get("decisions_reused") or 0),
                })
        finally:
            if mcp_config_path:
                try:
                    os.unlink(mcp_config_path)
                except OSError:
                    pass

        return finalize_turn(
            work, command, exec_result, pre_snapshot, pre_validation, session,
            lane_name=lane_name, pre_source_paths=pre_source_paths, pre_head=pre_head,
            control_root=_control_root(attempt_context),  # M-D: telemetry sink, not the worktree
        )
