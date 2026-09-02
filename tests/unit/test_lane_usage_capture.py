from __future__ import annotations

import json

from _dispatch_runtime.lane_claude_code_cli import _classify
from _dispatch_runtime.lane_codex_cli import _extract_total_tokens


def test_classify_surfaces_usage_and_duration():
    stdout = json.dumps(
        {
            "session_id": "s",
            "is_error": False,
            "usage": {"input_tokens": 111, "output_tokens": 222},
            "duration_ms": 3456,
        }
    )
    result = _classify(0, stdout, "")
    assert result["input_tokens"] == 111
    assert result["output_tokens"] == 222
    assert result["cli_duration_ms"] == 3456


def test_classify_non_json_zeroes_usage_keys():
    result = _classify(0, "this is not json output", "")
    assert result["input_tokens"] == 0
    assert result["output_tokens"] == 0
    assert result["cli_duration_ms"] == 0


def test_codex_extracts_only_reported_total_tokens():
    assert _extract_total_tokens("work complete\ntokens used\n12,345\n") == 12345
    assert _extract_total_tokens("work complete\ntokens used: 6,789\n") == 6789
    assert _extract_total_tokens("work complete\n") == 0
