"""Shared lane machinery: resolve work, run a phase turn, decide the outcome.

Both the claude and codex lane adapters are thin wrappers over this: they only
differ in how they build/run the CLI command and classify its raw output. The
common flow — resolve the work item, snapshot+validate before/after, apply the
post-turn decision, persist session continuity, write the attempt log, and map
to a DispatchResult for the scheduler — lives here.
"""

from __future__ import annotations

import json
import os
import select
import signal
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from _dispatch_runtime.lane_executor import DispatchResult, DispatchResultType
from _dispatch_runtime.paths import RUNTIME_DIR_NAMES, runtime_dir
from _dispatch_runtime.phase_runtime import (
    POST_GATE_PHASES,
    SYNC_RESULT_LOCKED_PATHS,
    MalformedControlFile,
    PostTurnDecision,
    _safe_yaml,
    capture_spec_snapshot,
    decide_post_turn,
    detect_phase,
    load_control_yaml,
    normalize_phase,
    validate_phase_completion,
)

# Bounded productive resumes before a stuck phase escalates to a human.
DEFAULT_RESUME_BUDGET = 3

# PostTurnDecision.outcome -> control-plane DispatchResultType.
# stale-escalate -> HUMAN_BLOCK is the Move-6 contract: no silent retries.
_DECISION_TO_RESULT: dict[str, DispatchResultType] = {
    "phase-complete": DispatchResultType.SUCCESS,
    "blocked-human": DispatchResultType.HUMAN_BLOCK,
    "stale-escalate": DispatchResultType.HUMAN_BLOCK,
    "rate-limit-cooldown": DispatchResultType.RATE_LIMITED,
    "retry-fresh-session": DispatchResultType.RETRYABLE_ERROR,
    "resume-same-session": DispatchResultType.RETRYABLE_ERROR,
    "cli-failed": DispatchResultType.RETRYABLE_ERROR,
}


# --- Agent CLI runner with guaranteed process-group reaping -----------------
# `claude -p` / `codex exec` spawn descendant agent processes. If we wait only on
# the top-level CLI (plain subprocess.run), those descendants can outlive the
# turn — once the CLI exits and we finalize via the artifact gate, they orphan
# (reparent to the daemon) and keep a model session alive, and can still touch
# the now-complete spec dir. So we launch the CLI as its own session/group leader
# (start_new_session) and SIGTERM→SIGKILL the whole group in a finally, covering
# normal completion, timeout, and error paths alike.
_GROUP_TERM_GRACE = 5.0  # seconds between SIGTERM and SIGKILL of the agent group
_DRAIN_JOIN_TIMEOUT = 2.0  # max wait for a gate-evidence capture drain to exit after the group dies
_DRAIN_POLL_SECONDS = 0.1  # drain select() slice, so a stopped drain exits promptly


def _kill_group(pgid: int, sig: int) -> None:
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def _reap_group(pgid: int) -> None:
    """Best-effort terminate every process still in the agent's group."""
    _kill_group(pgid, signal.SIGTERM)
    deadline = time.monotonic() + _GROUP_TERM_GRACE
    while time.monotonic() < deadline:
        try:
            os.killpg(pgid, 0)  # group still has members?
        except (ProcessLookupError, OSError):
            return  # group empty — done
        time.sleep(0.2)
    _kill_group(pgid, signal.SIGKILL)


# --- Orphan-agent registry (R12) --------------------------------------------
# The finally-reap above covers normal completion/timeout/error, but a SIGKILLed
# daemon (the operator's watchdog-relaunch model makes this routine) never runs it,
# and start_new_session leaves the agent group detached and alive. The next daemon
# could then reclaim the lease and dispatch a SECOND agent into the same tree while
# the orphan is still editing. So the dispatcher lanes record each turn's pgid +
# identity (start-time, cmdline) under the QUEUE dir, and the daemon sweeps survivors
# at startup — but ONLY killing a live process whose recorded identity still matches
# AND looks like an agent, which defeats pid reuse and innocent-process kills. The
# sweep is OPT-IN (default OFF): in a mis-scoped/multi-daemon setup it could reach a
# live process, so enable it only where one daemon owns the queue.
_AGENT_CMDLINE_MARKERS = ("claude", "codex")


def _cmdline_is_agent(cmdline) -> bool:
    """A basename match avoids classifying `/home/claude/postgres` as an agent. # publish-ok: container-user compatibility path"""
    if not cmdline:
        return False
    parts = str(cmdline).split()
    if not parts:
        return False
    base = os.path.basename(parts[0]).lower()
    return base in _AGENT_CMDLINE_MARKERS


def _live_pgids_dir(queue_root) -> Path:
    """The pgid registry for a queue (queue-scoped, NOT cwd-derived), so the lanes'
    record path and the daemon's sweep path always agree."""
    return Path(queue_root) / "live-pgids"


def _pgid_group_alive(pgid: int) -> bool:
    """Whether a process group still has a member, without changing that group."""
    if pgid <= 1:
        return False
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def _proc_identity(pgid: int):
    """(starttime_ticks, cmdline) for the live process-group leader, or None if gone.
    Linux /proc only (the container runtime); None where /proc is unavailable."""
    try:
        with open(f"/proc/{pgid}/stat", encoding="utf-8", errors="replace") as fh:
            after_comm = fh.read().rsplit(")", 1)[-1].split()
        starttime = after_comm[19]  # /proc stat field 22 (starttime), 0-indexed post-comm
        with open(f"/proc/{pgid}/cmdline", "rb") as fh:
            cmdline = fh.read().replace(b"\x00", b" ").decode("utf-8", "replace").strip()
        return starttime, cmdline
    except (OSError, IndexError):
        return None


def _record_live_pgid(pgid_dir, pgid: int) -> None:
    """Record pgid + identity so the startup sweep can prove the live process is still
    OUR agent before killing it. No-op for pgid<=1 (killpg(0/-1) would hit the caller)."""
    if pgid is None or pgid <= 1:
        return
    ident = _proc_identity(pgid) or ("", "")
    try:
        d = Path(pgid_dir)
        d.mkdir(parents=True, exist_ok=True)
        (d / str(pgid)).write_text(
            json.dumps({"pgid": pgid, "starttime": ident[0], "cmdline": ident[1][:400]}),
            encoding="utf-8")
    except Exception:  # noqa: BLE001 - pgid bookkeeping must never break a turn
        pass


def _clear_live_pgid(pgid_dir, pgid: int) -> None:
    try:
        (Path(pgid_dir) / str(pgid)).unlink(missing_ok=True)
    except Exception:  # noqa: BLE001
        pass


def sweep_orphan_pgids(pgid_dir, *, killer=None, identity=None) -> list[int]:
    """Kill agent process groups orphaned by a SIGKILLed predecessor daemon and clear the
    registry. Returns the pgids killed.

    OPT-IN via BUILDER_ORPHAN_SWEEP=1 (default OFF). Safeguards so the sweep never kills
    the wrong thing: pgid>1 only; the live leader's start-time must still match the recorded
    one (defeats pid reuse) AND its cmdline must look like an agent (claude/codex, defeats
    unrelated-process kills). `killer(pid,sig)` / `identity(pid)` injectable for tests."""
    if os.environ.get("BUILDER_ORPHAN_SWEEP", "0").strip().lower() not in ("1", "true", "on", "yes"):
        return []
    pgid_dir = Path(pgid_dir)
    if not pgid_dir.is_dir():
        return []
    kill = killer or (lambda pid, sig: os.killpg(pid, sig))
    ident_fn = identity or _proc_identity
    killed: list[int] = []
    try:
        entries = sorted(pgid_dir.iterdir())
    except OSError:
        return []  # dir vanished mid-scan — nothing to do
    for f in entries:
        try:
            pgid = int(f.name)
        except ValueError:
            continue  # ignore non-pgid files
        try:
            recorded = json.loads(f.read_text(encoding="utf-8") or "{}")
        except Exception:  # noqa: BLE001
            recorded = {}
        if not isinstance(recorded, dict):  # a non-mapping record must never authorize a kill
            recorded = {}
        try:
            if pgid > 1:
                live = ident_fn(pgid)  # None => the group is already gone
                if live is not None:
                    starttime, cmdline = live
                    rec_start = recorded.get("starttime")
                    # FAIL CLOSED: require a non-empty recorded start-time that matches the
                    # live one (defeats pid reuse AND ignores identity-less legacy records),
                    # AND an argv[0]-basename agent match.
                    start_matches = bool(rec_start) and rec_start == starttime
                    if start_matches and _cmdline_is_agent(cmdline):
                        kill(pgid, signal.SIGKILL)  # a verified orphan of a dead daemon
                        killed.append(pgid)
        except (ProcessLookupError, PermissionError, OSError):
            pass  # already dead / not reachable
        finally:
            try:
                f.unlink(missing_ok=True)
            except Exception:  # noqa: BLE001
                pass
    return killed


def run_cli_turn(command: list[str], *, cwd: str, env: dict[str, Any], timeout: int, pgid_dir=None):
    """Run an agent CLI to completion in its OWN process group and guarantee the
    whole group is reaped. Returns (returncode, stdout, stderr, timed_out).

    communicate() returns once the captured pipes hit EOF; the observed agent CLIs
    detach their children's stdio, so that coincides with top-level exit. A future
    child that holds the stdout/stderr pipe open would delay return up to `timeout`
    (same as the prior subprocess.run behavior) — the finally still reaps it.

    `pgid_dir` is OPT-IN (R12): the dispatcher lanes pass their queue's live-pgids dir so
    the startup orphan sweep can find survivors; other callers (distiller/judge) pass None
    and record nothing, so they never pollute an arbitrary cwd or become sweep candidates.

    Raises FileNotFoundError if the CLI binary is missing (caller classifies)."""
    proc = subprocess.Popen(  # noqa: S603 - command is built by the lane, not user input
        command, cwd=cwd, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        start_new_session=True,  # child becomes session+group leader; pgid == proc.pid
    )
    pgid = proc.pid  # guaranteed == process-group id by start_new_session
    if pgid_dir is not None:
        _record_live_pgid(pgid_dir, pgid)  # R12: registry for the startup orphan sweep
    timed_out = False
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_group(pgid, signal.SIGTERM)
        try:
            stdout, stderr = proc.communicate(timeout=_GROUP_TERM_GRACE)
        except subprocess.TimeoutExpired:
            _kill_group(pgid, signal.SIGKILL)
            stdout, stderr = proc.communicate()
    finally:
        # Reap descendants that outlived the top-level CLI (the orphan bug): on a
        # clean turn the CLI exits 0 while its agent children may still be running.
        _reap_group(pgid)
        if pgid_dir is not None:
            _clear_live_pgid(pgid_dir, pgid)  # reaped cleanly — drop it from the registry
    return proc.returncode, stdout, stderr, timed_out


def run_cli_turn_streaming(command: list[str], *, cwd: str, env: dict[str, Any], timeout: int, on_line):
    """Like :func:`run_cli_turn`, but invoke ``on_line(line)`` for each stdout line AS
    IT ARRIVES (interactive streaming) instead of buffering the whole turn — then reap
    the process group exactly like run_cli_turn. Returns ``(returncode, timed_out)``.

    stderr is merged into stdout (``STDOUT``) so a single reader can never deadlock on a
    full stderr pipe; non-JSON diagnostic lines reach ``on_line`` too (the caller filters
    them). A watchdog Timer enforces ``timeout`` by SIGTERM-ing the group (communicate's
    timeout can't be used while reading incrementally), escalating to SIGKILL after the
    grace window. Raises FileNotFoundError if the CLI binary is missing (caller classifies).

    Used by the interactive chat bridge ONLY; the autonomous dispatcher lanes keep using
    the buffered :func:`run_cli_turn`."""
    import threading

    proc = subprocess.Popen(  # noqa: S603 - command is built by the lane, not user input
        command, cwd=cwd, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
        start_new_session=True,  # child becomes session+group leader; pgid == proc.pid
    )
    pgid = proc.pid  # guaranteed == process-group id by start_new_session
    timed_out = threading.Event()
    kill_timer = None  # type: ignore[var-annotated]

    def _hard_kill() -> None:
        if proc.poll() is None:
            _kill_group(pgid, signal.SIGKILL)

    def _fire_timeout() -> None:
        nonlocal kill_timer
        timed_out.set()
        _kill_group(pgid, signal.SIGTERM)
        # Self-escalate on the watchdog's OWN clock. If SIGTERM is trapped/ignored, or
        # a descendant keeps the (merged) stdout write-end open, the blocking read below
        # would never reach the finally — so SIGKILL the whole group here after the
        # grace window, forcing the pipe shut and unblocking the reader. This restores
        # parity with run_cli_turn's wall-clock SIGTERM -> grace -> SIGKILL ladder
        # (whose hard kill is time-driven, not pipe-state-driven).
        kill_timer = threading.Timer(_GROUP_TERM_GRACE, _hard_kill)
        kill_timer.daemon = True
        kill_timer.start()

    timer = threading.Timer(timeout, _fire_timeout)
    timer.daemon = True
    timer.start()
    try:
        # Blocking line iteration: each newline-terminated line is delivered as the CLI
        # flushes it. Loop EOF coincides with the CLI closing stdout (clean exit, or the
        # timeout ladder above SIGKILLing the group), so the read cannot hang past
        # timeout + grace.
        assert proc.stdout is not None
        for line in proc.stdout:
            try:
                on_line(line)
            except Exception:  # noqa: BLE001 - a consumer hiccup must not wedge the reader
                pass
        proc.wait()
    finally:
        timer.cancel()
        if kill_timer is not None:
            kill_timer.cancel()
        if timed_out.is_set() and proc.poll() is None:
            _kill_group(pgid, signal.SIGKILL)
            try:
                proc.wait(timeout=_GROUP_TERM_GRACE)
            except subprocess.TimeoutExpired:
                pass
        # Reap descendants that outlived the top-level CLI (the orphan bug), same as
        # run_cli_turn's finally.
        _reap_group(pgid)
    return proc.returncode, timed_out.is_set()


@dataclass(frozen=True)
class Work:
    work_id: str
    spec_id: str
    phase: str
    project_dir: Path
    specs_dir: Path
    runner_task_ref: str | None
    capability_class: str | None
    queue_root: Path
    log_path: Path


@dataclass
class SessionState:
    session_id: str | None = None
    resume_count: int = 0


def _workspace_root(attempt_context: dict[str, Any]) -> Path:
    raw = attempt_context.get("workspace_root")
    return Path(raw) if raw else Path.cwd()


