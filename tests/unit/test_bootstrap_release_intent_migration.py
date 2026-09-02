from pathlib import Path

import planning
from tests.unit.public_export_support import require_live_spec_corpus


ROOT = Path(__file__).resolve().parents[2]


def _flatten(release_id: str) -> list[str]:
    release = planning.find_release(ROOT, release_id)
    assert release is not None
    inventory, _ = planning.intent_inventory(ROOT)
    by_id = {item.intent.intent: item.intent for item in inventory if item.intent is not None}
    return [member for intent_id in release.intents for member in by_id[intent_id].specs]


def test_bootstrap_and_operability_migrations_preserve_exact_order_once():
    require_live_spec_corpus(ROOT, "the bootstrap/operability intent-migration order invariant")
    expected = {
        "builder-intent-layer": [
            "intent-object-and-backlog",
            "intent-release-membership-cutover",
            "sync-phase-and-blocking-amendment",
            "backlog-collision-lint",
        ],
        "builder-operability": ["live-central-daemon-and-cutover"],
    }
    for release_id, original in expected.items():
        release = planning.find_release(ROOT, release_id)
        assert release is not None
        if not planning.release_uses_intents(release.status):
            # Same pattern as the sibling intent-migration invariants (commit 4d29712):
            # once a migrated release ships via owner-adoption, release.intents is empty
            # (historical/spec-mode) and the flatten-order invariant no longer has an
            # intent-based population to check. Skip it, keep asserting the unshipped ones.
            continue
        flattened = _flatten(release_id)
        assert flattened == original
        assert len(flattened) == len(set(flattened))


def test_zero_member_accepted_intent_is_never_vacuously_fulfilled(tmp_path):
    root = tmp_path / "repo"
    intent = root / ".builder" / "intents" / "empty" / "intent.yaml"
    intent.parent.mkdir(parents=True)
    intent.write_text(
        "artifact: intent-object\nintent: empty\ntitle: Empty\nstatus: accepted\nproblem: p\nwhy: w\n"
        "success_criteria:\n  - id: sc-1\n    statement: s\nnon_goals:\n  - n\n"
        "ssot_delta:\n  capabilities: []\n  behaviors: []\n  journeys: []\nspecs: []\n",
        encoding="utf-8",
    )
    release_path = root / ".builder" / "releases" / "demo.yaml"
    release_path.parent.mkdir(parents=True)
    release_path.write_text("release: demo\nstatus: draft\nintents:\n  - empty\n", encoding="utf-8")
    comp = planning.completeness(planning.parse_release(release_path, root), planning.Registry(tmp_path, root))
    assert comp.verified == 0
    assert comp.intents[0].visible_state == "accepted"
    assert comp.intents[0].members == []
