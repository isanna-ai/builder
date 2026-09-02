"""Capability-class to provider-specific model registry.

Single source of truth for capability-class to model pairing. Lane adapters
resolve from here; model ids are never hard-coded at call sites.
"""

from __future__ import annotations

# The active implementation path runs the claude-code-cli lane (Fable 5 / Opus 4.8 /
# Sonnet 4.6 — never Haiku). The claude lane resolves its model from the
# claude-code-cli column below (lane_claude_code_cli reads resolve_model), so these
# strings are LIVE: full model ids passed straight to `claude --model`. The codex-cli
# lane is an ACTIVE author/implement/verify lane -- `isanna init` generates
# pipeline.default_lane: codex, so a freshly-wired repo authors there; set it to claude to
# author on the claude lane instead. Most codex-cli classes are consolidated onto one model;
# independent_reviewer is deliberately not (see below).
CAPABILITY_MODEL_MAP: dict[str, dict[str, str]] = {
    # deep_reasoner (spec / design) runs on Fable 5 — the most capable model — because
    # spec defects cascade through plan -> implement -> verify. Falls back to Opus 4.8
    # (CAPABILITY_MODEL_FALLBACKS) when Fable is unavailable on the subscription or a
    # request is refused. Effort stays `high` (below): Fable is always-on-thinking.
    "deep_reasoner":          {"codex-cli": "gpt-5.6-sol", "claude-code-cli": "claude-opus-4-8"},
    # independent_reviewer = the verify gate (claude lane) + the two review phases
    # (codex lane). Verify runs Opus 4.8 @ xhigh — the last check before a human sees it.
    # The codex reviewer is deliberately NOT consolidated down with the other classes. This is the
    # cross-vendor adversarial reviewer -- the single highest-value signal in the pipeline -- and
    # consolidating it once quietly made it the cheapest model in the fleet. On the gate-evidence
    # change it caught a fail-open two other models missed, and drove five review rounds that
    # surfaced a hang, a double-execution and an fd leak, none of which a green suite caught.
    # Requires codex-cli >= 0.144.
    "independent_reviewer":   {"codex-cli": "gpt-5.6-sol", "claude-code-cli": "claude-opus-4-8"},
    "structured_planner":     {"codex-cli": "gpt-5.4", "claude-code-cli": "claude-opus-4-8"},
    "fast_editor":            {"codex-cli": "gpt-5.6-sol", "claude-code-cli": "claude-sonnet-5"},
    "broad_context_explorer": {"codex-cli": "gpt-5.4", "claude-code-cli": "claude-opus-4-8"},
}

# Capability-class to per-lane reasoning/effort level.
# Claude Code uses: low, medium, high, xhigh, max.
# Codex CLI uses: minimal, low, medium, high.
CAPABILITY_EFFORT_MAP: dict[str, dict[str, str]] = {
    "deep_reasoner":          {"codex-cli": "high",   "claude-code-cli": "high"},   # Fable 5 (always-on thinking) — `high` per 4.8/Fable guidance
    "independent_reviewer":   {"codex-cli": "high", "claude-code-cli": "xhigh"},  # verify: Opus 4.8, max->xhigh 2026-07 (xhigh = the coding/agentic sweet spot on 4.8); codex reviewer = sol @ high 2026-07-13 (medium was quota-fit; depth is the whole point of the adversarial reviewer)
    "structured_planner":     {"codex-cli": "medium", "claude-code-cli": "high"},
    "fast_editor":            {"codex-cli": "medium", "claude-code-cli": "high"},  # implement runs at medium so the saved budget goes to review, where it buys more
    "broad_context_explorer": {"codex-cli": "medium", "claude-code-cli": "xhigh"},
}

# Per-lane, ordered model fallbacks tried when the primary model's turn comes back as
# a hard `failed` (model unavailable on the subscription, or a safety refusal). This
# is NOT for transient statuses — rate_limited / session_expired / timed_out are
# retried on the SAME model by the scheduler's retry_policy. spec falls Fable 5 ->
# Opus 4.8 so the phase still lands when Fable can't serve it.
CAPABILITY_MODEL_FALLBACKS: dict[str, dict[str, list[str]]] = {
    # deep_reasoner's claude primary is Opus; a hard failure falls back to Sonnet.
    "deep_reasoner": {"claude-code-cli": ["claude-sonnet-5"]},
    "fast_editor": {"claude-code-cli": ["claude-sonnet-4-6"]},
}


def resolve_model(capability_class: str, lane_provider: str) -> str | None:
    """Return the concrete model for this capability class + lane provider, or None."""
    return CAPABILITY_MODEL_MAP.get(capability_class, {}).get(lane_provider)


def resolve_effort(capability_class: str, lane_provider: str) -> str | None:
    """Return the concrete reasoning/effort level for this capability + lane, or None."""
    return CAPABILITY_EFFORT_MAP.get(capability_class, {}).get(lane_provider)


def resolve_model_fallbacks(capability_class: str, lane_provider: str) -> list[str]:
    """Ordered fallback model ids for this capability + lane (empty if none)."""
    return list(CAPABILITY_MODEL_FALLBACKS.get(capability_class, {}).get(lane_provider, []))
