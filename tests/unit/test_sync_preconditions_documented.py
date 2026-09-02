"""The adapter's most important behaviour is the counter-intuitive one, and it was undocumented.

`_sync/adapter.py:observed_tuples` turns any changed path matching NO mapping into
`{capabilities, unmapped:<path>, enrich}`, and sync flags every undeclared observed tuple as
divergence. So an incomplete adapter does not make sync weaker — it makes sync FAIL, on files
nobody made a claim about. An empty adapter loads fine, clears `bootstrap_required`, and then
blocks every spec.

Everyone reasons the other way round on first contact. This session did: an earlier analysis
argued `mappings: []` would unblock sync while proving nothing, and proposed it as a safe
brownfield bootstrap. It would have converted a clean `bootstrap_required` refusal into a wall
of divergence failures across 20 repos.

The rule was written down in exactly one place — a header comment inside webapp's own
adapter, visible only to someone already editing that file — while
`standards/builder-contract.md`, the canonical lifecycle document, said nothing about sync
preconditions at all. These are guard tests over prose: nothing executes a standard, so
nothing else would notice if the section were dropped in a later edit.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONTRACT = ROOT / "standards" / "builder-contract.md"


def _contract() -> str:
    assert CONTRACT.is_file(), f"missing {CONTRACT}"
    return CONTRACT.read_text(encoding="utf-8")


def test_the_contract_has_a_sync_preconditions_section():
    assert "## Sync Preconditions" in _contract()


def test_the_unmapped_path_rule_is_stated():
    text = _contract()
    assert "unmapped:" in text, "the unmapped-path tuple shape must be spelled out"
    assert "divergence" in text.lower()


def test_the_contract_says_an_incomplete_adapter_blocks_rather_than_weakens():
    # The whole point. If this survives only as "the adapter should be complete", the next
    # author will reasonably assume an incomplete one merely observes less.
    lowered = _contract().lower()
    assert "blocks" in lowered or "block" in lowered
    assert "empty adapter" in lowered


def test_the_contract_names_both_repo_level_preconditions():
    text = _contract()
    assert ".builder/sync-adapter.yaml" in text
    assert "docs/system-behaviors.yaml" in text
    assert "bootstrap_required" in text


def test_the_contract_documents_the_no_claim_escape_hatch():
    # Without `tuples: []` an author has no way to cover a shared path except by inventing a
    # claim about it — which is how blanket mappings and stale tuples get written.
    text = _contract()
    assert "tuples: []" in text


def test_the_contract_records_the_forward_only_boundary():
    # Without this written down, a repo's permanently-unsyncable specs read as an oversight
    # to someone arriving later, and the obvious "fix" is to widen
    # readmission's provenance exception — which would hollow out what host-verified means.
    text = _contract()
    assert "forward only" in text.lower()
    assert "unsafe-evidence-directory" in text
    assert "gate-evidence" in text


def test_the_contract_says_curation_is_necessary_but_not_sufficient():
    # The trap: finish every adapter, expect syncs, get none, conclude the tooling is broken.
    lowered = _contract().lower()
    assert "not sufficient" in lowered


def test_the_contract_states_that_bootstrapping_alone_yields_no_syncs():
    # Measured: webapp is fully bootstrapped and had 1 ssot-delta across 49 specs.
    assert "ssot-delta.yaml" in _contract()


def test_the_contract_records_that_the_delta_is_enforced_at_advancement():
    # Without this, the next reader assumes validate-spec is the only enforcement — which is
    # exactly the assumption that let specs reach `planned` with no delta and never sync.
    text = _contract()
    assert "BUILDER_REQUIRE_SSOT_DELTA" in text
    assert "advancement" in text.lower()


def test_the_contract_documents_per_repo_archive_enforcement():
    # Without the resolution order written down, an operator who sets the repo key and still
    # has a stale env export will conclude the repo key does not work.
    text = _contract()
    assert "archive_require_sync" in text
    assert "BUILDER_ARCHIVE_REQUIRE_SYNC" in text


def test_the_contract_documents_both_gates_as_per_repo_keys():
    # An operator who sets one key and not the other, or who assumes require_ssot_delta is
    # env-only, gets a repo that enforces archiving but not advancement.
    text = _contract()
    assert "require_ssot_delta" in text
    assert "archive_require_sync" in text
    assert "staged_gate" in text


def test_the_contract_warns_that_enforce_blocks_re_advancement():
    # The non-obvious part: finished specs are fine where they stand but cannot be re-advanced.
    lowered = _contract().lower()
    assert "re-advanced" in lowered or "re-advance" in lowered
