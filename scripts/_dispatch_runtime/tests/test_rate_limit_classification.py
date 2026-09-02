"""Regression: rate/session limits are LANE-classified (returncode + stderr only),
never sniffed from an agent turn's stdout/combined-output text.

decide_post_turn (phase_runtime.py) used to OR the explicit lane status with a regex
scan of stdout+stderr; a turn that merely quoted rate-limit-ish text (docs, test
assertions, a spec describing the dispatcher's own rate-limiting behavior) while
actually making progress got mis-routed to rate-limit-cooldown instead of resuming.
The claude-lane no-JSON fallback classifier had the same shape of bug: it scanned
combined stdout+stderr regardless of returncode, so a clean (returncode 0) run whose
transcript discussed rate limits was misclassified. Both are fixed to trust only the
CLI channel: lane status (decide_post_turn) / returncode+stderr (claude lane).
See test_lane_codex_classify.py for the codex-lane sibling of this fix.
"""
from __future__ import annotations

from _dispatch_runtime.lane_claude_code_cli import _classify
from _dispatch_runtime.phase_runtime import (
    SpecSnapshot,
    ValidationResult,
    decide_post_turn,
)


def _snap(fp="a"):
    return SpecSnapshot(spec_id="s", phase="implement", fingerprint=fp, file_count=1,
                        phase_log_count=1, latest_phase_outcome="SUCCEEDED",
                        spec_status="in_progress", spec_current_phase="implement")


def _val(passed, outcome="SUCCEEDED", reason="ok"):
    return ValidationResult(passed, outcome, reason)


def test_interrupted_status_with_rate_limit_text_in_stdout_resumes_not_cooldown():
    # status=interrupted (explicit lane status, NOT rate_limited) + stdout quoting
    # rate-limit-ish text + real progress between snapshots -> must resume, never
    # be routed to rate-limit-cooldown by a stdout regex scan.
    exec_result = {
        "status": "interrupted",
        "stdout": "Implemented the 429 rate limit / usage limit cooldown handling.",
        "stderr": "",
    }
    d = decide_post_turn(exec_result, _snap("a"), _snap("b"), _val(False), _val(False), 0, 3)
    assert d.outcome == "resume-same-session"


def test_explicit_rate_limited_status_still_triggers_cooldown():
    exec_result = {"status": "rate_limited", "stdout": "", "stderr": ""}
    d = decide_post_turn(exec_result, _snap("a"), _snap("b"), _val(False), _val(False), 0, 3)
    assert d.outcome == "rate-limit-cooldown"


def test_claude_lane_clean_exit_with_quota_words_in_stdout_is_interrupted():
    # returncode 0, no parseable JSON -> falls to the text-pattern branch, but a
    # CLEAN exit must never be text-classified as a limit, whatever stdout says.
    result = _classify(0, "note: quota exceeded warning discussed in the docs", "")
    assert result["status"] == "interrupted"


def test_claude_lane_nonzero_exit_with_rate_limit_in_stderr_is_rate_limited():
    result = _classify(1, "", "error: 429 Too Many Requests")
    assert result["status"] == "rate_limited"


def test_claude_lane_nonzero_exit_with_clean_stderr_is_interrupted():
    result = _classify(1, "", "")
    assert result["status"] == "interrupted"
