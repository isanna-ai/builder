"""Phase -> {capability class, lane, model} routing.

Separates WHO runs a phase (lane: claude|codex) from HOW HARD it is (capability
class). The dispatcher routes deep reasoning to opus and mechanical /
high-volume edits to sonnet. A configured review lane can differ from the
author lane; a fallback lane must not be described as model-independent.

Lane selection matches by substring so it works whether lanes are named
"claude"/"codex" or "claude-code-cli"/"codex-cli".
"""

from __future__ import annotations

from _dispatch_runtime.phase_runtime import normalize_phase

# Phase -> capability class (the canonical Builder mapping).
PHASE_CLASS_MAP = {
    # reworked 4-phase pipeline
    "spec": "deep_reasoner",
    "plan": "structured_planner",
    "implement": "fast_editor",
    "verify": "independent_reviewer",
    "sync": "deep_reasoner",
    # opt-in review-augmented pipeline. The two review phases are independent_reviewer
    # (-> gpt-5.4 on the codex lane); the fix pass is a fast_editor on the author lane.
    "spec-review": "independent_reviewer",
    "spec-review-2": "independent_reviewer",
    "adversarial-review": "independent_reviewer",
    "adversarial-review-2": "independent_reviewer",
    "review-fix": "fast_editor",
    # legacy 7-phase
    "1-specify": "deep_reasoner",
    "2-design": "deep_reasoner",
    "3-review": "independent_reviewer",
    "4-plan": "structured_planner",
    "5-implement": "fast_editor",
    "6-verify": "independent_reviewer",
    "7-archive": "fast_editor",
}

_DEEP = {"deep_reasoner", "broad_context_explorer"}
_REVIEWER = {"independent_reviewer"}

# Phases that must run on the REVIEW lane (codex/gpt-5.4) when reviews are enabled,
# when configured. The fix + verify phases stay on the author lane. The scheduler
# consults this for per-phase lane routing.
REVIEW_LANE_PHASES = frozenset({
    "spec-review", "spec-review-2", "adversarial-review", "adversarial-review-2",
})


def capability_for_phase(phase: str, override: str | None = None) -> str:
    if override:
        return str(override)
    return PHASE_CLASS_MAP.get(normalize_phase(phase) or "", "structured_planner")


# Claude lane model split: sonnet ONLY for the
# code-editing capability (implement / review-fix = fast_editor); opus for every
# reasoning phase (spec / plan / verify, plus reviews when they land on the claude
# lane). Keep in sync with phase_runtime.model_for_phase.
_CLAUDE_SONNET_CAPABILITIES = {"fast_editor"}


def claude_model_for(capability: str) -> str:
    """Claude --model alias: sonnet for code-editing (fast_editor), opus otherwise."""
    return "sonnet" if capability in _CLAUDE_SONNET_CAPABILITIES else "opus"


def _pick(lanes: list[str], want: str) -> str | None:
    for lane in lanes:
        if want in lane.lower():
            return lane
    return None


def route_lane(
    phase: str,
    available_lanes,
    *,
    requested_lane: str | None = None,
    default_lane: str = "claude",
    **_ignored,
) -> str:
    """Lane selection — LOCKED to `default_lane` (claude) unless a lane is
    explicitly requested. Codex is used only on an explicit request (e.g.
    `--lane codex`), which then carries through the spec via the scheduler's
    carry-forward (requested_lane=item.lane). The per-phase *model* is still
    chosen by the lanes (claude_model_for / model_registry); this only picks the
    provider lane. `phase` is kept for signature stability / future policy.
    """
    lanes = [str(l) for l in (available_lanes or [])] or ["claude"]
    want = requested_lane or default_lane
    return _pick(lanes, want) or lanes[0]
