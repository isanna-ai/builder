from __future__ import annotations

import planning


def test_segments_use_sync_era_buckets():
    comp = planning.Completeness(
        "r",
        members=[
            planning.MemberStatus(planning.SpecRef(None, "a"), "synced", True),
            planning.MemberStatus(planning.SpecRef(None, "b"), "verified-awaiting-sync", True),
            planning.MemberStatus(planning.SpecRef(None, "c"), "planned-decomposing", True),
            planning.MemberStatus(planning.SpecRef(None, "d"), "self-reported", True),
            planning.MemberStatus(planning.SpecRef(None, "e"), "unknown", False),
        ],
        verified=1,
        total=5,
        dangling=1,
        planned=1,
    )
    assert "verified-awaiting-sync" in planning._segments(comp)
