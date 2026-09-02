"""Regression: the codex-lane result classifier must read rate-limit / real-error
signals from the CLI channel (returncode + stderr) only — NEVER the agent's stdout.

A spec that documents the dispatcher's own rate-limiting/draining/governor behavior
(the behavioral-SSOT specs) legitimately prints "rate limit" / "usage limit" into
stdout. Scanning stdout classified a *successful* run (returncode 0) as rate_limited,
cooled down the lane, and discarded the work — stalling the whole pipeline while
producing a false "codex is rate-limited" signal.
"""
from __future__ import annotations

from _dispatch_runtime.lane_codex_cli import _classify

# stdout that is ABOUT rate-limiting — the false-positive trigger.
RATE_LIMIT_CONTENT = "\n".join(
    [
        "DECIDED — Rate limits: subscription/quota cooldowns are provider-global.",
        "Only errors classified as provider rate limiting open the cooldown.",
        "usage limit / too many requests / retry-after / 429 all documented here.",
    ]
)


def test_clean_run_with_rate_limit_content_in_stdout_is_not_rate_limited():
    # returncode 0 + rate-limit terms only in stdout (agent content) -> success path.
    assert _classify(0, RATE_LIMIT_CONTENT, "").get("status") == "interrupted"


def test_real_error_words_in_stdout_do_not_mark_failed():
    body = "This spec covers authentication failed / invalid api key handling."
    assert _classify(0, body, "").get("status") == "interrupted"


def test_returncode_75_is_rate_limited():
    assert _classify(75, "", "").get("status") == "rate_limited"


def test_rate_limit_in_stderr_is_rate_limited():
    assert _classify(1, "", "error: rate limit exceeded (429)").get("status") == "rate_limited"


def test_real_error_in_stderr_is_failed():
    assert _classify(1, "", "authentication failed: invalid api key").get("status") == "failed"


def test_clean_exit_with_rate_limit_in_stderr_is_not_rate_limited():
    # A returncode-0 run whose stderr carries `make gate` pytest output about rate-limit tests
    # (governor/scheduler specs) must NOT be tagged rate_limited — a clean exit is a success.
    gate_stderr = "FAILED tests/unit/test_dispatch_cooldown_backoff.py::test_rate_limit_cooldown\n429 usage limit"
    assert _classify(0, "", gate_stderr).get("status") == "interrupted"


def test_clean_exit_with_real_error_words_in_stderr_is_not_failed():
    assert _classify(0, "", "test_authentication_failed_invalid_api_key PASSED").get("status") == "interrupted"
