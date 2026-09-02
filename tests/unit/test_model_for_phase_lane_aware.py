"""model_for_phase must record the REAL model per lane, not a lane-blind claude alias.

Bug: the phase-log used_model + goal-header model came from a claude-only sonnet/opus alias, so a
codex-lane turn (which actually runs gpt-5.6-*) recorded used_model: opus. Fixed to resolve the concrete
registry model for the phase's capability class on the actual lane provider.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from _dispatch_runtime.model_registry import resolve_model
from _dispatch_runtime.phase_runtime import model_for_phase
from _dispatch_runtime.phase_routing import capability_for_phase


def test_codex_lane_records_real_codex_model_not_claude_alias():
    # verify -> independent_reviewer -> gpt-5.6-sol on codex; NOT "opus"
    assert model_for_phase("verify", "codex-cli") == resolve_model("independent_reviewer", "codex-cli")
    assert model_for_phase("verify", "codex-cli") != "opus"
    # implement -> fast_editor -> gpt-5.6-sol on codex; NOT "sonnet"
    assert model_for_phase("implement", "codex-cli") == resolve_model("fast_editor", "codex-cli")
    assert model_for_phase("implement", "codex-cli") != "sonnet"


def test_claude_lane_records_concrete_claude_model():
    assert model_for_phase("verify", "claude-code-cli") == "claude-opus-4-8"
    assert model_for_phase("implement", "claude-code-cli") == "claude-sonnet-5"


def test_matches_what_each_lane_would_actually_run():
    for phase in ("spec", "plan", "implement", "verify", "sync", "adversarial-review"):
        for provider in ("codex-cli", "claude-code-cli"):
            assert model_for_phase(phase, provider) == resolve_model(capability_for_phase(phase), provider)


def test_no_provider_falls_back_to_claude_alias():
    # backward-compatible: goal-header hint when the lane provider is unknown
    assert model_for_phase("verify") == "opus"
    assert model_for_phase("implement") == "sonnet"
