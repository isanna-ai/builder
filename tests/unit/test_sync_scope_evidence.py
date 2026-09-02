from __future__ import annotations

from pathlib import Path

from _dispatch_runtime.phase_runtime import load_sync_result
from _sync.adapter import adapter_for_repo


def test_sync_result_stays_spec_local(tmp_path: Path):
    spec_dir = tmp_path / "spec"
    spec_dir.mkdir()
    spec_dir.joinpath("sync-result.yaml").write_text(
        "spec: demo\nverify_gate_id: gate-1\nverified_tree: tree\nchanged_paths_digest: paths\n"
        "declared_delta_digest: digest\nresult: hook_failed\nresolution_paths:\n  - amend the intent delta\n  - fix the SSOT\n  - file a narrowing task\n",
        encoding="utf-8",
    )
    assert load_sync_result(spec_dir)["spec"] == "demo"


def test_semantic_adapter_maps_host_paths_instead_of_echoing_declared_delta(tmp_path: Path):
    (tmp_path / ".builder").mkdir()
    (tmp_path / ".builder" / "sync-adapter.yaml").write_text(
        "artifact: sync-adapter\nmappings:\n  - paths: [scripts/*.py]\n    tuples:\n      - category: capabilities\n        target: mapped\n        change: enrich\n",
        encoding="utf-8",
    )
    adapter = adapter_for_repo(tmp_path)
    assert adapter is not None
    assert adapter.observed_tuples(["scripts/a.py"]) == [
        {"category": "capabilities", "target": "mapped", "change": "enrich"}
    ]
    assert adapter.observed_tuples(["outside.txt"])[0]["target"] == "unmapped:outside.txt"
