from __future__ import annotations

from pathlib import Path

from _sync.readmit import readmit_spec
from _validators.common import parse_yaml_like_file
from tests.unit.test_sync_readmit_cli import _seed_spec


def test_readmission_stops_before_normal_sync_and_completion(tmp_path: Path):
    spec_dir = _seed_spec(tmp_path, "demo")
    before, errors = parse_yaml_like_file(spec_dir / "spec.yaml")
    assert not errors
    readmit_spec(tmp_path, "demo")
    after, errors = parse_yaml_like_file(spec_dir / "spec.yaml")
    assert not errors
    assert after == before
    assert not (spec_dir / "sync-result.yaml").exists()
    assert not (tmp_path / ".builder" / "model" / "system-model.yaml").exists()
