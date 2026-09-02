from __future__ import annotations

from pathlib import Path
from unittest import SkipTest

from _validators.common import ValidationContext
from _validators.sync_artifacts import run_ssot_delta

_REPO_ROOT = Path(__file__).resolve().parents[2]


def test_ssot_delta_accepts_canonical_shape_for_sync_spec():
    # Repo-root-relative + skip-if-absent: the sync-phase spec's provenance is local (uncommitted)
    # state, absent in a fresh clone / dispatch worktree.
    spec_dir = _REPO_ROOT / ".builder/specs/sync-phase-and-blocking-amendment"
    if not spec_dir.is_dir():
        raise SkipTest("sync-phase-and-blocking-amendment spec dir absent in this checkout")
    result = run_ssot_delta(ValidationContext(spec_dir=spec_dir))
    assert result.errors == []


def test_ssot_delta_rejects_unknown_change_and_out_of_intent_envelope():
    spec_dir = _REPO_ROOT / "tests/fixtures/bad-ssot-delta/.builder/specs/bad-ssot-delta"
    result = run_ssot_delta(ValidationContext(spec_dir=spec_dir))
    assert any("expected one of" in err or "invalid" in err for err in result.errors)
