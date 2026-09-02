"""Codex CLI lane adapter.

Drives one Builder phase via headless `codex exec`. Confirmed flags (codex
v0.135): `codex exec [-m MODEL] -s <sandbox> -C <dir> --skip-git-repo-check <prompt>`.
Like the claude lane, exit code is not a completion signal — completion is
artifact-gated via phase_runtime.
"""

from __future__ import annotations

import os
import re
import time
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
from _dispatch_runtime.lane_executor import DispatchResult, DispatchResultType, resolve_effort, resolve_model
from _dispatch_runtime.phase_routing import capability_for_phase
from _dispatch_runtime.phase_runtime import (
    RATE_LIMIT_PATTERN,
    REAL_ERROR_PATTERN,
    MalformedControlFile,
    build_phase_goal,
    capture_spec_snapshot,
    validate_phase_completion,
)

DEFAULT_TIMEOUT = 1800


def _classify(returncode: int | None, stdout: str, stderr: str) -> dict[str, Any]:
    # A CLEAN exit (returncode 0) is a SUCCESSFUL run — never rate-limited/failed, whatever its
    # output says. codex signals a rate limit with exit code 75. Only a NON-clean exit may be
    # text-classified, and only from stderr — NEVER the agent's stdout, and never a returncode-0
    # run. The scheduler/draining/governor SSOT specs both WRITE docs about rate-limiting AND run
    # `make gate` (pytest emits rate-limit test names/assertions to stderr); scanning either channel
    # on a clean run tagged a successful attempt rate_limited, cooled the lane, and discarded the
    # work — a false "codex is rate-limited" that stalled the pipeline. See test_lane_codex_classify.
    if returncode == 75:
        return {"status": "rate_limited", "stdout": stdout, "stderr": stderr, "returncode": returncode}
    if returncode not in (0, None):
        err = stderr or ""
        if "rate limited" in err.lower() or RATE_LIMIT_PATTERN.search(err):
            return {"status": "rate_limited", "stdout": stdout, "stderr": stderr, "returncode": returncode}
        if REAL_ERROR_PATTERN.search(err):
            return {"status": "failed", "stdout": stdout, "stderr": stderr, "returncode": returncode}
    # Clean exit (or soft limit that still exited 0) — artifact validation decides completion.
    return {"status": "interrupted", "stdout": stdout, "stderr": stderr, "returncode": returncode}


def _extract_total_tokens(stdout: str) -> int:
    """Return Codex's reported total token count, or zero when it is unavailable.

    Codex prints a total only; it does not expose an input/output split.
    """
    text = stdout or ""
    match = re.search(r"tokens used\s*:\s*([\d,]+)\b", text, re.IGNORECASE)
    if match is None:
        match = re.search(r"tokens used\s*\r?\n\s*([\d,]+)\b", text, re.IGNORECASE)
    if match is None:
        return 0
    try:
        return int(match.group(1).replace(",", ""))
    except ValueError:
        return 0


class CodexCliLane:
    def __init__(self, process_runner=None):
        self.process_runner = process_runner

    def execute(self, task_ref: dict[str, Any], lane_name: str, attempt_context: dict[str, Any]) -> DispatchResult:
        try:
            work = resolve_work(task_ref, attempt_context)
        except MalformedControlFile as exc:
            # Fail LOUD: block for human repair via HUMAN_BLOCK (covers the yaml-shim
            # "no resolvable phase" case). A generic ValueError (bad runner-ref) propagates
            # and _complete_attempt fails it loudly — no silent re-run, no crash-loop (R12).
            return DispatchResult(
                result_type=DispatchResultType.HUMAN_BLOCK,
                metadata={"spec_id": str(task_ref.get("spec_id") or "unknown"),
                          "reason": f"malformed control file: {exc.path.name}"},
            )
        # Cross-session presence (env-gated, never raises) — see lane_presence.
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
            lane_provider="codex-cli",
        )
        pre_snapshot = capture_spec_snapshot(work.specs_dir, work.spec_id, work.phase)
        pre_validation = validate_phase_completion(work.specs_dir, work.spec_id, work.phase)
        pre_source_paths = _git_source_paths(work.project_dir)  # R2 baseline: tree BEFORE the agent runs
        pre_head = _git_head(work.project_dir)  # R2 baseline: HEAD sha BEFORE the agent runs
        maybe_env_up(work, attempt_context)  # prep project env before implement/verify

        capability = work.capability_class or capability_for_phase(work.phase)
        model = resolve_model(capability, "codex-cli")
        effort = resolve_effort(capability, "codex-cli")
        # Codex's own sandbox stays ON unless the operator explicitly turns it off.
        #
        # This used to pass --dangerously-bypass-approvals-and-sandbox unconditionally, on the
        # premise that "the runner already executes inside a sandbox". That premise is a property
        # of ONE deployment: in a container that forbids nested user namespaces, codex's bwrap
        # sandbox cannot start, so the bypass was the only way it would run at all. Anywhere else
        # -- which is everywhere a public user runs this -- the same line silently strips
        # approvals and sandboxing from an agent editing their machine. A safety default must not
        # be inherited from someone else's container.
        #
        # Set BUILDER_CODEX_BYPASS_SANDBOX=1 to restore the old behaviour, knowingly.
        bypass_sandbox = os.environ.get("BUILDER_CODEX_BYPASS_SANDBOX") == "1"
        command = [
            "codex", "exec",
            "--skip-git-repo-check",
            *(["--dangerously-bypass-approvals-and-sandbox"] if bypass_sandbox else []),
            "-C", str(work.project_dir),
            *(["-m", model] if model else []),
            *(["-c", f"model_reasoning_effort={effort}"] if effort else []),
            goal,
        ]

        runner = self.process_runner.run if self.process_runner else run_cli_turn
        # R12: only the REAL run_cli_turn gets the queue-scoped pgid registry (fakes don't).
        _pgid_kwargs = {} if self.process_runner else {"pgid_dir": work.queue_root / "live-pgids"}
        try:
            started_at = time.monotonic()
            cli_result = runner(
                command, cwd=str(work.project_dir), env=dict(os.environ), timeout=DEFAULT_TIMEOUT,
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
                               "stderr": stderr or "agent CLI timed out", "returncode": None}
            else:
                exec_result = _classify(returncode, stdout, stderr)
        except FileNotFoundError:
            exec_result = {"status": "failed", "stdout": "", "stderr": "codex CLI not found", "returncode": None}
            measured_ms = round((time.monotonic() - started_at) * 1000)

        exec_result["model"] = model or ""
        exec_result["total_tokens"] = _extract_total_tokens(str(exec_result.get("stdout") or ""))
        # Codex does not report a provider duration, so this is real subprocess wall time.
        exec_result["cli_duration_ms"] = int(exec_result.get("cli_duration_ms") or 0) or measured_ms

        return finalize_turn(
            work, command, exec_result, pre_snapshot, pre_validation, session,
            lane_name=lane_name, pre_source_paths=pre_source_paths, pre_head=pre_head,
            control_root=_control_root(attempt_context),  # M-D: telemetry sink, not the worktree
        )
