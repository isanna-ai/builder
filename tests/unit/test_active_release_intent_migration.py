from pathlib import Path

from unittest import SkipTest

import planning
from tests.unit.public_export_support import require_live_spec_corpus


ROOT = Path(__file__).resolve().parents[2]


def test_active_release_migrations_preserve_exact_once_order_and_identity():
    require_live_spec_corpus(ROOT, "the active-release intent-migration order invariant")
    expected = {
        "builder-behavioral-ssot": [
            "ssot-builder-home",
            "ssot-governor-sessions",
            "ssot-scheduler-draining",
            "ssot-authoring-cutover",
        ],
    }
    inventory, _ = planning.intent_inventory(ROOT)
    by_id = {item.intent.intent: item.intent for item in inventory if item.intent is not None}
    for release_id, original in expected.items():
        release = planning.find_release(ROOT, release_id)
        assert release is not None
        if not planning.release_uses_intents(release.status):
            raise SkipTest(
                f"{release_id} is shipped (historical/spec-based); the intent-migration flatten "
                "invariant applies only while a migrated release is unshipped/intent-based."
            )
        flattened = [member for intent_id in release.intents for member in by_id[intent_id].specs]
        assert flattened == original
        assert len(flattened) == len(set(flattened))
        parsed = [planning.parse_spec_ref(member)[0].canonical for member in flattened]
        assert parsed == original


def test_unshipped_migrated_releases_keep_their_three_intent_owners():
    require_live_spec_corpus(ROOT, "the three-intent-owners invariant")
    releases = [
        planning.find_release(ROOT, release_id)
        for release_id in ("builder-behavioral-ssot", "builder-operability")
    ]
    assert all(releases)
    if any(not planning.release_uses_intents(r.status) for r in releases):
        raise SkipTest(
            "builder-behavioral-ssot / builder-operability are shipped; the 'unshipped migrated "
            "releases keep their three intent owners' invariant no longer has an unshipped population."
        )
    assert sum(len(release.intents) for release in releases) == 3
