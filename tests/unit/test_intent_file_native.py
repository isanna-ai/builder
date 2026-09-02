from __future__ import annotations

from pathlib import Path


def test_intent_layer_stays_file_native():
    root = Path(__file__).resolve().parents[2]
    planning = (root / "scripts" / "planning.py").read_text(encoding="utf-8")
    record = (root / "scripts" / "record.py").read_text(encoding="utf-8")
    cli = (root / "scripts" / "isanna.py").read_text(encoding="utf-8")
    combined = "\n".join([planning, record, cli])
    assert "builder_intents" not in combined
    assert "Mission Control" not in combined