def _control_root(attempt_context: dict[str, Any]) -> Path:
    """M6: the canonical repo dir — always MAIN (scheduler._execute_item sets
    `control_root` unconditionally to `str(self.project_dir)`). Falls back to
    `_workspace_root` when absent (a bare attempt_context, e.g. an older caller
    or a direct/test invocation), so this is never a hard requirement. When not
    isolated, control_root == workspace_root, so callers keying off it get
    identical behavior to before this field existed."""
    raw = attempt_context.get("control_root")
    return Path(raw) if raw else _workspace_root(attempt_context)


def _spec_id_from_ref(ref: str) -> str | None:
    parts = Path(ref).parts
    if "specs" in parts:
        i = parts.index("specs")
        if i + 1 < len(parts):
            return parts[i + 1]
    return None


def resolve_work(task_ref: dict[str, Any], attempt_context: dict[str, Any]) -> Work:
    """Resolve a queue work item into the concrete phase to drive."""
    project_dir = _workspace_root(attempt_context)
    specs_dir = runtime_dir(project_dir) / "specs"
    queue_root = Path(attempt_context.get("queue_root") or (runtime_dir(project_dir) / "dispatch-queue"))

    runner_task_ref = task_ref.get("runner_task_ref") or task_ref.get("ref")
    runner_task_ref = str(runner_task_ref).strip() if runner_task_ref else None

    spec_id = task_ref.get("spec_id")
    if not spec_id and runner_task_ref:
        spec_id = _spec_id_from_ref(runner_task_ref)
    if not spec_id:
        raise ValueError(f"cannot resolve spec_id from task_ref: {task_ref}")
    spec_id = str(spec_id)

    spec_dir = specs_dir / spec_id
    explicit_phase = task_ref.get("phase")
    if explicit_phase:
        # An explicit phase bypasses detect_phase (which reads spec.yaml), so validate a
        # PRESENT spec.yaml here too — a corrupt control file must never run silently.
        spec_yaml = spec_dir / "spec.yaml"
        if spec_yaml.exists():
            load_control_yaml(spec_yaml)  # raises MalformedControlFile on a non-mapping / unparseable file
    phase = explicit_phase or detect_phase(spec_dir, project_dir, runner_task_ref)
    if not phase:
        spec_yaml = spec_dir / "spec.yaml"
        if spec_yaml.exists():
            # spec.yaml is PRESENT but yields no resolvable phase -> corrupt/garbage
            # (esp. under the permissive yaml shim, which parses garbage to a non-phase
            # string). Fail LOUD as a malformed control file (the lanes -> HUMAN_BLOCK),
            # NOT a generic ValueError (which is the bad-runner-ref rejection above).
            raise MalformedControlFile(spec_yaml, ValueError(f"no resolvable phase for spec '{spec_id}'"))
        raise ValueError(f"cannot resolve phase for spec '{spec_id}' (ref={runner_task_ref})")

    # Plan-approval gate — admission choke point. When the gate is armed, a phase PAST
    # the plan->implement boundary may run ONLY after a human `approve` (which writes
    # the <spec>.approved token). Any OTHER way an item arrived at implement/verify — a
    # crash/lease-reclaim re-dispatch that re-detected the advanced current_phase, or an
    # un-pinned resume — is a gate bypass: fold it back to the plan phase so it re-plans
    # and re-arms the gate. Every lane dispatch resolves here, so this one check covers
    # all re-queue paths, not just the ones the result-handler phase-pin sees.
    if bool(attempt_context.get("plan_gate", False)) and normalize_phase(phase) in POST_GATE_PHASES:
        approved = queue_root / "queue" / "gates" / f"{spec_id}.approved"
        if not approved.exists():
            phase = "plan"

    capability_class = task_ref.get("capability_class")
    if not capability_class and runner_task_ref:
        from _dispatch_runtime.phase_runtime import _safe_yaml  # local import; same module
        packet = _safe_yaml(project_dir / runner_task_ref) or {}
        capability_class = packet.get("capability_class")

    return Work(
        work_id=str(attempt_context.get("work_id") or task_ref.get("work_id") or "unknown"),
        spec_id=spec_id,
        phase=str(phase),
        project_dir=project_dir,
        specs_dir=specs_dir,
        runner_task_ref=runner_task_ref,
        capability_class=str(capability_class) if capability_class else None,
        queue_root=queue_root,
        log_path=queue_root / str(attempt_context.get("log_path") or f"queue/attempts/{attempt_context.get('attempt_id','adhoc')}.log"),
    )


# --- Auto env prep before test-running phases -------------------------------
ENV_PREP_PHASES = {"implement", "verify", "5-implement", "6-verify"}


def _builder_env_script() -> Path:
    return Path(__file__).resolve().parent.parent / "builder-env.py"


def maybe_env_up(work: Work, attempt_context: dict[str, Any], *, runner=None) -> bool:
    """Prepare the project env before a phase that runs tests (implement/verify):
    siblings present, npm ci (if needed), docker reachable. Best-effort — logs but
    never blocks the phase. Returns whether env-up was attempted.

    M6 (Model A): the env-up profile name + `--projects-dir` are derived from
    `control_root` (the canonical MAIN repo — see `_control_root`), NOT from
    `work.project_dir` — under `pipeline.worktree_isolation`, `work.project_dir`
    is the per-spec worktree nested at `<main>/.builder/worktrees/<spec_id>`,
    whose `.name` is the spec id (not the repo name) and whose `.parent` is
    `.builder/worktrees` (not the real projects root where sibling repos
    live) — using it directly would make `PROFILES[<repo>]` miss and sibling
    resolution look in the wrong place. When isolated, the actual env prep
    (npm ci, node_modules, etc.) still needs to land IN the worktree, so a
    `--target-dir` override is passed alongside so `builder-env.py up` selects
    the profile by the canonical name but runs it against the worktree.

    When `control_root == work.project_dir` (not isolated, or no control_root
    given), `--target-dir` is omitted and this argv is byte-identical to before
    M6 existed."""
    if not attempt_context.get("auto_env_up") or work.phase not in ENV_PREP_PHASES:
        return False
    script = _builder_env_script()
    if not script.exists():
        return False
    canonical = _control_root(attempt_context)
    argv = [sys.executable, str(script), "up", canonical.name,
            "--projects-dir", str(canonical.parent)]
    if canonical != work.project_dir:
        argv += ["--target-dir", str(work.project_dir)]
    run = runner or (lambda a: subprocess.run(a, capture_output=True, text=True))
    try:
        run(argv)
    except Exception:  # noqa: BLE001 - env prep must never crash the lane
        pass
    return True


# --- Session continuity (sidecar keyed by work_id; survives re-dispatch) -----
def _session_path(work: Work) -> Path:
    return work.queue_root / "queue" / "sessions" / f"{work.work_id}.json"


def load_session(work: Work) -> SessionState:
    path = _session_path(work)
    if not path.exists():
        return SessionState()
    try:
        data = json.loads(path.read_text(encoding="utf-8")) or {}
        return SessionState(session_id=data.get("session_id"), resume_count=int(data.get("resume_count", 0)))
    except Exception:  # noqa: BLE001
        return SessionState()


def _save_session(work: Work, state: SessionState) -> None:
    path = _session_path(work)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"session_id": state.session_id, "resume_count": state.resume_count}), encoding="utf-8")


def _clear_session(work: Work) -> None:
    path = _session_path(work)
    if path.exists():
        path.unlink()


def _write_attempt_log(work: Work, command: list[str], exec_result: dict[str, Any], decision: PostTurnDecision) -> None:
    try:
        work.log_path.parent.mkdir(parents=True, exist_ok=True)
        body = (
            f"# command: {' '.join(command)}\n"
            f"# status: {exec_result.get('status')}\n"
            f"# decision: {decision.outcome} ({decision.reason})\n"
            f"--- stdout ---\n{exec_result.get('stdout') or ''}\n"
            f"--- stderr ---\n{exec_result.get('stderr') or ''}\n"
        )
        work.log_path.write_text(body, encoding="utf-8")
    except OSError:
        pass


def _memory_mode_for_dispatcher(recall_stats: dict[str, Any]) -> str:
    """Resolve the A/B arm for the dispatcher side (R5 WHERE clause):
    "hivemind" when a hivemind endpoint is configured (so recall/write route to
    it), else "off" (baseline stays capturable with recall_calls=0)."""
    if os.environ.get("HIVEMIND_MCP_URL") and os.environ.get("HIVEMIND_API_KEY"):
        return "hivemind"
    return "off"


def _recall_mode_for_event() -> str:
    """The A/B recall mode for the emitted memory_eval (per SHARED CONTRACT):
    "off" when NEITHER HIVEMIND_* var is set (no hive => recall is structurally
    off); otherwise the configured MEMORY_RECALL_MODE (default "push")."""
    if not os.environ.get("HIVEMIND_MCP_URL") and not os.environ.get("HIVEMIND_API_KEY"):
        return "off"
    return os.environ.get("MEMORY_RECALL_MODE", "push")


def _last_write_stats() -> dict[str, int]:
    """Best-effort read of memory_hook.last_write_stats(), which the memory hook owns. The
    verify-phase real writer populates distilled/deduped there. Defaults to all-zero
    when the symbol is absent (e.g. a fake decision_writer was injected, or an older
    memory_hook) — that zero default is acceptable per the contract."""
    try:
        from _dispatch_runtime import memory_hook

        stats = memory_hook.last_write_stats()
        if isinstance(stats, dict):
            return stats
    except Exception:  # noqa: BLE001 - missing symbol / any error => zero defaults
        pass
    return {}


def _normalize(phase: str) -> str:
    from _dispatch_runtime.phase_runtime import normalize_phase

    return normalize_phase(phase) or str(phase)


def _verify_decisions_and_learned(work: Work) -> tuple[list[str], list[str]]:
    """Source post-verify memory content: decisions from the spec's decisions.yaml,
    learned items from verify-failure notes in the verify phase-log. Best-effort —
    a missing/malformed artifact yields empty lists."""
    from _dispatch_runtime.phase_runtime import _safe_yaml

    spec_dir = work.specs_dir / work.spec_id
    decisions: list[str] = []
    learned: list[str] = []
    dec = _safe_yaml(spec_dir / "decisions.yaml") or {}
    raw_decisions = dec.get("decisions") if isinstance(dec, dict) else None
    if isinstance(raw_decisions, list):
        for entry in raw_decisions:
            if isinstance(entry, dict):
                text = str(entry.get("decision") or entry.get("summary") or entry.get("title") or "").strip()
            else:
                text = str(entry or "").strip()
            if text:
                decisions.append(text)
    log = _safe_yaml(spec_dir / "phase-log.yaml") or {}
    entries = log.get("entries") if isinstance(log, dict) else None
    if isinstance(entries, list):
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            if _normalize(str(entry.get("phase", ""))) != "verify":
                continue
            if str(entry.get("outcome", "")).upper() in ("BLOCKED", "FAILED"):
                note = str(entry.get("notes") or entry.get("note") or "").strip()
                if note:
                    learned.append(note)
    return decisions, learned


def _writer_module(work: Work) -> str:
    """Resolve the memory `module` tag for the post-verify decision write.

    The historical call passed ``work.spec_id`` for BOTH the spec_id and the module
    arg, so the write was tagged ``[spec_id, spec_id]`` — a duplicated, non-grouping
    tag (the [spec_id, spec_id] tag-bug). The module must be DISTINCT from the
    spec_id so recall can group decisions by their target area across specs.

    Best-effort: prefer the spec's target area (the first design
    responsibility-allocation surface, slugified) when it is present and resolves to
    something OTHER than the spec_id; otherwise fall back to the constant
    ``"builder"``. Never raises — a missing/malformed artifact yields the constant.
    """
    try:
        from _dispatch_runtime.phase_runtime import _safe_yaml

        design = _safe_yaml(work.specs_dir / work.spec_id / "design.yaml") or {}
        if isinstance(design, dict):
            alloc = design.get("responsibility_allocation")
            if isinstance(alloc, list):
                for entry in alloc:
                    if not isinstance(entry, dict):
                        continue
                    surface = str(entry.get("surface") or "").strip()
                    if surface:
                        slug = "-".join(surface.lower().split())[:64]
                        if slug and slug != work.spec_id:
                            return slug
    except Exception:  # noqa: BLE001 - module resolution must never fail the turn
        pass
    return "builder"


def _emit_finalize_memory_eval(
    work: Work,
    exec_result: dict[str, Any],
    lane_name: str | None,
    *,
    emitter,
    decision_writer,
    decisions_written: int,
    control_root: Path | None = None,
) -> None:
    """Build + append the memory_eval event for this turn (R6). Plan turns carry
    the stashed recall stats with decisions_written=0; verify turns carry
    decisions_written from the post-verify write. Best-effort: never raises.

    M-D: `control_root` (when given) is the sink for the default `append_memory_eval`
    write, NOT `work.project_dir` — under `pipeline.worktree_isolation`,
    `work.project_dir` is the per-spec isolated worktree, and only
    `.builder/specs/<id>` is symlinked back to main there (NOT
    `.builder/telemetry/`), so a write straight to `work.project_dir` lands in
    the worktree: lost when `_cleanup_worktree` removes it, and the
    scheduler-side dedup (`_emit_memory_eval`'s `memory_eval_emitted` check) then
    drops the main-side emit too, undercounting the Tier-1 A/B clock. Falls back
    to `work.project_dir` when `control_root` is omitted, so a caller that
    doesn't pass it (an older/direct caller, or non-isolated dispatch where the
    two are equal anyway) is unaffected."""
    try:
        from _dispatch_runtime.phase_runtime import last_plan_recall_stats

        phase = _normalize(work.phase)
        recall_stats = last_plan_recall_stats() if phase == "plan" else {
            "recall_calls": 0, "recall_hits": 0, "recall_latency_ms": 0, "decisions_reused": 0,
        }
        lane = "codex" if "codex" in str(lane_name or "").lower() else "claude"
        status = str(exec_result.get("status", "")).lower()
        if status in ("failed", "session_expired"):
            spec_outcome = "failed"
        elif status in ("timed_out",):
            spec_outcome = "blocked"
        else:
            spec_outcome = "unknown"
        # Extended memory-telemetry fields. prior_art_tokens flows from the recall stats
        # (default 0 if absent); distilled/deduped come from the real verify-phase
        # writer's module-global breakdown (0 when a fake writer is injected, which
        # the contract permits); recall_mode per the shared A/B contract.
        write_stats = _last_write_stats()
        stats = {
            "run_id": work.work_id,
            "spec_id": work.spec_id,
            "lane": lane,
            "memory_mode": _memory_mode_for_dispatcher(recall_stats),
            "plan_tokens_in": int(exec_result.get("input_tokens") or 0),
            "plan_tokens_out": int(exec_result.get("output_tokens") or 0),
            "plan_wall_ms": int(exec_result.get("cli_duration_ms") or 0),
            "recall_calls": int(recall_stats.get("recall_calls") or 0),
            "recall_hits": int(recall_stats.get("recall_hits") or 0),
            "recall_latency_ms": int(recall_stats.get("recall_latency_ms") or 0),
            "decisions_reused": int(recall_stats.get("decisions_reused") or 0),
            "decisions_written": int(decisions_written),
            "prior_art_tokens": int(recall_stats.get("prior_art_tokens") or 0),
            "decisions_distilled": int(write_stats.get("distilled") or 0),
            "decisions_deduped": int(write_stats.get("deduped") or 0),
            "recall_mode": _recall_mode_for_event(),
            "spec_outcome": spec_outcome,
        }
        if emitter is not None:
            event = _build_memory_eval_event(stats)
            emitter(event)
            return
        # Default sink: the S4 emit helper (lazy import, swallow ImportError).
        from _telemetry.memory_eval import append_memory_eval, build_memory_eval

        record = build_memory_eval(**{k: v for k, v in stats.items()})
        append_memory_eval(control_root or work.project_dir, record)  # M-D: control root, not the worktree
    except Exception:  # noqa: BLE001 - telemetry must never break the lane
        pass


