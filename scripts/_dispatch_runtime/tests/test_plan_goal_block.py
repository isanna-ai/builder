"""Task 9 — inject the "Prior art / known pitfalls" block into the plan goal.

RED-first: build a temp specs dir with a spec.yaml summary, stub
memory_hook.plan_prior_art_block, and assert build_phase_goal injects a
=== PRIOR ART / KNOWN PITFALLS === section for the plan phase (and omits it when
the block is empty). Uses unittest.mock.patch (the local runner has no monkeypatch
fixture, but patch works as a context manager).

Item 4 (recall_mode) extends this with push/pull/off cases: MEMORY_RECALL_MODE
unset (default) reproduces push exactly; pull SKIPS plan_prior_art_block and
injects a one-line MCP directive; off (no hivemind endpoint) injects nothing. Env
is controlled via the os.environ pop/restore pattern (no monkeypatch fixture).
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest import mock

from _dispatch_runtime import memory_hook
from _dispatch_runtime.phase_runtime import build_phase_goal, last_plan_recall_stats

# Env keys whose presence/value selects the recall mode in build_phase_goal.
_RECALL_ENV_KEYS = ("MEMORY_RECALL_MODE", "HIVEMIND_MCP_URL", "HIVEMIND_API_KEY")


def _set_recall_env(values: dict[str, str | None]) -> dict[str, str | None]:
    """Apply the given recall-mode env (None => unset) and return the saved prior
    values for restoration. Mirrors the pop/restore idiom in test_memory_hook.py."""
    saved: dict[str, str | None] = {k: os.environ.get(k) for k in _RECALL_ENV_KEYS}
    for key, val in values.items():
        if val is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = val
    return saved


def _restore_recall_env(saved: dict[str, str | None]) -> None:
    for key, val in saved.items():
        if val is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = val


# Push mode is only active with a hivemind endpoint configured; tests that exercise
# the synchronous block set both HIVEMIND_* and leave MEMORY_RECALL_MODE unset.
_PUSH_ENV = {
    "MEMORY_RECALL_MODE": None,
    "HIVEMIND_MCP_URL": "http://memory.example.invalid:8000",
    "HIVEMIND_API_KEY": "test-key",
}


def _make_spec(tmp_path: Path, summary: str) -> tuple[Path, Path, str]:
    project_dir = tmp_path
    specs_dir = tmp_path / ".builder" / "specs"
    spec_id = "demo"
    spec_dir = specs_dir / spec_id
    spec_dir.mkdir(parents=True, exist_ok=True)
    (spec_dir / "spec.yaml").write_text(
        f'name: "{spec_id}"\nstatus: "planned"\ncurrent_phase: "plan"\nsummary: "{summary}"\n',
        encoding="utf-8",
    )
    return project_dir, specs_dir, spec_id


def test_plan_goal_injects_prior_art_section(tmp_path):
    project_dir, specs_dir, spec_id = _make_spec(tmp_path, "auth refactor intent")
    block = "- [decision] use squash merges\n- [learned] pg_trgm required"
    stats = {"recall_calls": 1, "recall_hits": 1, "recall_latency_ms": 7,
             "decisions_reused": 2, "prior_art_tokens": 16}

    saved = _set_recall_env(_PUSH_ENV)
    try:
        with mock.patch.object(
            memory_hook, "plan_prior_art_block", return_value=(block, stats)
        ) as stub:
            goal = build_phase_goal(project_dir, specs_dir, spec_id, "plan", None)
    finally:
        _restore_recall_env(saved)

    # The intent passed to the hook is the spec summary.
    assert stub.call_args.args[0] == "auth refactor intent"
    assert "=== PRIOR ART / KNOWN PITFALLS ===" in goal
    assert "squash merges" in goal
    assert "pg_trgm required" in goal
    # Recall stats are stashed for the lane (Task 10).
    assert last_plan_recall_stats() == stats


def test_plan_goal_omits_section_when_block_empty(tmp_path):
    project_dir, specs_dir, spec_id = _make_spec(tmp_path, "some intent")
    zero = {"recall_calls": 0, "recall_hits": 0, "recall_latency_ms": 0,
            "decisions_reused": 0, "prior_art_tokens": 0}

    saved = _set_recall_env(_PUSH_ENV)
    try:
        with mock.patch.object(memory_hook, "plan_prior_art_block", return_value=("", zero)):
            goal = build_phase_goal(project_dir, specs_dir, spec_id, "plan", None)
    finally:
        _restore_recall_env(saved)

    assert "=== PRIOR ART / KNOWN PITFALLS ===" not in goal
    # The phase still builds (a real, non-empty goal).
    assert "BUILDER AUTONOMOUS PIPELINE SESSION" in goal


def test_non_plan_phase_never_calls_hook(tmp_path):
    project_dir, specs_dir, spec_id = _make_spec(tmp_path, "intent")

    saved = _set_recall_env(_PUSH_ENV)
    try:
        with mock.patch.object(memory_hook, "plan_prior_art_block") as stub:
            goal = build_phase_goal(project_dir, specs_dir, spec_id, "implement", None)
    finally:
        _restore_recall_env(saved)

    stub.assert_not_called()
    assert "=== PRIOR ART / KNOWN PITFALLS ===" not in goal


def test_plan_goal_empty_summary_skips_hook(tmp_path):
    # When summary is absent/empty, plan_prior_art_block is given an empty intent
    # and (per its own contract) returns no block; the section is omitted.
    project_dir, specs_dir, spec_id = _make_spec(tmp_path, "")

    captured = {}

    def fake_block(intent, *, breaker_open):
        captured["intent"] = intent
        return ("", {"recall_calls": 0, "recall_hits": 0, "recall_latency_ms": 0,
                     "decisions_reused": 0, "prior_art_tokens": 0})

    saved = _set_recall_env(_PUSH_ENV)
    try:
        with mock.patch.object(memory_hook, "plan_prior_art_block", side_effect=fake_block):
            goal = build_phase_goal(project_dir, specs_dir, spec_id, "plan", None)
    finally:
        _restore_recall_env(saved)

    assert captured.get("intent", "") == ""
    assert "=== PRIOR ART / KNOWN PITFALLS ===" not in goal


# --- Item 4: recall_mode push / pull / off ----------------------------------
def test_push_mode_default_calls_hook_and_injects_block(tmp_path):
    # MEMORY_RECALL_MODE unset + hivemind configured => push (today's behavior):
    # the synchronous plan_prior_art_block is called and its block injected.
    project_dir, specs_dir, spec_id = _make_spec(tmp_path, "cache layer intent")
    block = "- [decision] use LRU"
    stats = {"recall_calls": 1, "recall_hits": 1, "recall_latency_ms": 3,
             "decisions_reused": 1, "prior_art_tokens": 5}

    saved = _set_recall_env(_PUSH_ENV)
    try:
        with mock.patch.object(
            memory_hook, "plan_prior_art_block", return_value=(block, stats)
        ) as stub:
            goal = build_phase_goal(project_dir, specs_dir, spec_id, "plan", None)
    finally:
        _restore_recall_env(saved)

    stub.assert_called_once()
    assert "=== PRIOR ART / KNOWN PITFALLS ===" in goal
    assert "use LRU" in goal
    # The pull directive must NOT appear in push mode.
    assert "(PULL)" not in goal
    assert last_plan_recall_stats()["decisions_reused"] == 1


def test_pull_mode_skips_hook_and_injects_directive(tmp_path):
    # MEMORY_RECALL_MODE=pull + hivemind configured => the synchronous hook is NOT
    # called; a one-line directive to call mcp__hive__hive_search_memories is added.
    project_dir, specs_dir, spec_id = _make_spec(tmp_path, "pull intent")

    saved = _set_recall_env({
        "MEMORY_RECALL_MODE": "pull",
        "HIVEMIND_MCP_URL": "http://memory.example.invalid:8000",
        "HIVEMIND_API_KEY": "test-key",
    })
    try:
        with mock.patch.object(memory_hook, "plan_prior_art_block") as stub:
            goal = build_phase_goal(project_dir, specs_dir, spec_id, "plan", None)
    finally:
        _restore_recall_env(saved)

    stub.assert_not_called()
    assert "mcp__hive__hive_search_memories" in goal
    assert "(PULL)" in goal
    # No synchronous block, so stats stay at the reset zeros.
    assert last_plan_recall_stats()["recall_calls"] == 0
    assert last_plan_recall_stats()["decisions_reused"] == 0


def test_hybrid_mode_injects_both_block_and_directive(tmp_path):
    # MEMORY_RECALL_MODE=hybrid + hivemind configured => BOTH the synchronous
    # prior-art block (small push floor) AND the pull directive are injected, so the
    # agent gets a cheap seed and may pull more on demand.
    project_dir, specs_dir, spec_id = _make_spec(tmp_path, "hybrid intent")
    block = "- [decision] prefer partial indexes"
    stats = {"recall_calls": 1, "recall_hits": 1, "recall_latency_ms": 2,
             "decisions_reused": 1, "prior_art_tokens": 4}

    saved = _set_recall_env({
        "MEMORY_RECALL_MODE": "hybrid",
        "HIVEMIND_MCP_URL": "http://memory.example.invalid:8000",
        "HIVEMIND_API_KEY": "test-key",
    })
    try:
        with mock.patch.object(
            memory_hook, "plan_prior_art_block", return_value=(block, stats)
        ) as stub:
            goal = build_phase_goal(project_dir, specs_dir, spec_id, "plan", None)
    finally:
        _restore_recall_env(saved)

    stub.assert_called_once()  # hybrid DOES call the synchronous hook (the push floor)
    assert "=== PRIOR ART / KNOWN PITFALLS ===" in goal   # the injected block
    assert "prefer partial indexes" in goal
    assert "mcp__hive__hive_search_memories" in goal       # AND the pull directive
    assert "(PULL)" in goal
    assert last_plan_recall_stats()["decisions_reused"] == 1


def test_off_mode_no_hivemind_injects_nothing(tmp_path):
    # No hivemind endpoint => off, regardless of MEMORY_RECALL_MODE: the hook is
    # not called and neither the push block nor the pull directive is injected.
    project_dir, specs_dir, spec_id = _make_spec(tmp_path, "off intent")

    saved = _set_recall_env({
        "MEMORY_RECALL_MODE": "pull",  # flag set, but no endpoint => forced off
        "HIVEMIND_MCP_URL": None,
        "HIVEMIND_API_KEY": None,
    })
    try:
        with mock.patch.object(memory_hook, "plan_prior_art_block") as stub:
            goal = build_phase_goal(project_dir, specs_dir, spec_id, "plan", None)
    finally:
        _restore_recall_env(saved)

    stub.assert_not_called()
    assert "=== PRIOR ART / KNOWN PITFALLS ===" not in goal
    assert "mcp__hive__hive_search_memories" not in goal
    # The phase still builds a real goal.
    assert "BUILDER AUTONOMOUS PIPELINE SESSION" in goal
    assert last_plan_recall_stats()["recall_calls"] == 0


def test_last_plan_recall_stats_includes_prior_art_tokens_key(tmp_path):
    # Reset (via build) must carry the prior_art_tokens key per the shared contract.
    project_dir, specs_dir, spec_id = _make_spec(tmp_path, "key intent")

    saved = _set_recall_env({
        "MEMORY_RECALL_MODE": "pull",
        "HIVEMIND_MCP_URL": "http://memory.example.invalid:8000",
        "HIVEMIND_API_KEY": "test-key",
    })
    try:
        build_phase_goal(project_dir, specs_dir, spec_id, "plan", None)
    finally:
        _restore_recall_env(saved)

    assert "prior_art_tokens" in last_plan_recall_stats()
    assert last_plan_recall_stats()["prior_art_tokens"] == 0
