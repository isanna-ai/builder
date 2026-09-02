from pathlib import Path
from unittest import SkipTest

import planning
from tests.unit.public_export_support import require_live_spec_corpus


def test_live_release_completeness_counts_fulfilled_intents():
    # Live-portfolio smoke test: it asserts the REAL builder-project-model completeness on the main
    # checkout. Inside a dispatch worktree a member spec is mid-pipeline, so intent state can be
    # transiently inconsistent — skip there (the dispatch gate must not depend on the live portfolio
    # state it is itself mutating).
    here = Path(__file__).resolve()
    if ".builder/worktrees/" in str(here):
        raise SkipTest("live-portfolio smoke test — main checkout only, not dispatch worktrees")
    root = here.parents[2]
    require_live_spec_corpus(root, "the live release-completeness tally")
    registry = planning._registry(root, projects_root=None)
    release = planning.find_release(root, "builder-project-model")
    assert release is not None
    if not planning.release_uses_intents(release.status):
        raise SkipTest(
            "builder-project-model is shipped (spec-based); the intent-count smoke test needs an "
            "intent-based release. Skipped rather than asserting a stale 3-intent shape."
        )
    comp = planning.completeness(release, registry)
    # Assert the counting CONTRACT, not a frozen progress tally: as the 3 intents advance from
    # in-flight -> fulfilled the raw verified/in-flight counts legitimately move, so a hard-coded
    # snapshot (the old `verified == 0` / `in-flight == 3`) self-invalidates the moment the roadmap it
    # measures starts completing. These invariants still catch real regressions in intent counting.
    assert comp.total == 3                                            # release membership is exactly 3 intents
    assert comp.dangling == 0                                         # no missing / rejected / superseded intent refs
    assert sum(comp.claimed_states.values()) == comp.total            # every intent lands in exactly one visible state
    assert comp.verified == comp.claimed_states.get("fulfilled", 0)   # verified numerator == count of fulfilled intents
    assert 0 <= comp.verified <= comp.total
    assert comp.percent == round(100 * comp.verified / comp.total)