def _build_memory_eval_event(stats: dict[str, Any]) -> dict[str, Any]:
    """Assemble the exact S4 key set from stats (for an injected emitter that does
    not go through build_memory_eval). Never re-defines the schema."""
    from datetime import datetime, timezone

    return {
        "artifact": "memory_eval",
        "ts": str(stats.get("ts") or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")),
        "run_id": str(stats.get("run_id") or "unknown"),
        "spec_id": str(stats.get("spec_id") or "unknown"),
        "phase": "4-plan",
        "lane": str(stats.get("lane") or "claude"),
        "memory_mode": str(stats.get("memory_mode") or "off"),
        "plan_tokens_in": int(stats.get("plan_tokens_in") or 0),
        "plan_tokens_out": int(stats.get("plan_tokens_out") or 0),
        "plan_wall_ms": int(stats.get("plan_wall_ms") or 0),
        "recall_calls": int(stats.get("recall_calls") or 0),
        "recall_hits": int(stats.get("recall_hits") or 0),
        "recall_latency_ms": int(stats.get("recall_latency_ms") or 0),
        "decisions_reused": int(stats.get("decisions_reused") or 0),
        "decisions_written": int(stats.get("decisions_written") or 0),
        "prior_art_tokens": int(stats.get("prior_art_tokens") or 0),
        "decisions_distilled": int(stats.get("decisions_distilled") or 0),
        "decisions_deduped": int(stats.get("decisions_deduped") or 0),
        "rubric_score": int(stats.get("rubric_score") or 0),
        "recall_mode": str(stats.get("recall_mode") or "push"),
        "spec_outcome": str(stats.get("spec_outcome") or "unknown"),
    }


# --- R1 host-executed verification gate -------------------------------------
_HOST_VERIFY_PHASES = {"implement", "verify", "5-implement", "6-verify"}


def _str_command_list(value) -> list[str]:
    """Coerce a YAML value to a list of command strings, shape-safely: a list -> its string
    items; a bare string -> a one-element list (NEVER iterated char-by-char); else []."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return [str(c) for c in value if isinstance(c, (str, int, float))]
    return []


def _dedup_commands(cmds: list[str]) -> list[str]:
    """Strip, drop empties, keep first-seen order. One helper so every reader below agrees
    about what "the same command twice" means."""
    out: list[str] = []
    for c in cmds:
        c = c.strip()
        if c and c not in out:
            out.append(c)
    return out


def _packet_verify_commands(work: Work) -> list[str]:
    """Verify commands from the runner packet bound to THIS turn: a single runner-task, or
    every task in a phase-batch. Empty when no packet is bound -- which is the normal state of
    a spec that has been planned but never dispatched. Shape-safe against malformed packets."""
    from _dispatch_runtime.phase_runtime import _safe_yaml

    cmds: list[str] = []
    ref = work.runner_task_ref
    if not ref:
        return cmds
    packet = _safe_yaml(work.project_dir / ref) or {}
    if isinstance(packet, dict):
        cmds += _str_command_list(packet.get("verify_commands"))
        tasks = packet.get("tasks")
        for t in (tasks if isinstance(tasks, list) else []):
            tref = t.get("task_ref") if isinstance(t, dict) else None
            if tref:
                td = _safe_yaml(work.project_dir / tref) or {}
                if isinstance(td, dict):
                    cmds += _str_command_list(td.get("verify_commands"))
    return cmds


def _tasks_yaml_verify_commands(work: Work) -> list[str]:
    """The spec's own acceptance commands, read straight from `tasks.yaml`
    (`tasks[].verify[].command`).

    These are written at plan time and are the ONLY spec-specific commands that exist before a
    spec is dispatched: with no `runs/` packet, packet collection returns nothing and a caller
    falls through to the project-wide setup-decisions defaults -- commands identical for every
    spec in the repo, which prove nothing about any one of them. Reading them here is what lets
    a caller ask "do this spec's own acceptance commands already pass?" BEFORE implementing it,
    and get an answer about that spec.

    Shape-safe by design: a missing, unreadable, or malformed artifact returns [] so the caller
    degrades to a refusal, never to a green."""
    from _dispatch_runtime.phase_runtime import _safe_yaml

    data = _safe_yaml(work.specs_dir / work.spec_id / "tasks.yaml") or {}
    if not isinstance(data, dict):
        return []
    cmds: list[str] = []
    tasks = data.get("tasks")
    for task in (tasks if isinstance(tasks, list) else []):
        if not isinstance(task, dict):
            continue
        verify = task.get("verify")
        if isinstance(verify, str):
            cmds += _str_command_list(verify)
            continue
        for item in (verify if isinstance(verify, list) else []):
            if isinstance(item, dict):
                command = item.get("command")
                if isinstance(command, str):
                    cmds += _str_command_list(command)
            elif isinstance(item, str):
                cmds += _str_command_list(item)
    return cmds


def _packet_is_bound(work: Work) -> bool:
    """Whether a runner packet is bound to this turn AT ALL -- independent of whether it yielded
    any commands.

    An EMPTY bound packet is still a bound packet. Deciding boundness by asking whether packet
    collection returned anything (`_packet_verify_commands(work) or ...`) conflates "no packet"
    with "a packet that declares no verify commands", and the second silently fell through to
    plan-time `tasks.yaml` -- judging an in-flight turn against commands it was NOT dispatched
    with, which is the exact substitution the packet-wins rule exists to prevent. Found by
    independent review; the fallthrough was reachable via a packet with `verify_commands: []`."""
    return bool(work.runner_task_ref)


def _spec_scoped_verify_commands(work: Work) -> list[str]:
    """Commands that speak about THIS spec and no other: the bound runner packet when there is
    one, else the spec's plan-time `tasks.yaml` acceptance commands.

    The project-wide setup-decisions defaults are deliberately EXCLUDED. A caller that asked
    about one spec must be able to tell whether that spec has any evidence of its own; folding
    the project defaults in here would make every spec look like it had commands, and would let
    a repo-wide smoke test be reported as a verdict about a single spec.

    The packet wins whenever one is BOUND -- not merely whenever it is non-empty -- so a turn in
    flight is judged against what it was dispatched with rather than against plan-time commands
    that may since have been re-planned. A bound-but-empty packet therefore yields NOTHING, and
    the caller reports the spec unverifiable rather than substituting other evidence."""
    if _packet_is_bound(work):
        return _dedup_commands(_packet_verify_commands(work))
    return _dedup_commands(_tasks_yaml_verify_commands(work))


def _collect_verify_commands(work: Work, *, include_spec_tasks: bool = False) -> list[str]:
    """Host-side verify commands for a turn: the task packet's `verify_commands` (a single
    runner-task, or every task in a phase-batch) plus the setup-decisions command map
    (default test + check). Shape-safe against malformed packets. Deduped; empties dropped.

    `include_spec_tasks` additionally admits the spec's plan-time `tasks.yaml` acceptance
    commands when no runner packet is BOUND (not merely when the packet came back empty -- see
    `_packet_is_bound`). It defaults OFF, and no dispatcher call site passes it: the live
    in-flight gate therefore collects exactly what it collected before this option existed.
    Only an explicitly spec-scoped caller opts in.

    KEYWORD-ONLY on purpose. A positional `_collect_verify_commands(work, True)` would opt the
    live gate in while reading as an ordinary call, and the source-level call-site guard in
    tests/test_host_verify.py cannot recognise that shape. Making the parameter keyword-only
    turns that evasion into a TypeError instead of relying on a regex to notice it."""
    cmds = _packet_verify_commands(work)
    if include_spec_tasks and not _packet_is_bound(work):
        cmds += _tasks_yaml_verify_commands(work)
    # setup-decisions command map: UNION of per-spec AND project-level (order: packet,
    # per-spec, project). A per-spec setup-decisions.yaml with its own `commands` used to
    # shadow the project-level map entirely (first-found-wins) -- a spec that declared only
    # a narrow per-spec command silently dropped the project's real verify commands instead
    # of adding to them. Both are read; dedup below drops any overlap.
    from _dispatch_runtime.phase_runtime import _safe_yaml

    for sd_path in (work.specs_dir / work.spec_id / "setup-decisions.yaml",
                    work.specs_dir.parent / "setup-decisions.yaml"):
        sd = _safe_yaml(sd_path) or {}
        commands = sd.get("commands") if isinstance(sd, dict) else None
        default = commands.get("default") if isinstance(commands, dict) else None
        default = default if isinstance(default, dict) else {}
        for key in ("test", "check"):
            c = default.get(key)
            if isinstance(c, str) and c.strip():
                cmds.append(c)
    return _dedup_commands(cmds)


def _host_verify_aggregate_budget() -> int:
    """Total wall-time budget (seconds) for the WHOLE host-verify gate — bounded well under
    the lease margin (lease 2100s - CLI 1800s = 300s) so the gate can never push a leased
    attempt past its lease and cause a double-dispatch (HIGH-1). Configurable, default 240."""
    try:
        budget = int(os.environ.get("BUILDER_HOST_VERIFY_TIMEOUT", "240") or "240")
    except (TypeError, ValueError):
        budget = 240
    return budget if budget > 0 else 240


def _run_verify_injected(commands: list[str], cwd: str, verify_runner) -> list[str]:
    """Test seam: run each command via the injected runner (returncode / CompletedProcess)."""
    failed: list[str] = []
    for cmd in commands:
        try:
            res = verify_runner(cmd, cwd)
            rc = res if isinstance(res, int) else int(getattr(res, "returncode", 1))
        except Exception:  # noqa: BLE001 - a runner error counts as failure
            rc = 1
        if rc != 0:
            failed.append(cmd)
    return failed


def _utcstamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _tail_text_from_string(text: str, cap: int) -> tuple[str, int, bool]:
    data = (text or "").encode("utf-8")
    tail = data[-cap:].decode("utf-8", "replace").replace("\x00", "")
    return tail, len(data), len(data) > cap


def _run_verify_injected_detailed(commands: list[str], cwd: str, verify_runner) -> list:
    """Detailed test seam mirroring _run_verify_injected's failure semantics."""
    from _dispatch_runtime.gate_evidence import CommandResult, TAIL_BYTES

    results: list[CommandResult] = []
    for cmd in commands:
        started = _utcstamp()
        t0 = time.monotonic()
        try:
            res = verify_runner(cmd, cwd)
            rc = res if isinstance(res, int) else int(getattr(res, "returncode", 1))
            raw_stdout = "" if isinstance(res, int) else getattr(res, "stdout", "")
            raw_stderr = "" if isinstance(res, int) else getattr(res, "stderr", "")
            stdout = raw_stdout if isinstance(raw_stdout, str) else ""
            stderr = raw_stderr if isinstance(raw_stderr, str) else ""
            out_tail, out_total, out_trunc = _tail_text_from_string(stdout, TAIL_BYTES)
            err_tail, err_total, err_trunc = _tail_text_from_string(stderr, TAIL_BYTES)
            results.append(CommandResult(
                command=cmd,
                exit_code=int(rc),
                duration_ms=int((time.monotonic() - t0) * 1000),
                stdout_tail=out_tail,
                stderr_tail=err_tail,
                stdout_bytes_total=out_total,
                stderr_bytes_total=err_total,
                truncated=bool(out_trunc or err_trunc),
                started_at=started,
                finished_at=_utcstamp(),
            ))
        except Exception:  # noqa: BLE001 - a runner error counts as failure
            results.append(CommandResult(
                command=cmd,
                exit_code=None,
                duration_ms=int((time.monotonic() - t0) * 1000),
                spawn_error="runner_raised",
                started_at=started,
                finished_at=_utcstamp(),
            ))
    return results


class _TailDrain(threading.Thread):
    """Continuously drain one pipe, keeping only the last `cap` bytes (a ring buffer).

    Why a pipe + a draining thread, and not a temp file:
      * RLIMIT_FSIZE on the child is WRONG -- it bounds every file the child writes, so a passing
        verify command that emits a big artifact (coverage db, build output) gets SIGXFSZ and is
        recorded as a gate FAILURE. Evidence must never change a verdict.
      * An unbounded temp file is WRONG -- a verify command is agent-authored, and `sh -c 'yes'`
        writes GB/s into TMPDIR (tmpfs == RAM in the container). Polling + truncation does not bound
        it either: a fast writer produces hundreds of MB between polls (measured: 1.1 GB peak).
    Draining continuously is what makes a pipe deadlock-free (it is exactly what communicate() does);
    we keep Popen(start_new_session=True) + the _reap_group() kill, which subprocess.run() would lose.
    Memory is bounded to `cap` bytes per stream regardless of how much the child emits.
    """

    def __init__(self, fd: int, cap: int):
        super().__init__(daemon=True)
        self._fd = fd  # raw read end of a pipe WE own -- started before the child is spawned
        self._cap = cap
        self._buf = bytearray()
        self._total = 0
        self._truncated = False
        self._lock = threading.Lock()
        # NOT `self._stop`: this is a threading.Thread subclass, and CPython's own
        # Thread._stop() is called by _wait_for_tstate_lock() when a join times out.
        # Shadowing it with an Event turns every timed-out join into
        # `TypeError: 'Event' object is not callable` -- which lands in the host-verify
        # command runner, i.e. the one mechanism this project's claim rests on.
        self._stop_event = threading.Event()

    def run(self) -> None:
        # select() + os.read() on the RAW fd, never a blocking stream.read(). A verify command can
        # fork, setsid, exit 0 and leave a grandchild holding the inherited pipe open forever: the
        # drain would then never see EOF. A blocking read in that state also wedges stream.close(),
        # which is how a "bounded" join still hung the gate indefinitely (measured). Polling with a
        # stop flag means the drain ALWAYS exits promptly once the group has been reaped.
        try:
            while not self._stop_event.is_set():
                try:
                    ready, _, _ = select.select([self._fd], [], [], _DRAIN_POLL_SECONDS)
                except InterruptedError:  # EINTR is transient -- an OSError subclass, so catch FIRST
                    continue
                except (OSError, ValueError):  # fd closed underneath us
                    return
                if not ready:
                    continue
                try:
                    chunk = os.read(self._fd, 65536)
                except InterruptedError:
                    continue
                except (OSError, ValueError):
                    return
                if not chunk:  # EOF: every writer closed
                    return
                with self._lock:
                    self._total += len(chunk)
                    self._buf.extend(chunk)
                    excess = len(self._buf) - self._cap
                    if excess > 0:
                        del self._buf[:excess]
                        self._truncated = True
        except Exception:  # noqa: BLE001 - draining must never raise into the gate
            return

    def stop(self) -> None:
        self._stop_event.set()

    def snapshot(self) -> tuple[str, int, bool]:
        with self._lock:
            data, total, trunc = bytes(self._buf), self._total, self._truncated
        return data.decode("utf-8", "replace").replace("\x00", ""), total, trunc


