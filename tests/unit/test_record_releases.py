"""The Record's Releases surface preserves the host/agent provenance boundary."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"


def _load_record():
    spec = importlib.util.spec_from_file_location("record_releases_under_test", SCRIPTS / "record.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


record = _load_record()

IMPLEMENT_GATES = {
    "host_verify": "pass", "source_diff": "pass",
    "red_baseline": "abstain:non_gated_phase", "packet_contract": "abstain:off",
}
VERIFY_GATES = {
    "host_verify": "pass", "source_diff": "abstain:non_gated_phase",
    "red_baseline": "abstain:non_gated_phase", "packet_contract": "abstain:off",
}


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "release-demo"
    (root / ".builder" / "specs").mkdir(parents=True)
    _write(root / ".builder" / "dispatch.yaml", {"queue_store": {"path": ".builder/dispatch-queue"}})
    return root


def _spec(root: Path, spec_id: str, status: str) -> Path:
    spec = root / ".builder" / "specs" / spec_id
    _write(spec / "spec.yaml", {"id": spec_id, "name": f"{spec_id} title", "status": status})
    return spec


def _attempt(root: Path, spec_id: str, phase: str, gates: dict) -> None:
    attempts = root / ".builder" / "dispatch-queue" / "queue" / "attempts"
    _write(
        attempts / f"{phase}-{spec_id}.yaml",
        {
            "attempt_id": f"{phase}-{spec_id}",
            "created_at": "2026-07-14T00:00:00Z",
            "metadata": {
                "spec_id": spec_id,
                "phase": phase,
                "decision": "phase-complete",
                "reason": "outcome: SUCCEEDED",
                "started_at": "2026-07-14T00:00:00Z",
                "gates": gates,
            },
        },
    )


def test_releases_surface_counts_only_host_verified_members(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _write(root / ".builder" / "product.yaml", {"product": "release-demo", "repos": [{"alias": "release-demo"}]})
    _write(
        root / ".builder" / "releases" / "f2.yaml",
        {"release": "f2", "product": "release-demo", "title": "F2 Releases", "status": "shipped",
         "specs": ["verified", "planned", "ghost"]},
    )
    _spec(root, "verified", "verified")
    _spec(root, "planned", "planned")
    # A host gate record is the source of the numerator; the agent's `status: verified` is not.
    _attempt(root, "verified", "implement", IMPLEMENT_GATES)
    _attempt(root, "verified", "verify", VERIFY_GATES)
    gate_row = next(row for row in record.gc.scan_repo(root)["specs"] if row["spec"] == "verified")
    assert gate_row["verification"] == "host-verified"
    # Drive completeness through planning's REAL scan (the same scan_repo above) — no hand-seeded
    # cache. This is what production runs; a prior seed used a `spec_id` key the real scan never
    # emits, so it masked a lookup bug that made every host-verified member read as unknown.
    record.planning._scan_cache.clear()
    release = record.planning.load_releases(root)[0]
    comp = record.planning.completeness(release, record.planning._registry(root, None))
    assert comp.fraction == "1/3", [(m.ref.canonical, m.verification, m.resolved, m.error) for m in comp.members]

    out = tmp_path / "out"
    assert record.main(["build", "--root", str(root), "--out", str(out)]) == 0
    html = (out / root.name / "releases.html").read_text(encoding="utf-8")

    assert "1/3 · 33% done" in html
    assert "[host-verified 1 · planned 1 · self-reported 0 · unknown 1]" in html
    assert "intentional roadmap member" in html
    assert '<span class="chip claimed">planned</span>' in html
    assert "BROKEN RELEASE REF" in html and "ghost" in html
    assert '<div class="host-seal host-ok"><span class="stamp">host-verified</span>' in html
    # Exactly two host-success registers may render: the release completeness seal and the one
    # host-verified member. A planned member is agent intent and must never acquire a host seal.
    assert html.count('<div class="host-seal host-ok">') == 2
    assert '<a href="releases.html">Releases</a>' in (out / root.name / "roadmap.html").read_text(encoding="utf-8")
    assert "2/3" not in html, "agent-authored planned status must never enter the host numerator"


def test_resolved_but_unverified_member_is_unknown_not_broken(tmp_path: Path) -> None:
    """A release member whose spec dir EXISTS but has no host-verification is an honest 'unknown',
    NOT a BROKEN RELEASE REF. Only a genuinely dangling ref (missing spec dir) is broken. Regression:
    the repo-level Releases surface used to mislabel every not-yet-host-verified member as broken."""
    root = _repo(tmp_path)
    _write(root / ".builder" / "product.yaml", {"product": "release-demo", "repos": [{"alias": "release-demo"}]})
    _write(
        root / ".builder" / "releases" / "r.yaml",
        {"release": "r", "product": "release-demo", "title": "R", "status": "shipped",
         "specs": ["exists-unverified", "ghost"]},
    )
    _spec(root, "exists-unverified", "implementing")  # resolves, but no host gate evidence -> unknown
    # (ghost is intentionally NOT created -> a genuinely dangling ref)
    out = tmp_path / "out"
    assert record.main(["build", "--root", str(root), "--out", str(out)]) == 0
    html = (out / root.name / "releases.html").read_text(encoding="utf-8")
    assert "exists-unverified" in html
    assert "not host-verified" in html, "a resolved-but-unstamped member renders as an honest unknown card"
    # BROKEN RELEASE REF appears for the genuinely dangling ghost ref ONLY, not the resolved member.
    assert html.count("BROKEN RELEASE REF") == 1, "only the missing-spec ref is broken, not the resolved one"
    assert "ghost" in html


def test_no_release_files_produce_no_releases_page(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _spec(root, "only-spec", "planned")

    out = tmp_path / "out"
    assert record.main(["build", "--root", str(root), "--out", str(out)]) == 0
    assert not (out / root.name / "releases.html").exists()
    assert "releases.html" not in (out / root.name / "roadmap.html").read_text(encoding="utf-8")


def test_host_verified_member_counts_in_the_numerator_via_the_real_scan(tmp_path: Path) -> None:
    """Regression guard for the numerator that always read 0.

    gate-coverage's scan_repo keys each row by `spec`, but planning._spec_verification looked rows up
    by `spec_id` — a key the real scan never emits — so EVERY genuinely host-verified member fell
    through to `unknown` and `% done` was stuck at 0 for every release in production. The pre-existing
    tests missed it because they seeded the scan cache with a `spec_id` key that matched the bug.

    This drives the REAL scan (real gate records, NO cache seed): a host-verified member MUST count."""
    record.planning._scan_cache.clear()
    root = _repo(tmp_path)
    _write(root / ".builder" / "product.yaml",
           {"product": "release-demo", "repos": [{"alias": "release-demo"}]})
    _write(root / ".builder" / "releases" / "r.yaml",
           {"release": "r", "product": "release-demo", "title": "R", "status": "shipped",
            "specs": ["shipped", "planned-one"]})
    _spec(root, "shipped", "verified")
    _spec(root, "planned-one", "planned")
    # Real host gate records — the same shape the dispatcher writes — make `shipped` host-verified.
    _attempt(root, "shipped", "implement", IMPLEMENT_GATES)
    _attempt(root, "shipped", "verify", VERIFY_GATES)
    # Confirm the real scan (production's source of truth) does stamp it host-verified under `spec`.
    scan_row = next(r for r in record.gc.scan_repo(root)["specs"] if r["spec"] == "shipped")
    assert scan_row["verification"] == "host-verified"

    record.planning._scan_cache.clear()  # force completeness to run the real scan, not a seed
    comp = record.planning.completeness(
        record.planning.load_releases(root)[0], record.planning._registry(root, None))
    members = {m.ref.canonical: m.verification for m in comp.members}
    assert members["shipped"] == "host-verified", members
    assert members["planned-one"] == "planned", members
    assert comp.fraction == "1/2" and comp.percent == 50, (comp.fraction, comp.percent)
