from __future__ import annotations

from pathlib import Path

import planning
from tests.unit.sync_evidence_support import write_host_scope, write_sync_result


def test_verified_awaiting_sync_does_not_enter_the_numerator(tmp_path: Path):
    repo = tmp_path / "repo"
    (repo / ".builder" / "releases").mkdir(parents=True)
    (repo / ".builder" / "specs" / "demo").mkdir(parents=True)
    (repo / ".builder" / "specs" / "demo" / "spec.yaml").write_text(
        "status: verified\ncurrent_phase: sync\n", encoding="utf-8"
    )
    spec_dir = repo / ".builder" / "specs" / "demo"
    spec_dir.joinpath("ssot-delta.yaml").write_text("capabilities: []\nbehaviors: []\njourneys: []\n", encoding="utf-8")
    scope = write_host_scope(repo, "demo")
    write_sync_result(spec_dir, scope, "divergence", undeclared=[
        {"category": "capabilities", "target": "outside", "change": "enrich"}
    ])
    (repo / ".builder" / "releases" / "r.yaml").write_text(
        "release: r\nproduct: repo\ntitle: R\nstatus: archived\nspecs:\n  - demo\n", encoding="utf-8"
    )
    planning._scan_cache[str(repo.resolve())] = {"specs": [{"spec": "demo", "verification": "host-verified"}]}
    comp = planning.completeness(planning.load_releases(repo)[0], planning.Registry(tmp_path, repo))
    assert comp.verified == 0
    assert comp.members and comp.members[0].verification == "verified-awaiting-sync"
