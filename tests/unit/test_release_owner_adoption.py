"""Release-level owner-adoption precondition (planning.adoption_satisfied).

Adoption lets a release count an accepted intent fulfilled when its member specs are host-verified +
merged to main but their spec-level sync bookkeeping can't be reconstructed (already-merged work) --
WITHOUT faking spec-level sync artifacts. The precondition must refuse to adopt un-done work.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import planning


class _M:
    def __init__(self, verification):
        self.verification = verification


def test_adopts_when_declared_and_all_members_host_verified():
    members = [_M("host-verified"), _M("synced")]
    assert planning.adoption_satisfied("i1", ("i1",), members, has_findings=False, terminal_reference=False)


def test_refuses_when_not_declared_adopted():
    members = [_M("host-verified")]
    assert not planning.adoption_satisfied("i1", (), members, has_findings=False, terminal_reference=False)


def test_refuses_undone_work_a_member_not_host_verified():
    # the honesty anchor: one implementing/unknown member blocks adoption
    for bad in ("unknown", "self-reported", "", None):
        members = [_M("host-verified"), _M(bad)]
        assert not planning.adoption_satisfied(
            "i1", ("i1",), members, has_findings=False, terminal_reference=False
        ), f"must refuse adoption with a {bad!r} member"


def test_refuses_on_findings_or_terminal_reference():
    members = [_M("host-verified")]
    assert not planning.adoption_satisfied("i1", ("i1",), members, has_findings=True, terminal_reference=False)
    assert not planning.adoption_satisfied("i1", ("i1",), members, has_findings=False, terminal_reference=True)


def test_refuses_empty_membership():
    assert not planning.adoption_satisfied("i1", ("i1",), [], has_findings=False, terminal_reference=False)
