from __future__ import annotations

import isanna


def test_sync_readmission_is_additive_to_the_cli_lifecycle():
    parser = isanna.build_parser()
    assert parser is not None
    verbs = parser._subparsers._group_actions[0].choices
    assert "sync-readmit" in verbs
    assert "sync" in verbs and "dispatch" in verbs and "verify" in verbs
