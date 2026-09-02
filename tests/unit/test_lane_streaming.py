"""Unit tests for run_cli_turn_streaming — the incremental-stdout helper the MC chat
bridge uses to stream a live claude turn. These spawn a trivial python subprocess (no
claude) to exercise the real line-delivery + timeout/kill/reap path, which stub mode
cannot cover."""

from __future__ import annotations

import os
import sys

from _dispatch_runtime.lane_common import run_cli_turn_streaming


def test_streaming_invokes_on_line_per_stdout_line(tmp_path):
    lines: list[str] = []
    rc, timed_out = run_cli_turn_streaming(
        [sys.executable, "-c",
         "import sys\nfor i in range(3):\n print('line%d' % i); sys.stdout.flush()"],
        cwd=str(tmp_path), env=dict(os.environ), timeout=10,
        on_line=lambda ln: lines.append(ln.strip()),
    )
    assert rc == 0
    assert timed_out is False
    assert [ln for ln in lines if ln] == ["line0", "line1", "line2"]


def test_streaming_times_out_and_kills(tmp_path):
    lines: list[str] = []
    rc, timed_out = run_cli_turn_streaming(
        [sys.executable, "-c",
         "import sys, time\nprint('first'); sys.stdout.flush(); time.sleep(30)"],
        cwd=str(tmp_path), env=dict(os.environ), timeout=1,
        on_line=lambda ln: lines.append(ln.strip()),
    )
    assert timed_out is True
    assert "first" in [ln for ln in lines if ln]


def test_streaming_hard_kills_sigterm_resistant_child(tmp_path):
    """A child that IGNORES SIGTERM (and holds stdout open) must still be SIGKILLed by
    the self-escalating watchdog within ~grace — not wedge the reader for its full
    sleep. Regression guard for the review finding (SIGKILL was gated behind the read
    loop reaching EOF)."""
    import time

    code = (
        "import signal, sys, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"  # defy the graceful term
        "print('alive'); sys.stdout.flush()\n"
        "time.sleep(60)\n"  # would hang the reader for 60s if only SIGTERM were sent
    )
    t0 = time.monotonic()
    rc, timed_out = run_cli_turn_streaming(
        [sys.executable, "-c", code], cwd=str(tmp_path), env=dict(os.environ),
        timeout=1, on_line=lambda ln: None,
    )
    elapsed = time.monotonic() - t0
    assert timed_out is True
    # timeout(1s) + grace(_GROUP_TERM_GRACE=5s) + slack — NOT the child's 60s sleep.
    assert elapsed < 20, f"watchdog failed to hard-kill a SIGTERM-resistant child ({elapsed:.1f}s)"


def test_streaming_merges_stderr_and_survives_consumer_error(tmp_path):
    """stderr is merged into the stream (no deadlock), and an on_line that raises
    must not wedge the reader (the turn still completes)."""
    seen: list[str] = []

    def boom(ln: str) -> None:
        if "explode" in ln:
            raise ValueError("consumer blew up")
        seen.append(ln.strip())

    rc, timed_out = run_cli_turn_streaming(
        [sys.executable, "-c",
         "import sys\nprint('out-ok'); print('explode'); "
         "sys.stderr.write('err-line\\n'); sys.stdout.flush(); sys.stderr.flush()"],
        cwd=str(tmp_path), env=dict(os.environ), timeout=10, on_line=boom,
    )
    assert rc == 0
    assert timed_out is False
    assert "out-ok" in seen
    assert "err-line" in seen  # stderr merged into the single reader stream
