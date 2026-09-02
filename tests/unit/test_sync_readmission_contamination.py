from __future__ import annotations

from pathlib import Path

from tests.unit.test_sync_readmit_cli import _seed_spec
from _sync.readmit import readmit_spec


def test_sync_readmission_stays_per_spec(tmp_path: Path):
    _seed_spec(tmp_path, "demo")
    other = _seed_spec(tmp_path, "other")
    readmit_spec(tmp_path, "demo")
    assert not (other / "implementation-baseline.yaml").exists()

