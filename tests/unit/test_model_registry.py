"""Tests for model_registry.py — capability-class to model resolution.

The registry is the runtime source of truth; the workflow §4 table is kept in
sync by `lint-builder-assets.py --check-model-registry-drift`. When the pairs
change, update the registry, the §4 table, AND these expectations together.

Claude lane (2026-07): deep_reasoner=Fable 5, reasoning/review=Opus 4.8,
fast_editor=Sonnet 4.6 (never Haiku, all >= high). Codex lane: all classes on
gpt-5.4 (quota consolidation).
"""

from __future__ import annotations

import traceback

from _dispatch_runtime.model_registry import CAPABILITY_MODEL_MAP, resolve_effort, resolve_model

# Allowed Claude-lane model families (full ids, `claude-` prefixed, never haiku).
_ALLOWED_CLAUDE_FAMILIES = ("fable", "opus", "sonnet")


def test_resolve_fast_editor_codex():
    assert resolve_model("fast_editor", "codex-cli") == "gpt-5.6-sol"


def test_resolve_fast_editor_claude():
    assert resolve_model("fast_editor", "claude-code-cli") == "claude-sonnet-5"


def test_resolve_deep_reasoner_codex():
    assert resolve_model("deep_reasoner", "codex-cli") == "gpt-5.6-sol"


def test_resolve_deep_reasoner_claude():
    # 2026-07-22: deep_reasoner (spec/design) runs Opus 4.8 (was Fable 5); spec defects cascade downstream.
    assert resolve_model("deep_reasoner", "claude-code-cli") == "claude-opus-4-8"


def test_resolve_independent_reviewer_codex():
    # The cross-vendor adversarial reviewer moved OFF the quota-consolidated gpt-5.4 (2026-07-13):
    # it is the highest-value verification signal in the pipeline, and consolidation had quietly made
    # it the cheapest model in the set. Requires codex-cli >= 0.144.
    assert resolve_model("independent_reviewer", "codex-cli") == "gpt-5.6-sol"


def test_resolve_independent_reviewer_claude():
    # The verify gate runs Opus 4.8 — the last check before a human sees it.
    assert resolve_model("independent_reviewer", "claude-code-cli") == "claude-opus-4-8"


def test_resolve_structured_planner_codex():
    assert resolve_model("structured_planner", "codex-cli") == "gpt-5.4"


def test_resolve_structured_planner_claude():
    assert resolve_model("structured_planner", "claude-code-cli") == "claude-opus-4-8"


def test_resolve_broad_context_explorer_codex():
    assert resolve_model("broad_context_explorer", "codex-cli") == "gpt-5.4"


def test_resolve_broad_context_explorer_claude():
    assert resolve_model("broad_context_explorer", "claude-code-cli") == "claude-opus-4-8"


def test_resolve_fast_editor_effort():
    assert resolve_effort("fast_editor", "codex-cli") == "medium"
    assert resolve_effort("fast_editor", "claude-code-cli") == "high"


def test_resolve_deep_reasoner_effort():
    # Fable 5 is always-on-thinking; `high` per 4.8/Fable guidance.
    assert resolve_effort("deep_reasoner", "codex-cli") == "high"
    assert resolve_effort("deep_reasoner", "claude-code-cli") == "high"


def test_resolve_independent_reviewer_effort():
    # Verify runs Opus 4.8 @ xhigh (the coding/agentic sweet spot on 4.8).
    # The codex reviewer runs sol @ high (was medium, a quota-fit): depth is the entire point of an
    # adversarial reviewer.
    assert resolve_effort("independent_reviewer", "codex-cli") == "high"
    assert resolve_effort("independent_reviewer", "claude-code-cli") == "xhigh"


def test_claude_lane_effort_is_at_least_high():
    # Requirement: every Claude-lane class runs at >= high reasoning, never lower.
    rank = {"low": 0, "medium": 1, "high": 2, "xhigh": 3, "max": 4}
    for cap_class in CAPABILITY_MODEL_MAP:
        effort = resolve_effort(cap_class, "claude-code-cli")
        assert rank[effort] >= rank["high"], f"{cap_class}: claude effort below high: {effort}"


def test_claude_lane_uses_only_allowed_families():
    for cap_class, providers in CAPABILITY_MODEL_MAP.items():
        claude_model = providers["claude-code-cli"]
        assert claude_model.startswith("claude-"), \
            f"{cap_class}: claude slot is not a claude-* id: {claude_model}"
        assert any(fam in claude_model for fam in _ALLOWED_CLAUDE_FAMILIES), \
            f"{cap_class}: claude slot not in {_ALLOWED_CLAUDE_FAMILIES}: {claude_model}"
        assert "haiku" not in claude_model, f"{cap_class}: haiku is not allowed: {claude_model}"


def test_unknown_class_returns_none():
    assert resolve_model("none_such", "codex-cli") is None


def test_unknown_provider_returns_none():
    assert resolve_model("fast_editor", "unknown-provider") is None


def test_empty_class_returns_none():
    assert resolve_model("", "codex-cli") is None


def test_no_cross_provider_contamination():
    for cap_class, providers in CAPABILITY_MODEL_MAP.items():
        codex_model = providers.get("codex-cli", "")
        claude_model = providers.get("claude-code-cli", "")
        assert not codex_model.startswith("claude"), f"{cap_class}: codex slot has claude model: {codex_model}"
        assert not claude_model.startswith("gpt"), f"{cap_class}: claude slot has gpt model: {claude_model}"
        # codex lane uses gpt-series models
        assert "gpt" in codex_model, f"{cap_class}: codex model does not look like a gpt model: {codex_model}"
        # claude lane uses claude-* models — fable/opus/sonnet only, never haiku
        assert claude_model.startswith("claude-"), \
            f"{cap_class}: claude model is not a claude-* id: {claude_model}"
        assert any(fam in claude_model for fam in _ALLOWED_CLAUDE_FAMILIES), \
            f"{cap_class}: claude model not in {_ALLOWED_CLAUDE_FAMILIES}: {claude_model}"


def test_all_capability_classes_covered():
    expected = {"deep_reasoner", "independent_reviewer", "structured_planner", "fast_editor", "broad_context_explorer"}
    assert set(CAPABILITY_MODEL_MAP.keys()) == expected


def test_all_classes_have_both_providers():
    for cap_class, providers in CAPABILITY_MODEL_MAP.items():
        assert "codex-cli" in providers, f"{cap_class} missing codex-cli entry"
        assert "claude-code-cli" in providers, f"{cap_class} missing claude-code-cli entry"


def test_registry_is_not_mutated_by_resolution():
    original_keys = set(CAPABILITY_MODEL_MAP.keys())
    resolve_model("fast_editor", "codex-cli")
    resolve_model("unknown", "unknown")
    assert set(CAPABILITY_MODEL_MAP.keys()) == original_keys


if __name__ == "__main__":
    tests = [(k, v) for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = 0
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  PASS  {name}")
            passed += 1
        except Exception as exc:
            print(f"  FAIL  {name}: {exc}")
            traceback.print_exc()
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    if failed:
        raise SystemExit(1)
