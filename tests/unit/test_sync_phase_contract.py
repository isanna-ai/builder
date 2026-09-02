from __future__ import annotations

from _dispatch_runtime.phase_runtime import PHASE_META, SPEC_PHASE_ORDER, next_phase


def test_sync_phase_is_part_of_the_spec_order():
    assert SPEC_PHASE_ORDER[-1] == "sync"
    assert next_phase("verify", order=SPEC_PHASE_ORDER) == "sync"


def test_verify_advances_into_syncing_and_sync_finishes_synced():
    assert PHASE_META["verify"]["status"] == "syncing"
    assert PHASE_META["sync"]["status"] == "synced"
