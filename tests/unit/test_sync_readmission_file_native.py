from __future__ import annotations

from pathlib import Path

from _sync.readmit import readmit_spec
from tests.unit.test_sync_readmit_cli import _seed_spec


def test_sync_readmission_uses_only_repo_local_files(tmp_path: Path):
    _seed_spec(tmp_path, "demo")
    readmit_spec(tmp_path, "demo")
    assert (tmp_path / ".builder" / "specs" / "demo" / "sync-readmission-report.yaml").is_file()

