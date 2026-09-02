from __future__ import annotations

from pathlib import Path

from _validators.common import ValidationContext
from _validators.sync_artifacts import run_sync_result


def test_sync_result_uses_the_canonical_filename(tmp_path: Path):
    spec_dir = tmp_path / "spec"
    spec_dir.mkdir()
    spec_dir.joinpath("spec.yaml").write_text("name: demo\nstatus: synced\ncurrent_phase: sync\n", encoding="utf-8")
    result = run_sync_result(ValidationContext(spec_dir=spec_dir))
    assert not result.skipped
    assert any("required" in error for error in result.errors)
