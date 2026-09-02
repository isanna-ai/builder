"""End-to-end proof of the product's founding ask, as a regression guard.

The original request: author "the next roadmap" (a Product -> Release -> set of planned specs)
and SEE IT in The Record exactly as it was initially designed -- the planned specs visible
before any of them is built, with a `% done` that only the HOST can move.

This test drives the REAL user-facing chain through the `isanna` CLI entrypoints:

    isanna release create <id> --specs a,b,c     (the F1 scaffolder)
    isanna record build                          (the F2 Releases surface)

and asserts the roadmap-as-designed shows up. It exists so that a future refactor to EITHER
the scaffolder or the recorder that silently breaks the chain -- or worse, lets an agent-declared
status leak into the host-verified numerator -- fails the gate instead of shipping.

Follows tests/unit/test_isanna.py's discipline: load the CLI as a module, call main() in-process,
no fixtures beyond tmp_path.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


def _load(script: str, name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / script)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


isanna = _load("isanna.py", "isanna_cli_roadmap_e2e")


def _host_attempt(repo: Path, spec_id: str, phase: str, gates: dict[str, str]) -> None:
    path = repo / ".builder" / "dispatch-queue" / "queue" / "attempts" / f"{phase}-{spec_id}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "attempt_id": f"{phase}-{spec_id}",
        "created_at": "2026-07-14T00:00:00Z",
        "metadata": {"spec_id": spec_id, "phase": phase, "decision": "phase-complete",
                     "reason": "outcome: SUCCEEDED", "started_at": "2026-07-14T00:00:00Z",
                     "gates": gates},
    }), encoding="utf-8")


def test_authored_roadmap_is_visible_in_the_record_as_designed(tmp_path):
    repo = tmp_path / "ledger"
    repo.mkdir()
    out = tmp_path / "record-out"

    # 1. Author the roadmap: one command scaffolds product + release + a planned stub per member.
    rc = isanna.main([
        "release", "create", "ledger-v2",
        "--specs", "auth-core,billing,webhooks",
        "--title", "Ledger v2", "--root", str(repo),
    ])
    assert rc == 0
    # The planned stubs exist on disk before anything is built.
    for member in ("auth-core", "billing", "webhooks"):
        assert (repo / ".builder" / "specs" / member / "spec.yaml").is_file()

    # 2. Build The Record over the freshly-authored roadmap (nothing has been implemented yet).
    rc = isanna.main(["record", "build", "--root", str(repo), "--out", str(out)])
    assert rc == 0

    releases_html = out / "ledger" / "releases.html"
    roadmap_html = out / "ledger" / "roadmap.html"
    assert releases_html.is_file(), "the Releases surface must be emitted when a release exists"
    page = releases_html.read_text(encoding="utf-8")

    # --- The roadmap as designed: the live release renders as one intent with its planned members visible up front ---
    for member in ("auth-core", "billing", "webhooks"):
        assert member in page
    assert "Ledger v2" in page
    assert "ledger-v2-intent" in page
    assert "projection: decomposed" in page
    assert page.count("canonical status: planned") == 3

    # --- The trust property: completeness is now intent-based, and agents still cannot inflate it ---
    # Nothing is fulfilled yet, so the live release stays at 0/1 even though three planned member specs exist on disk.
    assert "0/1 · 0% done" in page
    assert "[fulfilled 0 · in-flight 0 · decomposed 1 · accepted 0 · blocked 0]" in page
    assert "1/1 · 100% done" not in page, "agent-authored planned status must never enter the host numerator"

    # --- The two registers stay distinct: the fraction lives in the HOST seal; planned members
    #     carry the AGENT `claimed` chip. Provenance, never content, assigns the register. ---
    assert "host-seal" in page and "host-verified completeness" in page
    assert "canonical status: planned" in page

    # The roadmap page links to the releases surface so the roadmap is reachable from the planner.
    assert '<a href="releases.html">Releases</a>' in roadmap_html.read_text(encoding="utf-8")


def test_authored_roadmap_counts_a_real_host_stamp_not_its_status(tmp_path):
    """The same CLI chain moves only when the host's real scan emits `spec` evidence."""
    repo = tmp_path / "ledger"
    repo.mkdir()
    out = tmp_path / "record-out"
    assert isanna.main(["release", "create", "ledger-v2", "--specs", "auth-core,billing,webhooks",
                        "--root", str(repo)]) == 0
    spec_yaml = repo / ".builder" / "specs" / "auth-core" / "spec.yaml"
    spec_yaml.write_text("id: auth-core\nstatus: verified\n", encoding="utf-8")
    _host_attempt(repo, "auth-core", "implement", {
        "host_verify": "pass", "source_diff": "pass", "red_baseline": "abstain:non_gated_phase",
        "packet_contract": "abstain:off"})
    _host_attempt(repo, "auth-core", "verify", {
        "host_verify": "pass", "source_diff": "abstain:non_gated_phase",
        "red_baseline": "abstain:non_gated_phase", "packet_contract": "abstain:off"})
    assert isanna.main(["record", "build", "--root", str(repo), "--out", str(out)]) == 0
    page = (out / "ledger" / "releases.html").read_text(encoding="utf-8")
    assert "0/1 · 0% done" in page
    assert "[fulfilled 0 · in-flight 1 · decomposed 0 · accepted 0 · blocked 0]" in page
    assert "projection: in-flight" in page
