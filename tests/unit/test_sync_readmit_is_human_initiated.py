from __future__ import annotations

from pathlib import Path


def test_dispatch_surfaces_do_not_expose_sync_readmit_implicitly(tmp_path: Path):
    for path in (
        Path("scripts/builder-dispatch.py"),
        Path("scripts/_dispatch_runtime/scheduler.py"),
    ):
        text = path.read_text(encoding="utf-8")
        assert "sync-readmit" not in text
