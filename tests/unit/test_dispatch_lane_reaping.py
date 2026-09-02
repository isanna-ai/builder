"""run_cli_turn must reap the agent's whole process group.

Regression for the orphan bug observed live 2026-06-05: `claude -p` exits while
its descendant agent process keeps running; under plain subprocess.run that
descendant orphaned (reparented to the daemon) and held a model session open.
run_cli_turn launches the CLI as its own session/group leader and SIGTERM→SIGKILL
the whole group in a finally, so descendants cannot outlive the turn.
"""

from __future__ import annotations

import os
import time

from _dispatch_runtime.lane_common import run_cli_turn


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:  # exists but not ours — still "alive"
        return True


def test_run_cli_turn_reaps_orphaned_descendant():
    # bash exits 0 immediately but spawns a backgrounded child (FDs detached so it
    # does not hold the captured pipe) that WOULD orphan under plain subprocess.run.
    cmd = ["bash", "-c", "sleep 30 >/dev/null 2>&1 & echo $!; exit 0"]
    rc, stdout, stderr, timed_out = run_cli_turn(cmd, cwd=".", env=dict(os.environ), timeout=30)
    assert rc == 0
    assert timed_out is False
    child_pid = int((stdout or "").strip().splitlines()[0])
    deadline = time.time() + 5  # SIGTERM delivery is async
    while _alive(child_pid) and time.time() < deadline:
        time.sleep(0.1)
    assert not _alive(child_pid), f"descendant {child_pid} survived run_cli_turn (orphan leak)"


def test_run_cli_turn_timeout_terminates_and_flags():
    # A hung CLI must be terminated on timeout and reported, not waited out.
    cmd = ["bash", "-c", "sleep 30; echo done"]
    t0 = time.time()
    rc, stdout, stderr, timed_out = run_cli_turn(cmd, cwd=".", env=dict(os.environ), timeout=1)
    assert timed_out is True
    assert time.time() - t0 < 20, "run_cli_turn waited out the full sleep instead of killing on timeout"


def test_run_cli_turn_normal_completion_captures_output():
    cmd = ["bash", "-c", "echo hello; exit 0"]
    rc, stdout, stderr, timed_out = run_cli_turn(cmd, cwd=".", env=dict(os.environ), timeout=10)
    assert rc == 0 and timed_out is False
    assert "hello" in (stdout or "")
