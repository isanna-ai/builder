from __future__ import annotations

from pathlib import Path

from _dispatch_runtime.phase_runtime import sync_visibility


def test_sync_visibility_refuses_stale_delta_digest(tmp_path: Path):
    spec_dir = tmp_path / "spec"
    spec_dir.mkdir()
    spec_dir.joinpath("spec.yaml").write_text("name: demo\nstatus: verified\ncurrent_phase: sync\n", encoding="utf-8")
    spec_dir.joinpath("ssot-delta.yaml").write_text("capabilities: []\nbehaviors: []\njourneys: []\n", encoding="utf-8")
    spec_dir.joinpath("sync-result.yaml").write_text(
        "spec: demo\nverify_gate_id: gate-1\nverified_tree: tree\nchanged_paths_digest: paths\n"
        "declared_delta_digest: stale\nresult: divergence\nresolution_paths:\n  - amend the intent delta\n  - fix the SSOT\n  - file a narrowing task\n",
        encoding="utf-8",
    )
    assert sync_visibility(spec_dir) is None
