from __future__ import annotations

import record


def test_record_buckets_include_synced_status():
    labels = {label for label, _ in record.BUCKETS}
    assert "Synced" in labels
    synced_statuses = next(statuses for label, statuses in record.BUCKETS if label == "Synced")
    assert "synced" in synced_statuses
    awaiting = next(statuses for label, statuses in record.BUCKETS if label == "Awaiting sync")
    assert "verified-awaiting-sync" in awaiting
