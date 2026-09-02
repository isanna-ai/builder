from __future__ import annotations

from pathlib import Path

from _dispatch_runtime.phase_runtime import sync_visibility


def test_invalid_sync_result_does_not_upgrade_visibility(tmp_path: Path):
    spec_dir = tmp_path / "spec"
    spec_dir.mkdir()
    spec_dir.joinpath("spec.yaml").write_text("name: demo\nstatus: verified\ncurrent_phase: sync\n", encoding="utf-8")
    spec_dir.joinpath("sync-result.yaml").write_text("result: divergence\n", encoding="utf-8")
    assert sync_visibility(spec_dir) is None