def _run_verify_commands_detailed(commands: list[str], cwd: str, *, capture=False) -> list:
    """Run verify commands with optional deadlock-safe, memory-bounded output capture."""
    from _dispatch_runtime.gate_evidence import CommandResult, TAIL_BYTES

    deadline = time.monotonic() + _host_verify_aggregate_budget()
    scrubbed = {k: v for k, v in os.environ.items()
                if k not in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_API_KEY", "CLAUDE_API_KEY")}
    results: list[CommandResult] = []
    for cmd in commands:
        remaining = deadline - time.monotonic()
        started = _utcstamp()
        t0 = time.monotonic()
        if remaining <= 0:  # aggregate budget exhausted -> the rest can't run in time
            results.append(CommandResult(
                command=cmd,
                exit_code=None,
                duration_ms=0,
                timed_out=True,
                spawn_error="aggregate_budget_exhausted",
                started_at=started,
                finished_at=_utcstamp(),
            ))
            continue
        drains: list[_TailDrain] = []
        read_fds: list[int] = []
        write_fds: list[int] = []
        proc = None
        pgid = None
        reaped = False
        try:
            # Capture is set up BEFORE the command is spawned, never after. If we spawned first and
            # the drains then failed to start, the only recoveries would be (a) leave the pipes
            # undrained -- a chatty child blocks and is misreported as a timeout -- or (b) re-run the
            # command, which EXECUTES A VERIFY COMMAND TWICE (proved: a counter file reached 2). So
            # we own the pipes, start the drains on them, and only then launch. If any of that fails
            # we tear it down and spawn ONCE with DEVNULL -- the command runs exactly once, always.
            if capture:
                try:
                    for _ in range(2):
                        r, w = os.pipe()
                        read_fds.append(r)
                        write_fds.append(w)
                    for fd in read_fds:
                        d = _TailDrain(fd, TAIL_BYTES)
                        d.start()
                        drains.append(d)
                except Exception:  # noqa: BLE001 - e.g. thread exhaustion: degrade to no capture
                    for d in drains:
                        d.stop()
                    for d in drains:
                        d.join(timeout=_DRAIN_JOIN_TIMEOUT)
                    drains = []
                    for fd in read_fds + write_fds:
                        try:
                            os.close(fd)
                        except OSError:
                            pass
                    read_fds = []
                    write_fds = []
            stdout_target = write_fds[0] if drains else subprocess.DEVNULL
            stderr_target = write_fds[1] if drains else subprocess.DEVNULL
            try:
                proc = subprocess.Popen(  # noqa: S602 - cmd from the spec's own verify list
                    cmd, shell=True, cwd=cwd, env=scrubbed, start_new_session=True,
                    stdout=stdout_target, stderr=stderr_target)
            except Exception:  # noqa: BLE001 - spawn failure counts as failure
                results.append(CommandResult(
                    command=cmd,
                    exit_code=None,
                    duration_ms=int((time.monotonic() - t0) * 1000),
                    spawn_error="spawn_failed",
                    started_at=started,
                    finished_at=_utcstamp(),
                ))
                continue
            pgid = proc.pid  # the instant the child exists -- the finally must be able to reap it
            # Drop OUR copies of the write ends: only the child holds them now, so the drains see a
            # real EOF when it exits (and we never hold a pipe open against ourselves).
            for fd in write_fds:
                try:
                    os.close(fd)
                except OSError:
                    pass
            write_fds = []
            timed_out = False
            try:
                rc = proc.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                timed_out = True
                rc = 1
                # Reap OUR direct child first. _reap_group polls killpg(pgid, 0), and an unwaited
                # zombie still counts as a group member -- so without this the group never looks
                # empty and the reap burns its whole grace window (measured: a 1s budget took ~6s).
                try:
                    proc.kill()
                    proc.wait(timeout=_GROUP_TERM_GRACE)
                except Exception:  # noqa: BLE001 - best effort; _reap_group is the backstop
                    pass
            finally:
                _reap_group(pgid)  # kill the whole group (children survive a plain shell kill)
                reaped = True
            stdout_tail = ""
            stderr_tail = ""
            stdout_total = 0
            stderr_total = 0
            stdout_trunc = False
            stderr_trunc = False
            if drains:
                # The group is dead, so its write ends are closed and the drains hit EOF. But a
                # descendant that escaped the group (fork + setsid) can hold the pipe open forever,
                # so we do not rely on EOF: tell the drains to stop and bound the join. They poll a
                # stop flag, so they exit promptly and never wedge the stream close below.
                for d in drains:
                    d.stop()
                for d in drains:
                    d.join(timeout=_DRAIN_JOIN_TIMEOUT)
                stdout_tail, stdout_total, stdout_trunc = drains[0].snapshot()
                stderr_tail, stderr_total, stderr_trunc = drains[1].snapshot()
            results.append(CommandResult(
                command=cmd,
                exit_code=int(rc) if rc is not None else None,
                duration_ms=int((time.monotonic() - t0) * 1000),
                timed_out=timed_out,
                stdout_tail=stdout_tail,
                stderr_tail=stderr_tail,
                stdout_bytes_total=stdout_total,
                stderr_bytes_total=stderr_total,
                truncated=bool(stdout_trunc or stderr_trunc),
                started_at=started,
                finished_at=_utcstamp(),
            ))
        finally:
            # Reap on EVERY post-spawn path, including an exception raised between Popen and wait --
            # but only if the inner path did not already do it. Reaping twice spends TWO grace
            # windows on the timeout path (measured: a 1s budget took ~11s instead of ~6s).
            if pgid is not None and not reaped:
                _reap_group(pgid)
            # Stop + join the drains HERE, so every exit path converges on the same cleanup -- the
            # spawn-failure path `continue`s without ever reaching the stop/join below, and skipping
            # it left the drains alive, which made the read-fd close below bail out and leak the
            # pipe (measured: 10 failed spawns leaked 18 fds). The drains poll a stop flag, so this
            # does not depend on EOF and cannot hang. Re-stopping an already-stopped drain is a no-op.
            for d in drains:
                d.stop()
            for d in drains:
                d.join(timeout=_DRAIN_JOIN_TIMEOUT)
            for fd in write_fds:  # only non-empty if we never reached Popen
                try:
                    os.close(fd)
                except OSError:
                    pass
            for i, fd in enumerate(read_fds):
                # NEVER close an fd a drain could still be reading -- that is how a "bounded" join
                # still wedged the gate. If a drain somehow outlived its join, leak the fd rather
                # than hang the dispatcher.
                if i < len(drains) and drains[i].is_alive():
                    continue
                try:
                    os.close(fd)
                except OSError:
                    pass
    return results


def _run_verify_commands_bounded(commands: list[str], cwd: str) -> list[str]:
    """Run each verify command in its OWN process group, reaping the whole group (children
    too) on timeout — a hung `npm test`/pytest tree cannot survive the gate (MEDIUM-5). Bound
    the TOTAL wall time across all commands to the aggregate budget (HIGH-1). Scrub the agent
    API keys from the child env for parity with the lanes (LOW-8). Returns failed commands."""
    return [r.command for r in _run_verify_commands_detailed(commands, cwd, capture=False) if not r.ok]


def gate_evidence_enabled() -> bool:
    from _dispatch_runtime.gate_evidence import enabled

    return enabled()


def _gate_mode(env_name: str, default: str = "enforce") -> str:
    """Resolve a gate flag to 'off' | 'warn' | 'enforce'. Unrecognized values fall back to the
    default and warn -- a typo must never be the thing that silently removes a gate."""
    from _dispatch_runtime.gate_evidence import gate_mode

    return gate_mode(env_name, default)


def _append_gate_outcome(outcome_sink, *, gate: str, verdict: str, detail: str = "",
                         mode: str = "off", reason: str = "", commands=None) -> None:
    if outcome_sink is None:
        return
    from _dispatch_runtime.gate_evidence import GateOutcome

    outcome_sink.append(GateOutcome(
        gate=gate,
        verdict=verdict,
        detail=detail,
        mode=mode,
        blocking=(verdict == "fail" and mode == "enforce"),
        reason=reason,
        commands=list(commands or []),
    ))


def _host_verify_gate(work: Work, phase: str, *, verify_runner=None, outcome_sink=None) -> tuple[bool | None, str]:
    """R1: run the spec's verify commands HOST-SIDE and gate completion on exit 0, so a
    phase is 'verified' only when tests actually pass — not merely because the agent wrote
    SUCCEEDED. Staged via BUILDER_HOST_VERIFY: 'enforce' (DEFAULT) = block on failure;
    'warn' = run + record but never block; 'off' = no gate. Opting out must be spelled
    exactly 'off' --
    record but never block; 'enforce' = block on failure. Total gate time bounded (HIGH-1);
    hung command trees reaped (MEDIUM-5); a malformed packet is contained, never raised (HIGH-2).

    BUILDER_HOST_VERIFY_REQUIRE_COMMANDS defaults to '1' (fail-closed): a gated phase with
    ZERO verify commands (no per-spec/project setup-decisions.yaml command map) cannot be
    host-verified at all -- an agent's self-report is not evidence, and abstaining silently
    let it count as complete anyway. Set '0' to opt back into the old abstain:no_commands
    behavior for a repo that genuinely has no command map yet (env is per-repo/per-run,
    checked fresh on every call -- no caching).

    Returns (passed, reason): None = not applicable (mode off / non-gated phase / no
    commands + opt-out / warn mode); True = all passed (enforce); False = a command
    failed OR (enforce + no commands + default enforcement) unverifiable."""
    mode = _gate_mode("BUILDER_HOST_VERIFY")
    if mode not in ("warn", "enforce"):
        _append_gate_outcome(outcome_sink, gate="host_verify", verdict="abstain", detail="off", mode="off")
        return None, ""
    if phase not in _HOST_VERIFY_PHASES:
        _append_gate_outcome(outcome_sink, gate="host_verify", verdict="abstain", detail="non_gated_phase", mode=mode)
        return None, ""
    try:
        commands = _collect_verify_commands(work)
        if not commands:
            if (os.environ.get("BUILDER_HOST_VERIFY_REQUIRE_COMMANDS", "1") or "1").strip() == "1":
                reason = "host verify unverifiable: gated phase has zero verify commands"
                ret_reason = reason if mode == "enforce" else f"[warn] {reason}"
                _append_gate_outcome(
                    outcome_sink, gate="host_verify", verdict="fail", detail="unverifiable",
                    mode=mode, reason=ret_reason, commands=[])
                return (False, reason) if mode == "enforce" else (None, ret_reason)
            _append_gate_outcome(
                outcome_sink, gate="host_verify", verdict="abstain", detail="no_commands", mode=mode)
            return None, ""  # nothing to run -> cannot host-gate
        if verify_runner is not None:
            if outcome_sink is not None:
                results = _run_verify_injected_detailed(commands, str(work.project_dir), verify_runner)
                failed = [r.command for r in results if not r.ok]
            else:
                results = []
                failed = _run_verify_injected(commands, str(work.project_dir), verify_runner)
        else:
            if outcome_sink is not None:
                results = _run_verify_commands_detailed(commands, str(work.project_dir), capture=True)
                failed = [r.command for r in results if not r.ok]
            else:
                results = []
                failed = _run_verify_commands_bounded(commands, str(work.project_dir))
    except Exception:  # noqa: BLE001 - HIGH-2: a malformed packet must NOT escape the gate
        _append_gate_outcome(outcome_sink, gate="host_verify", verdict="abstain", detail="error", mode=mode)
        return None, ""
    if not failed:
        _append_gate_outcome(
            outcome_sink, gate="host_verify", verdict="pass", mode=mode, commands=results)
        return (True if mode == "enforce" else None), ""
    reason = f"host verify failed ({len(failed)}/{len(commands)}): {failed[0]}"
    detail = "assertion_failure"
    if outcome_sink is not None:
        from _dispatch_runtime.gate_evidence import classify_failure

        first = next((r for r in results if not r.ok), None)
        if first is not None:
            detail = classify_failure(first)
    ret_reason = reason if mode == "enforce" else f"[warn] {reason}"
    _append_gate_outcome(
        outcome_sink, gate="host_verify", verdict="fail", detail=detail,
        mode=mode, reason=ret_reason, commands=results)
    if mode == "enforce":
        return False, reason
    return None, ret_reason  # recorded in metadata below, but never blocks


def _tdd_required(work: Work) -> bool:
    """True if any task in the turn's packet declares tdd_mode: required. Shape-safe against
    a non-mapping packet (must never raise out of the gate — HIGH-2)."""
    from _dispatch_runtime.phase_runtime import _safe_yaml

    ref = work.runner_task_ref
    if not ref:
        return False
    packet = _safe_yaml(work.project_dir / ref) or {}
    if not isinstance(packet, dict):
        return False
    if str(packet.get("tdd_mode", "")).strip().lower() == "required":
        return True
    tasks = packet.get("tasks")
    for t in (tasks if isinstance(tasks, list) else []):
        tref = t.get("task_ref") if isinstance(t, dict) else None
        if not tref:
            continue
        td = _safe_yaml(work.project_dir / tref) or {}
        if isinstance(td, dict) and str(td.get("tdd_mode", "")).strip().lower() == "required":
            return True
    return False


_TEST_DIR_COMPONENTS = {"test", "tests", "__tests__", "spec", "specs"}


def _looks_like_test(path: str) -> bool:
    """Anchored test-file detection (NOT substring — 'latest.py'/'special.py' must NOT
    match): a test-named basename (test_*, *_test.*, *.test.*, *.spec.*, *_spec.*) or any
    path component that is a test directory."""
    p = path.replace("\\", "/").lower()
    parts = [seg for seg in p.split("/") if seg]
    if any(seg in _TEST_DIR_COMPONENTS for seg in parts[:-1]):
        return True
    base = parts[-1] if parts else ""
    stem = base.rsplit(".", 1)[0] if "." in base else base
    if stem.startswith("test_") or stem.endswith("_test") or stem.endswith("_spec"):
        return True
    return ".test." in base or ".spec." in base


def _git_source_paths(project_dir, git_runner=None) -> set[str] | None:
    """The working-tree changed paths OUTSIDE .builder/ (source), or None if git is
    unavailable / errored — so callers fail SAFE (never a false block on a non-repo). Renames
    count BOTH old and new paths; only `R`-status lines are split on ` -> `. `git_runner(args,
    cwd)` returns a porcelain string or a CompletedProcess (returncode checked)."""
    run = git_runner or (lambda args, cwd: subprocess.run(  # noqa: S603
        ["git", *args], cwd=cwd, capture_output=True, text=True))
    try:
        res = run(["status", "--porcelain"], str(project_dir))
    except Exception:  # noqa: BLE001 - git missing / not a repo -> unavailable
        return None
    if isinstance(res, str):
        out, rc = res, 0
    else:
        out = getattr(res, "stdout", "") or ""
        rc = int(getattr(res, "returncode", 0) or 0)
    if rc != 0:
        return None  # 128 non-repo / unsafe ownership / other git error -> unavailable
    paths: set[str] = set()
    for ln in out.splitlines():
        if len(ln) < 4:
            continue
        xy, rest = ln[:2], ln[3:]
        candidates = rest.split(" -> ") if ("R" in xy and " -> " in rest) else [rest]
        for name in candidates:
            name = name.strip().strip('"')
            if name and not any(name.startswith(f"{runtime_name}/") for runtime_name in RUNTIME_DIR_NAMES):
                paths.add(name)
    return paths


def _git_head(project_dir, git_runner=None) -> str | None:
    """The repo's current HEAD sha, or None if git is unavailable / errored — fail safe (a
    missing HEAD must never be treated as "advanced"). Mirrors `_git_source_paths`'
    runner-shape handling: `git_runner(args, cwd)` returns either a porcelain/string (rc 0
    implied) or a CompletedProcess (returncode checked)."""
    run = git_runner or (lambda args, cwd: subprocess.run(  # noqa: S603
        ["git", *args], cwd=cwd, capture_output=True, text=True))
    try:
        res = run(["rev-parse", "HEAD"], str(project_dir))
    except Exception:  # noqa: BLE001 - git missing / not a repo -> unavailable
        return None
    if isinstance(res, str):
        out, rc = res, 0
    else:
        out = getattr(res, "stdout", "") or ""
        rc = int(getattr(res, "returncode", 0) or 0)
    if rc != 0:
        return None
    sha = out.strip()
    return sha or None


def _git_committed_source_paths(project_dir, base_sha, head_sha, git_runner=None) -> set[str] | None:
    """R2 head-advance case: paths committed between base_sha..head_sha OUTSIDE .builder/ —
    recovers the changed-file list for a turn that COMMITTED its work (leaving a clean working
    tree, which the uncommitted-diff check in `_git_source_paths` cannot see).

    Returns None (distinct "unavailable") on any error / nonzero / exception, so the caller can
    ABSTAIN (fail open) rather than mistake missing evidence for an empty change-set and block a
    genuine committed turn. Returns a set (possibly empty) ONLY on a successful diff. `--no-renames`
    keeps a source/test file renamed INTO .builder/ visible via its outside (deleted) path
    instead of being folded away into a single rename entry."""
    run = git_runner or (lambda args, cwd: subprocess.run(  # noqa: S603
        ["git", *args], cwd=cwd, capture_output=True, text=True))
    try:
        res = run(["diff", "--name-only", "--no-renames", str(base_sha), str(head_sha)], str(project_dir))
    except Exception:  # noqa: BLE001 - git missing / errored -> unavailable
        return None
    if isinstance(res, str):
        out, rc = res, 0
    else:
        out = getattr(res, "stdout", "") or ""
        rc = int(getattr(res, "returncode", 0) or 0)
    if rc != 0:
        return None
    paths: set[str] = set()
    for ln in out.splitlines():
        name = ln.strip().strip('"')
        if name and not any(name.startswith(f"{runtime_name}/") for runtime_name in RUNTIME_DIR_NAMES):
            paths.add(name)
    return paths


def _source_diff_gate(
    work: Work, phase: str, *, pre_source_paths=None, pre_head=None, git_runner=None, outcome_sink=None
) -> tuple[bool | None, str]:
    """R2: an `implement` turn must leave a real SOURCE change (outside .builder/), and a
    tdd-required task must touch a TEST file — else the agent 'implemented' nothing. Compares the
    current changed-source set against the pre-turn baseline captured by the lane BEFORE the agent
    ran, and is evaluated before R1's verify commands (which could mutate the tree). Same staged
    flag as R1.

    An agent that COMMITS its work in-turn leaves a clean working tree, so the uncommitted-delta
    check alone would false-block; when HEAD has advanced past `pre_head` (the pre-turn HEAD the
    lane captured), the committed files between pre_head..HEAD count too (`_git_committed_source_
    paths`). `changed` is the union of the uncommitted delta and (when HEAD advanced) the
    committed files; the tdd-required test-file check runs over that same union.

    Fail-OPEN discipline: the gate BLOCKS only when it can CONFIDENTLY establish there was no
    source change (a clean tree with a genuinely unchanged HEAD, or a definite empty change-set
    from AVAILABLE evidence). Whenever the HEAD probe or the committed-file diff is unavailable
    — or there is no HEAD baseline yet (an unborn-repo first commit, `pre_head` falsy) — it
    ABSTAINS (returns None, no block).

    LIMITATION (why enforce must wait for Phase 2 / R5 worktree isolation): committed paths are
    NOT baseline-content-isolated. This turn's `pre_head..HEAD` diff includes ANY commit made in
    that window, and an already-dirty file that the agent merely stages+commits (without authoring
    a real change) still surfaces here. The gate is therefore an exact per-turn source-change proof
    only under per-spec worktree isolation (P2.2); do NOT flip to `enforce` before that lands.
    Warn mode never blocks, so it is safe to enable warn as-is.

    Returns (passed, reason). None (no block) when: mode off / non-implement phase / no pre-turn
    baseline / any git evidence unavailable / cannot confidently prove no-change."""
    mode = _gate_mode("BUILDER_HOST_VERIFY")
    if mode not in ("warn", "enforce"):
        _append_gate_outcome(outcome_sink, gate="source_diff", verdict="abstain", detail="off", mode="off")
        return None, ""
    if phase not in ("implement", "5-implement"):
        _append_gate_outcome(outcome_sink, gate="source_diff", verdict="abstain", detail="non_gated_phase", mode=mode)
        return None, ""
    if pre_source_paths is None:
        _append_gate_outcome(outcome_sink, gate="source_diff", verdict="abstain", detail="no_baseline", mode=mode)
        return None, ""  # no baseline captured -> cannot prove a per-turn change (fail safe)
    try:
        current = _git_source_paths(work.project_dir, git_runner)
        if current is None:
            _append_gate_outcome(outcome_sink, gate="source_diff", verdict="abstain", detail="git_unavailable", mode=mode)
            return None, ""  # git unavailable / errored -> do not block
        new_paths = set(current) - set(pre_source_paths)

        current_head = _git_head(work.project_dir, git_runner)
        head_advanced = bool(pre_head) and bool(current_head) and pre_head != current_head
        changed = set(new_paths)
        if head_advanced:
            committed = _git_committed_source_paths(work.project_dir, pre_head, current_head, git_runner)
            if committed is None:
                _append_gate_outcome(
                    outcome_sink, gate="source_diff", verdict="abstain",
                    detail="committed_diff_unavailable", mode=mode)
                return None, ""  # committed evidence unavailable -> abstain (fail open)
            changed |= committed
        elif not new_paths and (not pre_head or not current_head):
            # Clean tree AND the HEAD baseline/current is unavailable (unborn repo / rev-parse
            # errored) -> cannot prove no source change was authored -> abstain (fail open).
            _append_gate_outcome(outcome_sink, gate="source_diff", verdict="abstain", detail="unborn_head", mode=mode)
            return None, ""

        def _fail(reason, detail):
            ret = (False, reason) if mode == "enforce" else (None, f"[warn] {reason}")
            _append_gate_outcome(
                outcome_sink, gate="source_diff", verdict="fail", detail=detail,
                mode=mode, reason=ret[1])
            return (False, reason) if mode == "enforce" else (None, f"[warn] {reason}")

        if not changed:
            return _fail("implement changed no NEW source file vs the pre-turn baseline", "no_source_change")
        if _tdd_required(work) and not any(_looks_like_test(f) for f in changed):
            return _fail("tdd-required implement changed no test file", "no_test_change")
        _append_gate_outcome(outcome_sink, gate="source_diff", verdict="pass", mode=mode)
        return (True if mode == "enforce" else None), ""
    except Exception:  # noqa: BLE001 - HIGH-2: never let the gate raise out of finalize_turn
        _append_gate_outcome(outcome_sink, gate="source_diff", verdict="abstain", detail="error", mode=mode)
        return None, ""


def _combine_host_gates(*gates: tuple[bool | None, str]) -> tuple[bool | None, str]:
    """Combine host-gate verdicts: any False dominates (block); else True if any passed;
    else None (nothing gated). Carries the first meaningful reason."""
    verdict: bool | None = None
    reason = ""
    for passed, r in gates:
        if passed is False:
            return False, r
        if passed is True:
            verdict = True
        if r and not reason:
            reason = r
    return verdict, reason


# --- R11 plan-time RED baseline gate ----------------------------------------
# A tdd_mode:required task must be RED before the code exists: its FOCUSED verify
# command (the first/narrowest one) must FAIL (exit != 0) on the pre-implementation
# working tree. If it PASSES (exit 0) before any implementation, the test is
# NON-PROBATIVE — it cannot prove the task's behavior was added — so we BLOCK plan
# approval (the point after which implement turns run). Staged EXACTLY like
# BUILDER_HOST_VERIFY via a NEW flag, BUILDER_RED_BASELINE: 'enforce' (DEFAULT) blocks;
# 'warn' records only; 'off' =
# complete no-op; 'warn' = run + report but never block; 'enforce' = block. The
# live dispatcher runs with this UNSET, so the default path is byte-identical to today.
_RED_BASELINE_PLAN_PHASES = {"plan", "4-plan"}


def _red_baseline_mode() -> str:
    """BUILDER_RED_BASELINE staging, mirroring `_host_verify_gate`'s BUILDER_HOST_VERIFY
    read/normalize style: 'enforce' (DEFAULT) | 'warn' | 'off'. Any unrecognized value ->
    'off' (fail SAFE: an unknown flag value must never start blocking plan approval)."""
    mode = _gate_mode("BUILDER_RED_BASELINE")
    return mode if mode in ("off", "warn", "enforce") else "off"


def _task_tdd_mode(task) -> str:
    """A task's tdd mode, from EITHER the canonical plan form (`tdd: {mode: required}`) or the
    flat runner-packet form (`tdd_mode: required`). '' when absent / a non-mapping task."""
    if not isinstance(task, dict):
        return ""
    tdd = task.get("tdd")
    if isinstance(tdd, dict) and tdd.get("mode"):
        return str(tdd.get("mode")).strip().lower()
    return str(task.get("tdd_mode", "")).strip().lower()


def _task_focused_verify(task) -> str | None:
    """A task's FOCUSED (first/narrowest) verify command, from EITHER the canonical plan form
    (`verify: [{command: ...}, ...]`) or the flat runner-packet form (`verify_commands: [...]`).
    The FIRST non-empty command wins (the focused test); None when there is none. Shape-safe."""
    if not isinstance(task, dict):
        return None
    verify = task.get("verify")
    if isinstance(verify, (list, tuple)):
        for entry in verify:
            c = entry.get("command") if isinstance(entry, dict) else entry
            if isinstance(c, str) and c.strip():
                return c.strip()
    for c in _str_command_list(task.get("verify_commands")):
        if c.strip():
            return c.strip()
    return None


def _plan_red_baseline_tasks(work: Work) -> list:
    """The plan's task list (`tasks:` from tasks.yaml, else plan.yaml) for the RED baseline gate.
    Shape-safe: a missing / malformed artifact or a non-list `tasks` yields []."""
    from _dispatch_runtime.phase_runtime import _safe_yaml

    spec_dir = work.specs_dir / work.spec_id
    for name in ("tasks.yaml", "plan.yaml"):
        data = _safe_yaml(spec_dir / name)
        if isinstance(data, dict) and isinstance(data.get("tasks"), list):
            return data["tasks"]
    return []


def _red_baseline_gate(work: Work, tasks, *, verify_runner=None, phase=None, outcome_sink=None) -> tuple[bool | None, str]:
    """R11: at plan approval, prove every `tdd_mode: required` task's FOCUSED verify command is
    RED (exits non-zero) on the CURRENT pre-implementation working tree. A focused command that
    PASSES (exit 0) before any code exists is NON-PROBATIVE — it cannot prove the task's behavior
    was added — so BLOCK plan approval, naming the offending task id + command.

    Staged via BUILDER_RED_BASELINE: 'enforce' (DEFAULT) = block; 'off' = no-op (returns immediately, runs NOTHING);
    'warn' = run + report but never block; 'enforce' = block on a non-probative task. Reuses the
    same bounded/ injected runner machinery as `_host_verify_gate` (`_run_verify_injected` for tests,
    `_run_verify_commands_bounded` in production), and is contained like the other host gates —
    a malformed packet is swallowed, never raised out of finalize_turn (HIGH-2).

    Returns (passed, reason): None = not applicable (mode off / no tdd-required task with a focused
    command / warn mode); True = every focused command was RED (enforce); False = a tdd-required
    task's focused command PASSED on the pre-implementation tree (enforce)."""
    mode = _red_baseline_mode()
    if mode == "off":
        _append_gate_outcome(outcome_sink, gate="red_baseline", verdict="abstain", detail="off", mode="off")
        return None, ""  # complete no-op — do not even inspect the tasks
    if phase is not None and phase not in _RED_BASELINE_PLAN_PHASES:
        _append_gate_outcome(
            outcome_sink, gate="red_baseline", verdict="abstain", detail="non_gated_phase", mode=mode)
        return None, ""
    if not tasks:
        _append_gate_outcome(outcome_sink, gate="red_baseline", verdict="abstain", detail="no_tasks", mode=mode)
        return None, ""
    try:
        # (task_id, focused_command) for every tdd-required task that has a focused command.
        checks: list[tuple[str, str]] = []
        for t in (tasks if isinstance(tasks, (list, tuple)) else []):
            if _task_tdd_mode(t) != "required":
                continue  # non-tdd-required tasks are ignored
            cmd = _task_focused_verify(t)
            if not cmd:
                continue
            tid = str((t.get("id") or t.get("task_id") or "?")) if isinstance(t, dict) else "?"
            checks.append((tid, cmd))
        if not checks:
            _append_gate_outcome(
                outcome_sink, gate="red_baseline", verdict="abstain", detail="no_tdd_tasks", mode=mode)
            return None, ""  # nothing to prove -> cannot gate
        commands = [c for _tid, c in checks]
        if verify_runner is not None:
            if outcome_sink is not None:
                results = _run_verify_injected_detailed(commands, str(work.project_dir), verify_runner)
                failed = [r.command for r in results if not r.ok]
            else:
                results = []
                failed = _run_verify_injected(commands, str(work.project_dir), verify_runner)
        else:
            if outcome_sink is not None:
                results = _run_verify_commands_detailed(commands, str(work.project_dir), capture=True)
                failed = [r.command for r in results if not r.ok]
            else:
                results = []
                failed = _run_verify_commands_bounded(commands, str(work.project_dir))
    except Exception:  # noqa: BLE001 - a malformed packet must NOT escape the gate (HIGH-2)
        _append_gate_outcome(outcome_sink, gate="red_baseline", verdict="abstain", detail="error", mode=mode)
        return None, ""
    failed_set = set(failed)
    # RED == the command FAILED. A command NOT in the failed set PASSED (exit 0) => non-probative.
    non_probative = [(tid, c) for tid, c in checks if c not in failed_set]
    if not non_probative:
        _append_gate_outcome(
            outcome_sink, gate="red_baseline", verdict="pass", mode=mode, commands=results)
        return (True if mode == "enforce" else None), ""
    tid, c = non_probative[0]
    reason = (
        f"red baseline non-probative: task {tid} focused verify passed (exit 0) on the "
        f"pre-implementation tree — it cannot prove the task's behavior was added: {c}"
    )
    if mode == "enforce":
        _append_gate_outcome(
            outcome_sink, gate="red_baseline", verdict="fail", detail="non_probative",
            mode=mode, reason=reason, commands=results)
        return False, reason
    ret_reason = f"[warn] {reason}"
    _append_gate_outcome(
        outcome_sink, gate="red_baseline", verdict="fail", detail="non_probative",
        mode=mode, reason=ret_reason, commands=results)
    return None, ret_reason  # warn never blocks


# --- P0.1 self-contained runner-packet contract gate ------------------------
# The runner packet is the implementer's EXCLUSIVE runtime interface. Under
# BUILDER_PACKET_CONTRACT=enforce, an implement turn's packet MUST carry the
# normative contract (non-empty objective + steps + done_when + allowed_change_files),
# filled VERBATIM from the approved task, so the runner is never left to INFER the
# desired behavior from source files. Staged EXACTLY like the host gates:
# 'enforce' (DEFAULT) = reject an underinformed packet; 'off' = complete no-op (no inspection, no file read);
# 'enforce' = reject a genuinely underinformed packet. There is no 'warn' rung — a
# malformed contract is a plan-quality defect, not a runtime measurement.
_PACKET_CONTRACT_PHASES = {"implement", "5-implement"}


def _packet_contract_gate(work: Work, phase: str, *, outcome_sink=None) -> tuple[bool | None, str]:
    """Under enforce, verify every per-task packet in this implement turn carries the P0.1
    contract (filled from the approved task). Contained like the other host gates — a malformed
    packet is swallowed, never raised out of finalize_turn (HIGH-2).

    Returns (passed, reason): None = not applicable (mode off / non-implement phase / no packet
    / evidence unavailable); True = every task packet carries the contract; False = a packet is
    missing it under enforce (reason names the task + absent fields)."""
    mode = _gate_mode("BUILDER_PACKET_CONTRACT")
    if mode != "enforce":
        _append_gate_outcome(outcome_sink, gate="packet_contract", verdict="abstain", detail="off", mode="off")
        return None, ""  # strict no-op: no import, no file read
    if phase not in _PACKET_CONTRACT_PHASES:
        _append_gate_outcome(
            outcome_sink, gate="packet_contract", verdict="abstain", detail="non_gated_phase", mode=mode)
        return None, ""  # strict no-op: no import, no file read
    ref = work.runner_task_ref
    if not ref:
        _append_gate_outcome(
            outcome_sink, gate="packet_contract", verdict="abstain", detail="no_packet", mode=mode)
        return None, ""
    try:
        from _dispatch_runtime.packet_contract import (
            apply_contract,
            links_from_traceability,
            validate_packet_contract,
        )
        from _dispatch_runtime.phase_runtime import _approved_task_index, _safe_yaml

        # The `draft` flow enqueues the SPEC DIRECTORY as runner_task_ref (not a packet file), so
        # _safe_yaml() on it cannot parse and the gate used to report `abstain:error` -- a lie. The
        # truth is "there are no packets to check": the autonomous plan directive never emits
        # runs/*.yaml (the phantom-contract hole, SDD review A3). Say so honestly -- `no_packet`
        # leaves the gate UNCOVERED in gate-coverage (0% adjudicated), which is exactly the signal
        # the operator needs to decide "make the packet contract real, or delete it". `error` stays
        # reserved for a packet that exists and is genuinely malformed.
        ref_path = work.project_dir / ref
        spec_dir_ref = work.specs_dir / work.spec_id
        # ONLY the spec directory itself means "no packets to check". A ref that should be a packet
        # FILE but is a directory (e.g. runs/foo.yaml created as a dir) is genuinely malformed and
        # stays `error` -- do not launder a corrupt packet into an honest-looking abstain.
        if ref_path.is_dir() and ref_path.resolve() == spec_dir_ref.resolve():
            runs = sorted(ref_path.glob("runs/*.yaml")) + sorted(ref_path.glob("runs/*.json"))
            if not runs:
                _append_gate_outcome(
                    outcome_sink, gate="packet_contract", verdict="abstain", detail="no_packet", mode=mode)
                return None, ""
            batch = {"tasks": [{"task_ref": str(p.relative_to(work.project_dir))} for p in runs]}
        else:
            batch = _safe_yaml(ref_path)
        if not isinstance(batch, dict):
            _append_gate_outcome(
                outcome_sink, gate="packet_contract", verdict="abstain", detail="error", mode=mode)
            return None, ""
        spec_dir = work.specs_dir / work.spec_id
        task_index = _approved_task_index(spec_dir)
        traceability = _safe_yaml(spec_dir / "traceability.yaml") or {}
        # A single-task ref (task-<id>.yaml) is a degenerate one-task batch.
        entries = batch.get("tasks")
        packets: list[dict] = []
        if isinstance(entries, list) and entries:
            for e in entries:
                tref = e.get("task_ref") if isinstance(e, dict) else None
                if not tref:
                    _append_gate_outcome(
                        outcome_sink, gate="packet_contract", verdict="abstain", detail="error", mode=mode)
                    return None, ""
                p = _safe_yaml(work.project_dir / tref)
                if not isinstance(p, dict):
                    _append_gate_outcome(
                        outcome_sink, gate="packet_contract", verdict="abstain", detail="error", mode=mode)
                    return None, ""
                packets.append(p)
        elif batch.get("task_id"):
            packets.append(batch)
        if not packets:
            _append_gate_outcome(
                outcome_sink, gate="packet_contract", verdict="abstain", detail="no_packet", mode=mode)
            return None, ""  # no packet to inspect -> cannot gate
        for p in packets:
            tid = p.get("task_id")
            task = task_index.get(str(tid)) if tid is not None else None
            links = links_from_traceability(traceability, str(tid)) if tid is not None else None
            filled = apply_contract(p, task, links=links) if isinstance(task, dict) else p
            passed, reason = validate_packet_contract(filled)
            if passed is False:
                _append_gate_outcome(
                    outcome_sink, gate="packet_contract", verdict="fail",
                    detail="contract_missing_fields", mode=mode, reason=reason)
                return False, reason
        _append_gate_outcome(outcome_sink, gate="packet_contract", verdict="pass", mode=mode)
        return True, ""
    except Exception:  # noqa: BLE001 - a malformed packet must NOT escape the gate (HIGH-2)
        _append_gate_outcome(outcome_sink, gate="packet_contract", verdict="abstain", detail="error", mode=mode)
        return None, ""


def _packet_task_ids(work: Work) -> list[str]:
    from _dispatch_runtime.phase_runtime import _safe_yaml

    ref = work.runner_task_ref
    if not ref:
        return []
    ids: list[str] = []
    packet = _safe_yaml(work.project_dir / ref) or {}
    if isinstance(packet, dict):
        tid = packet.get("task_id")
        if tid:
            ids.append(str(tid))
        tasks = packet.get("tasks")
        for t in (tasks if isinstance(tasks, list) else []):
            tref = t.get("task_ref") if isinstance(t, dict) else None
            if not tref:
                continue
            td = _safe_yaml(work.project_dir / tref) or {}
            if isinstance(td, dict) and td.get("task_id"):
                ids.append(str(td.get("task_id")))
    out: list[str] = []
    for tid in ids:
        if tid not in out:
            out.append(tid)
    return out


def _git_evidence_output(project_dir, args, git_runner=None) -> tuple[str, int]:
    try:
        if git_runner is not None:
            res = git_runner(args, str(project_dir))
        else:
            from _dispatch_runtime import gate_evidence

            with tempfile.TemporaryFile() as stdout_file:
                res = subprocess.run(  # noqa: S603
                    ["git", *args], cwd=str(project_dir), stdout=stdout_file,
                    stderr=subprocess.DEVNULL, timeout=gate_evidence.GIT_EVIDENCE_TIMEOUT)
                out, _total, _truncated = gate_evidence.read_tail(
                    stdout_file, gate_evidence.CAPTURE_FILE_BYTES)
            return out, int(getattr(res, "returncode", 0) or 0)
        if isinstance(res, str):
            return res, 0
        out = getattr(res, "stdout", "") or ""
        if isinstance(out, bytes):
            out = out.decode("utf-8", "replace")
        elif not isinstance(out, str):
            out = str(out)
        return out, int(getattr(res, "returncode", 0) or 0)
    except Exception:  # noqa: BLE001
        return "", 1


def _git_diff_patch_tail(project_dir, git_runner=None) -> str:
    try:
        from _dispatch_runtime import gate_evidence

        if git_runner is not None:
            out, rc = _git_evidence_output(project_dir, ["diff", "HEAD"], git_runner)
            if rc != 0:
                return ""
            return _tail_text_from_string(out, gate_evidence.TAIL_BYTES)[0]
        with tempfile.TemporaryFile() as f:
            try:
                res = subprocess.run(  # noqa: S603
                    ["git", "diff", "HEAD"], cwd=str(project_dir),
                    stdout=f, stderr=subprocess.DEVNULL, timeout=gate_evidence.GIT_EVIDENCE_TIMEOUT)
            except Exception:  # noqa: BLE001
                return ""
            if int(getattr(res, "returncode", 0) or 0) != 0:
                return ""
            return gate_evidence.read_tail(f, gate_evidence.TAIL_BYTES)[0]
    except Exception:  # noqa: BLE001
        return ""


def _git_diff_stat(project_dir, git_runner=None) -> dict | None:
    try:
        numstat, rc = _git_evidence_output(project_dir, ["diff", "--numstat", "HEAD"], git_runner)
        if rc != 0:
            return None
        files: list[str] = []
        insertions = 0
        deletions = 0
        for line in numstat.splitlines():
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            ins, dels, path = parts[0], parts[1], "\t".join(parts[2:]).strip().strip('"')
            insertions += int(ins) if ins.isdigit() else 0
            deletions += int(dels) if dels.isdigit() else 0
            if path:
                files.append(path)
        status, src = _git_evidence_output(project_dir, ["status", "--porcelain"], git_runner)
        if src == 0:
            for line in status.splitlines():
                if line.startswith("?? "):
                    name = line[3:].strip().strip('"')
                    if name:
                        files.append(name)
        deduped: list[str] = []
        for name in files:
            if name not in deduped:
                deduped.append(name)
        return {"files_changed": len(deduped), "insertions": insertions, "deletions": deletions, "files": deduped}
    except Exception:  # noqa: BLE001
        return None


def _git_evidence_head(project_dir, git_runner=None) -> str | None:
    out, rc = _git_evidence_output(project_dir, ["rev-parse", "HEAD"], git_runner)
    if rc != 0:
        return None
    sha = out.strip()
    return sha or None


def _deciding_command(outcome):
    commands = list(getattr(outcome, "commands", []) or [])
    if not commands:
        return None
    if outcome.verdict == "fail":
        if outcome.gate == "red_baseline":
            return next((r for r in commands if r.ok), commands[0])
        return next((r for r in commands if not r.ok), commands[0])
    return commands[-1]


def _command_entry(result, cap: int) -> dict:
    stdout_tail, stdout_total, stdout_trunc = _tail_text_from_string(getattr(result, "stdout_tail", "") or "", cap)
    stderr_tail, stderr_total, stderr_trunc = _tail_text_from_string(getattr(result, "stderr_tail", "") or "", cap)
    total_stdout = int(getattr(result, "stdout_bytes_total", 0) or 0)
    total_stderr = int(getattr(result, "stderr_bytes_total", 0) or 0)
    return {
        "command": result.command,
        "exit_code": result.exit_code,
        "duration_ms": int(getattr(result, "duration_ms", 0) or 0),
        "timed_out": bool(getattr(result, "timed_out", False)),
        "spawn_error": getattr(result, "spawn_error", "") or "",
        "stdout_tail": stdout_tail,
        "stderr_tail": stderr_tail,
        "stdout_bytes_total": total_stdout or stdout_total,
        "stderr_bytes_total": total_stderr or stderr_total,
        "truncated": bool(getattr(result, "truncated", False) or stdout_trunc or stderr_trunc),
    }


def _emit_gate_evidence(work, outcomes, *, control_root=None, lane_name=None, git_runner=None) -> None:
    try:
        from _dispatch_runtime import gate_evidence

        def should_emit(o) -> bool:
            if o.verdict == "abstain":
                return False
            if o.gate in ("host_verify", "red_baseline") and o.commands:
                return True
            return o.verdict == "fail"

        emit_outcomes = [o for o in outcomes if should_emit(o)]
        if not emit_outcomes:
            return
        root = Path(control_root) if control_root else work.project_dir
        spec_dir = runtime_dir(root) / "specs" / work.spec_id
        evidence_dir = spec_dir / gate_evidence.EVIDENCE_DIRNAME
        attempt_id = work.log_path.stem if getattr(work, "log_path", None) else "adhoc"
        task_ids = _packet_task_ids(work)
        task_id = task_ids[0] if task_ids else None
        git_head_sha = _git_evidence_head(work.project_dir, git_runner)
        need_diff = any(o.gate in ("host_verify", "source_diff") for o in emit_outcomes)
        need_patch = any(o.verdict == "fail" and o.gate in ("host_verify", "source_diff") for o in emit_outcomes)
        diff_stat = _git_diff_stat(work.project_dir, git_runner) if need_diff else None
        diff_patch_tail = _git_diff_patch_tail(work.project_dir, git_runner) if need_patch else ""
        env_fp = sorted(k for k in os.environ if k not in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_API_KEY", "CLAUDE_API_KEY"))
        for outcome in emit_outcomes:
            deciding = _deciding_command(outcome)
            failure_class = outcome.detail if outcome.verdict == "fail" else None
            body = {
                "schema": gate_evidence.SCHEMA,
                "gate_id": "",
                "seq": 0,
                "gate": outcome.gate,
                "polarity": "red" if outcome.gate == "red_baseline" else "green",
                "spec_id": work.spec_id,
                "phase": _normalize(work.phase),
                "task_id": task_id,
                "task_ids": task_ids,
                "attempt_id": attempt_id,
                "work_id": work.work_id,
                "lane": lane_name,
                "mode": outcome.mode,
                "verdict": outcome.verdict,
                "blocking": bool(outcome.verdict == "fail" and outcome.mode == "enforce"),
                "failure_class": failure_class,
                "failure_reason": outcome.reason if outcome.verdict == "fail" else "",
                "command": ["/bin/sh", "-c", deciding.command] if deciding is not None else None,
                "cwd": str(work.project_dir),
                "env_fingerprint": env_fp,
                "exit_code": deciding.exit_code if deciding is not None else None,
                "started_at": deciding.started_at if deciding is not None else "",
                "finished_at": deciding.finished_at if deciding is not None else "",
                "duration_ms": int(getattr(deciding, "duration_ms", 0) or 0) if deciding is not None else 0,
                "stdout_tail": deciding.stdout_tail if deciding is not None else "",
                "stderr_tail": deciding.stderr_tail if deciding is not None else "",
                "stdout_bytes_total": int(getattr(deciding, "stdout_bytes_total", 0) or 0) if deciding is not None else 0,
                "stderr_bytes_total": int(getattr(deciding, "stderr_bytes_total", 0) or 0) if deciding is not None else 0,
                "truncated": bool(getattr(deciding, "truncated", False)) if deciding is not None else False,
                "timed_out": bool(getattr(deciding, "timed_out", False)) if deciding is not None else False,
                "commands": [
                    _command_entry(r, gate_evidence.CMD_TAIL_BYTES)
                    for r in (getattr(outcome, "commands", []) or [])
                ],
                "git_head_sha": git_head_sha,
                "diff_stat": diff_stat if outcome.gate in ("host_verify", "source_diff") else None,
                "diff_patch_tail": (
                    diff_patch_tail
                    if outcome.verdict == "fail" and outcome.gate in ("host_verify", "source_diff")
                    else ""
                ),
                "host": gate_evidence.host_info(),
                "prev_bundle_sha256": "",
                "bundle_sha256": "",
            }
            path = gate_evidence.write_bundle(evidence_dir, body)
            if path is not None:
                outcome.bundle_path = f"{gate_evidence.EVIDENCE_DIRNAME}/{path.name}"
                outcome.bundle_sha256 = str(body.get("bundle_sha256") or "")
    except Exception:  # noqa: BLE001 - evidence must never break a turn
        return


def _record_sync_implementation_baseline(work, *, pre_source_paths, pre_head, control_root) -> None:
    """Persist the first host-observed implementation baseline for later sync scope proof."""
    if _normalize(work.phase) not in ("implement", "5-implement"):
        return
    spec_dir = work.specs_dir / work.spec_id
    path = spec_dir / "implementation-baseline.yaml"
    if path.exists():
        return
    try:
        from _sync.evidence import atomic_write_yaml, changed_paths_digest, sha256_bytes

        baseline_paths = sorted(pre_source_paths or [])
        main = Path(control_root or work.project_dir).resolve()
        workspace = Path(work.project_dir).resolve()
        transaction_id = sha256_bytes(
            f"{work.spec_id}\0{pre_head or ''}\0{workspace}\0{main}".encode("utf-8")
        )
        atomic_write_yaml(path, {
            "schema": "implementation-baseline/v1",
            "spec": work.spec_id,
            "implementation_baseline": str(pre_head or ""),
            "baseline_paths": baseline_paths,
            "baseline_paths_digest": changed_paths_digest(baseline_paths),
            "worktree_root": str(workspace),
            "control_root": str(main),
            "worktree_isolated": workspace != main and not baseline_paths,
            "transaction_id": transaction_id,
        })
    except Exception:  # noqa: BLE001 - inability to prove scope must fail closed at sync
        return


def _repair_sync_implementation_baseline_if_reconstructible(work, *, control_root):
    """Recover legacy empty-baseline provenance into the isolated verify worktree.

    This is intentionally narrow: only a clean git baseline (no pre-existing source
    paths outside `.builder/`) can be re-anchored to the isolated worktree after the
    fact. If the baseline included uncommitted source files, the original workspace is
    load-bearing and we fail closed.
    """
    spec_dir = work.specs_dir / work.spec_id
    path = spec_dir / "implementation-baseline.yaml"
    baseline = _safe_yaml(path) or {}
    workspace = Path(work.project_dir).resolve()
    main = Path(control_root or work.project_dir).resolve()
    if workspace == main:
        return baseline
    baseline_paths = baseline.get("baseline_paths")
    baseline_paths = baseline_paths if isinstance(baseline_paths, list) else None
    if (
        baseline.get("schema") != "implementation-baseline/v1"
        or baseline.get("spec") != work.spec_id
        or not str(baseline.get("implementation_baseline", "")).strip()
        or baseline_paths is None
        or baseline_paths
    ):
        return baseline
    try:
        from _sync.evidence import atomic_write_yaml, changed_paths_digest, sha256_bytes

        transaction_id = str(baseline.get("transaction_id") or "").strip() or sha256_bytes(
            f"{work.spec_id}\0{baseline.get('implementation_baseline', '')}\0{workspace}\0{main}".encode("utf-8")
        )

        repaired = {
            "schema": "implementation-baseline/v1",
            "spec": work.spec_id,
            "implementation_baseline": str(baseline.get("implementation_baseline", "")).strip(),
            "baseline_paths": [],
            "baseline_paths_digest": changed_paths_digest([]),
            "worktree_root": str(workspace),
            "control_root": str(main),
            "worktree_isolated": True,
            "transaction_id": transaction_id,
        }
        if baseline != repaired:
            atomic_write_yaml(path, repaired)
        return repaired
    except Exception:  # noqa: BLE001 - inability to repair keeps sync fail-closed
        return baseline


def _write_sync_scope_after_verify(work, outcomes, *, control_root, git_runner) -> None:
    """Bind a passing host verify verdict to the isolated baseline-to-verify source manifest."""
    if _normalize(work.phase) != "verify":
        return
    host = next((item for item in (outcomes or []) if item.gate == "host_verify" and item.verdict == "pass"), None)
    if host is None or not host.bundle_path or not host.bundle_sha256:
        return
    spec_dir = work.specs_dir / work.spec_id
    baseline = _repair_sync_implementation_baseline_if_reconstructible(work, control_root=control_root)
    try:
        from _sync.evidence import (
            atomic_write_yaml,
            changed_paths_digest,
            sha256_bytes,
            verified_tree_digest,
        )

        baseline_head = str(baseline.get("implementation_baseline", "")).strip()
        current_head = _git_head(work.project_dir, git_runner)
        current_paths = _git_source_paths(work.project_dir, git_runner)
        committed = _git_committed_source_paths(work.project_dir, baseline_head, current_head, git_runner)
        if not baseline_head or not current_head or current_paths is None or committed is None:
            return
        changed = sorted(set(current_paths) | set(committed))
        workspace = Path(work.project_dir).resolve()
        main = Path(control_root or work.project_dir).resolve()
        isolated = bool(baseline.get("worktree_isolated")) and workspace != main
        atomic_write_yaml(spec_dir / "sync-scope.yaml", {
            "schema": "sync-scope/v1",
            "spec": work.spec_id,
            "implementation_baseline": baseline_head,
            "verified_head": current_head,
            "verified_tree": verified_tree_digest(workspace, changed, current_head),
            "changed_paths": changed,
            "changed_paths_digest": changed_paths_digest(changed),
            "declared_delta_digest": sha256_bytes((spec_dir / "ssot-delta.yaml").read_bytes()),
            "verify_gate_id": f"{work.spec_id}:verify:host_verify:{Path(host.bundle_path).name.split('-', 1)[0]}",
            "verify_gate_bundle": host.bundle_path,
            "verify_gate_sha256": host.bundle_sha256,
            "worktree_root": str(workspace),
            "control_root": str(main),
            "worktree_isolated": isolated,
            "transaction_id": str(baseline.get("transaction_id") or ""),
        })
    except Exception:  # noqa: BLE001 - missing scope evidence makes sync refuse to run
        return


def _run_host_sync_phase(work) -> tuple[int | None, str]:
    """Run the repo hook as the host and project its typed result onto canonical state."""
    if _normalize(work.phase) != "sync":
        return None, ""
    spec_dir = work.specs_dir / work.spec_id
    scope = spec_dir / "sync-scope.yaml"
    isanna = Path(__file__).resolve().parents[1] / "isanna.py"
    try:
        from _sync.evidence import (
            atomic_write_yaml,
            repair_legacy_sync_transaction,
            result_is_corroborated,
            sha256_bytes,
            sync_result_payload_digest,
            validate_scope_evidence,
        )
        from _dispatch_runtime import gate_evidence
        from _validators.common import ValidationContext
        from _validators.sync_artifacts import run_sync_result

        def mark_syncing() -> None:
            spec = load_control_yaml(spec_dir / "spec.yaml") or {}
            spec["status"] = "syncing"
            spec["current_phase"] = "sync"
            atomic_write_yaml(spec_dir / "spec.yaml", spec)

        repair_legacy_sync_transaction(Path(work.project_dir), spec_dir, scope)
        scope_data, scope_errors = validate_scope_evidence(Path(work.project_dir), spec_dir, scope)
        if scope_errors or scope_data is None:
            mark_syncing()
            return 1, f"host sync admission refused: {'; '.join(scope_errors)}"

        result_path = spec_dir / "sync-result.yaml"
        result_path.unlink(missing_ok=True)
        proc = subprocess.run(  # noqa: S603 - fixed interpreter/script argv, no shell
            [sys.executable, str(isanna), "sync", "--root", str(work.project_dir),
             "--spec", work.spec_id, "--scope-evidence", str(scope)],
            cwd=str(work.project_dir), capture_output=True, text=True, timeout=300, check=False,
        )
        hook_result = _safe_yaml(result_path) or {}
        allowed = {"synced", "divergence", "bootstrap_required", "hook_failed"}
        outcome = str(hook_result.get("result", "")).strip()
        tuples_valid = all(
            isinstance(rows, list) and all(
                isinstance(row, dict)
                and str(row.get("category", "")) in {"capabilities", "behaviors", "journeys"}
                and bool(str(row.get("target", "")).strip())
                and str(row.get("change", "")) in {"create", "enrich", "rewire"}
                for row in rows
            )
            for rows in (hook_result.get("observed_tuples"), hook_result.get("undeclared_tuples"))
        )
        pairs_match = all(hook_result.get(key) == scope_data.get(key) for key in (
            "verify_gate_id", "verify_gate_sha256", "verified_tree",
            "changed_paths_digest", "declared_delta_digest",
        ))
        hook_contract_valid = bool(
            outcome in allowed
            and hook_result.get("spec") == work.spec_id
            and hook_result.get("worktree_root") == str(Path(work.project_dir).resolve())
            and hook_result.get("hook_exit_code") == proc.returncode
            and hook_result.get("publish_state") in {"published", "staged-only"}
            and tuples_valid
            and pairs_match
            and isinstance(hook_result.get("preimage_manifest_digest"), str)
            and len(hook_result.get("preimage_manifest_digest", "")) == 64
            and list(hook_result.get("resolution_paths") or []) == list(SYNC_RESULT_LOCKED_PATHS)
            and ((proc.returncode == 0 and outcome == "synced" and hook_result.get("publish_state") == "published")
                 or (proc.returncode != 0 and outcome != "synced" and hook_result.get("publish_state") == "staged-only"))
        )
        effective_exit = int(proc.returncode)
        if hook_contract_valid:
            result = dict(hook_result)
        else:
            effective_exit = effective_exit or 1
            outcome = "hook_failed"
            result = {
                "spec": work.spec_id,
                "worktree_root": str(Path(work.project_dir).resolve()),
                "verify_gate_id": scope_data["verify_gate_id"],
                "verify_gate_sha256": scope_data["verify_gate_sha256"],
                "verified_tree": scope_data["verified_tree"],
                "changed_paths_digest": scope_data["changed_paths_digest"],
                "declared_delta_digest": scope_data["declared_delta_digest"],
                "preimage_manifest_digest": sha256_bytes(b""),
                "observed_tuples": [],
                "undeclared_tuples": [],
                "hook_exit_code": int(proc.returncode),
                "publish_state": "staged-only",
                "result": outcome,
                "resolution_paths": list(SYNC_RESULT_LOCKED_PATHS),
            }

        already_corroborated = hook_contract_valid and result_is_corroborated(spec_dir, result)
        gate_body = {
            "schema": gate_evidence.SCHEMA,
            "gate_id": "",
            "seq": 0,
            "gate": "host_sync",
            "polarity": "green" if outcome == "synced" else "red",
            "spec_id": work.spec_id,
            "phase": "sync",
            "verdict": "pass" if outcome == "synced" and effective_exit == 0 else "fail",
            "hook_exit_code": result["hook_exit_code"],
            "result": outcome,
            "verify_gate_id": result["verify_gate_id"],
            "verified_tree": result["verified_tree"],
            "changed_paths_digest": result["changed_paths_digest"],
            "declared_delta_digest": result["declared_delta_digest"],
            "sync_result_payload_sha256": sync_result_payload_digest(result),
            "prev_bundle_sha256": "",
            "bundle_sha256": "",
        }
        if not already_corroborated:
            bundle = gate_evidence.write_bundle(spec_dir / gate_evidence.EVIDENCE_DIRNAME, gate_body)
            if bundle is None:
                mark_syncing()
                return 1, "host sync gate evidence could not be persisted"
            result["sync_gate_id"] = gate_body["gate_id"]
            result["sync_gate_bundle"] = f"{gate_evidence.EVIDENCE_DIRNAME}/{bundle.name}"
            result["sync_gate_sha256"] = gate_body["bundle_sha256"]
            atomic_write_yaml(result_path, result)
        validation = run_sync_result(ValidationContext(spec_dir=spec_dir))
        corroborated = not validation.errors and not validation.skipped and result_is_corroborated(spec_dir, result)
        spec = load_control_yaml(spec_dir / "spec.yaml") or {}
        if outcome == "synced" and effective_exit == 0 and corroborated:
            spec["status"] = "synced"
        elif outcome == "divergence" and effective_exit != 0 and corroborated:
            spec["status"] = "verified"
        else:
            spec["status"] = "syncing"
        spec["current_phase"] = "sync"
        atomic_write_yaml(spec_dir / "spec.yaml", spec)
        detail = (proc.stderr or proc.stdout or "").strip()
        if not corroborated:
            return 1, "; ".join(validation.errors) or "host sync result was not corroborated"
        return effective_exit, detail
    except Exception as exc:  # noqa: BLE001 - operational failures remain retryable syncing
        try:
            from _sync.evidence import atomic_write_yaml

            spec = load_control_yaml(spec_dir / "spec.yaml") or {}
            spec["status"] = "syncing"
            spec["current_phase"] = "sync"
            atomic_write_yaml(spec_dir / "spec.yaml", spec)
        except Exception:  # noqa: BLE001 - retain the original operational failure
            pass
        return None, f"host sync invocation failed: {exc}"


def finalize_turn(
    work: Work,
    command: list[str],
    exec_result: dict[str, Any],
    pre_snapshot,
    pre_validation,
    session: SessionState,
    *,
    resume_budget: int = DEFAULT_RESUME_BUDGET,
    lane_name: str | None = None,
    emitter=None,
    decision_writer=None,
    force_verify_passed: bool | None = None,
    verify_decisions: list[str] | None = None,
    verify_learned: list[str] | None = None,
    verify_runner=None,
    git_runner=None,
    pre_source_paths=None,
    pre_head=None,
    control_root: Path | None = None,
) -> DispatchResult:
    """Post-turn: validate, decide, persist session, write log, map to DispatchResult.

    Also emits one ``memory_eval`` event for plan/verify turns (R6): plan turns
    carry the stashed recall stats (``decisions_written=0``); a verify turn whose
    completion validation passes writes decision/learned memories via
    ``decision_writer`` (default: ``memory_hook.write_decision_memory``) and reports
    the count. The ``emitter``/``decision_writer``/``force_verify_passed``/
    ``verify_*`` kwargs are injection seams for tests; production resolves them.

    M-D: ``control_root`` (the canonical MAIN repo dir — callers pass
    ``_control_root(attempt_context)``) is threaded to ``_emit_finalize_memory_eval``
    so the default memory_eval sink writes under MAIN's ``.builder/telemetry/``
    even when ``work.project_dir`` is a per-spec isolated worktree. ``None``
    (the default — an older/direct caller) falls back to ``work.project_dir``,
    so this is optional, not a hard requirement.
    """
    _record_sync_implementation_baseline(
        work, pre_source_paths=pre_source_paths, pre_head=pre_head, control_root=control_root)
    sync_exit, sync_reason = _run_host_sync_phase(work)
    post_snapshot = capture_spec_snapshot(work.specs_dir, work.spec_id, work.phase)
    post_validation = validate_phase_completion(work.specs_dir, work.spec_id, work.phase)
    # R1 + R2: host gate. ONLY when the artifact predicate passed (the agent claims done) —
    # no point testing an unfinished turn. Run R2 (source-diff, baseline = pre-agent tree)
    # BEFORE R1 (verify commands, which could mutate the tree). A phase is 'verified' only
    # when tests pass AND implement made a real source change. One flag (BUILDER_HOST_VERIFY),
    # off by default, staged warn->enforce.
    _phase_n = _normalize(work.phase)
    sink = None
    if gate_evidence_enabled():
        from _dispatch_runtime import gate_evidence

        sink = [] if gate_evidence.enabled() else None
    if getattr(post_validation, "passed", False):
        # R11: plan-time RED baseline. Only build the plan's task list (an extra tasks.yaml read)
        # when the flag is actually armed on the plan phase — so with BUILDER_RED_BASELINE unset
        # (the live default) nothing new is read or run: byte-identical to today. Each gate
        # self-guards its own phase (the RED gate no-ops off the plan phase / off flag, the
        # source-diff + host-verify gates no-op off implement/verify), so combining is clean.
        red_tasks = (
            _plan_red_baseline_tasks(work)
            if (_phase_n in _RED_BASELINE_PLAN_PHASES and _red_baseline_mode() != "off")
            else None
        )
        host_gate_passed, host_gate_reason = _combine_host_gates(
            _source_diff_gate(
                work, _phase_n, pre_source_paths=pre_source_paths, pre_head=pre_head,
                git_runner=git_runner, outcome_sink=sink
            ),
            _host_verify_gate(work, _phase_n, verify_runner=verify_runner, outcome_sink=sink),
            _red_baseline_gate(work, red_tasks, verify_runner=verify_runner, phase=_phase_n, outcome_sink=sink),
            # P0.1: reject an underinformed implement packet under BUILDER_PACKET_CONTRACT=
            # enforce (self-guards its own phase/flag — off by default => byte-identical).
            _packet_contract_gate(work, _phase_n, outcome_sink=sink),
        )
    else:
        if sink is not None:
            hv_mode = _gate_mode("BUILDER_HOST_VERIFY")
            hv_mode = hv_mode if hv_mode in ("warn", "enforce") else "off"
            rb_mode = _red_baseline_mode()
            pc_mode = _gate_mode("BUILDER_PACKET_CONTRACT")
            pc_mode = pc_mode if pc_mode == "enforce" else "off"
            for gate, mode in (
                ("host_verify", hv_mode),
                ("source_diff", hv_mode),
                ("red_baseline", rb_mode),
                ("packet_contract", pc_mode),
            ):
                _append_gate_outcome(
                    sink, gate=gate, verdict="abstain", detail="turn_incomplete", mode=mode)
        host_gate_passed, host_gate_reason = None, ""
    if sink:
        _emit_gate_evidence(work, sink, control_root=control_root, lane_name=lane_name, git_runner=git_runner)
        _write_sync_scope_after_verify(work, sink, control_root=control_root, git_runner=git_runner)
    decision = decide_post_turn(
        exec_result, pre_snapshot, post_snapshot, pre_validation, post_validation,
        session.resume_count, resume_budget,
        host_gate_passed=host_gate_passed, host_gate_reason=host_gate_reason,
    )
    if _normalize(work.phase) == "sync":
        sync_result = _safe_yaml(work.specs_dir / work.spec_id / "sync-result.yaml") or {}
        sync_spec = _safe_yaml(work.specs_dir / work.spec_id / "spec.yaml") or {}
        if (
            sync_result.get("result") == "divergence"
            and sync_exit not in (None, 0)
            and sync_spec.get("status") == "verified"
        ):
            decision = PostTurnDecision(
                "blocked-human",
                "verified implementation diverges from the declared delta; choose exactly one: "
                "amend the intent delta, fix the SSOT, or file a narrowing task",
                True,
            )
        elif sync_exit not in (None, 0) and sync_reason:
            host_gate_reason = sync_reason

    # R7: on a host-gate-failed resume, persist the reason so the NEXT turn's goal can
    # inject it once (build_phase_goal -> _render_host_verify_feedback) instead of the agent
    # re-asserting "done" blind and burning the resume budget. The phase is stamped on line 1
    # so the reader can DISCARD feedback that belongs to a different phase (a spec advanced/
    # skipped between turns must never inherit stale feedback). Best-effort — never raises.
    _feedback_path = work.specs_dir / work.spec_id / "host-verify-feedback.txt"
    if decision.outcome == "resume-same-session" and host_gate_reason:
        try:
            _feedback_path.parent.mkdir(parents=True, exist_ok=True)
            _feedback_path.write_text(f"{work.phase}\n{host_gate_reason}", encoding="utf-8")
        except Exception:  # noqa: BLE001 - feedback persistence must never break the turn
            pass

    # Session continuity per decision.
    new_session_id = exec_result.get("session_id") or session.session_id
    if decision.outcome == "resume-same-session":
        _save_session(work, SessionState(session_id=new_session_id, resume_count=session.resume_count + 1))
    elif decision.outcome == "retry-fresh-session":
        _clear_session(work)
    elif decision.outcome in ("phase-complete", "blocked-human", "stale-escalate"):
        _clear_session(work)
        # R7: a resolved/advanced/escalated spec must not carry stale host-verify feedback
        # into a later turn — drop it alongside the session. Best-effort.
        try:
            _feedback_path.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001 - cleanup must never break the turn
            pass
    elif decision.outcome == "rate-limit-cooldown":
        _save_session(work, SessionState(session_id=new_session_id, resume_count=session.resume_count))

    _write_attempt_log(work, command, exec_result, decision)

    # --- S3 / R5+R6: post-verify decision write + memory_eval emission --------
    phase = _normalize(work.phase)
    decisions_written = 0
    # S7 R2: hoist the verify learned notes so the run-ledger call (after the
    # memory_eval block) always has them in scope. None on non-verify phases, so
    # the ledger call never references `learned` directly (no NameError risk).
    verify_learned_for_ledger: list[str] | None = None
    if phase == "verify":
        # A verify turn writes decision/learned memories only when its completion
        # validation passed (terminal verify outcome). Tests force this via
        # `force_verify_passed`; production reads post_validation.passed.
        verify_passed = (
            force_verify_passed
            if force_verify_passed is not None
            else bool(getattr(post_validation, "passed", False))
        )
        if verify_passed:
            decisions = verify_decisions
            learned = verify_learned
            if decisions is None and learned is None:
                decisions, learned = _verify_decisions_and_learned(work)
            decisions = decisions or []
            learned = learned or []
            # S7 R2: capture the in-scope learned notes (verify failures) for the
            # run-ledger verify_runs rows, AFTER `learned` is computed.
            verify_learned_for_ledger = learned
            writer = decision_writer
            if writer is None and (os.environ.get("HIVEMIND_MCP_URL") and os.environ.get("HIVEMIND_API_KEY")):
                try:
                    from _dispatch_runtime.memory_hook import write_decision_memory

                    writer = (
                        lambda sid, mod, decs, lrn, _ln=lane_name: write_decision_memory(sid, mod, decs, lrn, lane=_ln)
                    )
                except Exception:  # noqa: BLE001
                    writer = None
            if writer is not None and (decisions or learned):
                # Tag-bug fix: pass a real module DISTINCT from spec_id so the write
                # is tagged [module, spec_id] (was [spec_id, spec_id]). Always-on
                # because it is strictly correct.
                module = _writer_module(work)
                try:
                    decisions_written = int(
                        writer(work.spec_id, module, decisions, learned)
                    )
                except Exception:  # noqa: BLE001 - a write failure must not fail the turn
                    decisions_written = 0

    if phase in ("plan", "verify"):
        _emit_finalize_memory_eval(
            work, exec_result, lane_name,
            emitter=emitter, decision_writer=decision_writer,
            decisions_written=decisions_written,
            control_root=control_root,  # M-D: control root, not the (possibly isolated) worktree
        )

    # Structured run-ledger row per finalized phase (plan/implement/
    # verify). Additive to the memory_eval emission above — same env gate, same
    # swallow-on-error contract; the ledger MUST NEVER break the dispatch loop.
    # Reads ONLY the hoisted `verify_learned_for_ledger` (never `learned`), so it
    # cannot NameError on a non-verify phase.
    if phase in ("plan", "implement", "verify"):
        try:
            from _dispatch_runtime import run_ledger
            from _dispatch_runtime.phase_runtime import last_plan_recall_stats

            # Tier-1 forward-transfer clock: the spec's plan-time recall exposure.
            # last_plan_recall_stats() persists per-process, so at implement/verify
            # it still holds THIS spec's plan recall (one spec per dispatcher proc).
            _recall = last_plan_recall_stats() or {}
            run_ledger.write_run_ledger(
                work, exec_result, lane_name, decision,
                decisions_learned=verify_learned_for_ledger,
                memory_mode=_memory_mode_for_dispatcher(_recall),
                recall_hits=int(_recall.get("recall_hits") or 0),
            )
        except Exception:  # noqa: BLE001 - ledger must never break the lane
            pass

    # Cross-session presence: this autonomous phase is done — flip the shared
    # session_presence row to ended so interactive roll-call stops showing it.
    # Covers BOTH lanes (claude + codex). Env-gated; never raises.
    try:
        from _dispatch_runtime import lane_presence

        lane_presence.end_lane(work, lane_name)
    except Exception:  # noqa: BLE001 - presence must never break the lane
        pass

    result_type = _DECISION_TO_RESULT.get(decision.outcome, DispatchResultType.RETRYABLE_ERROR)
    metadata = {
        "spec_id": work.spec_id,
        "phase": work.phase,
        "decision": decision.outcome,
        "reason": decision.reason,
        "status": exec_result.get("status"),
        "returncode": exec_result.get("returncode"),
        "session_id": new_session_id,
        "logs": [str(work.log_path)],
        "runner_task_ref": work.runner_task_ref,
        "plan_tokens_in": int(exec_result.get("input_tokens") or 0),
        "plan_tokens_out": int(exec_result.get("output_tokens") or 0),
        "cli_duration_ms": int(exec_result.get("cli_duration_ms") or 0),
        "used_model": str(exec_result.get("model") or ""),
        "total_tokens": int(exec_result.get("total_tokens") or 0) or (
            int(exec_result.get("input_tokens") or 0) + int(exec_result.get("output_tokens") or 0)
        ),
        "decisions_written": decisions_written,
        # Signal the scheduler that this turn already emitted its memory_eval, so
        # it does not emit a duplicate (S4 baseline emit stays for paths that do
        # not flow through finalize_turn, e.g. StubExecutor unit tests).
        "memory_eval_emitted": phase in ("plan", "verify"),
    }
    if sink is not None:
        metadata["gates"] = {o.gate: o.enum_string() for o in sink}
        evidence_entries = [
            {"path": o.bundle_path, "sha256": o.bundle_sha256}
            for o in sink if o.bundle_path and o.bundle_sha256
        ]
        if evidence_entries:
            metadata["gate_evidence"] = evidence_entries
    if host_gate_reason:  # only present when the host gate ran + had something to report
        metadata["host_verify"] = host_gate_reason
    retry_after = exec_result.get("cooldown_seconds")
    return DispatchResult(
        result_type=result_type,
        retry_after=(f"PT{int(retry_after)}S" if retry_after else None),
        metadata=metadata,
    )
