from __future__ import annotations

from pathlib import Path

from scripts._validators.anchors import run
from scripts._validators.common import ValidationContext


def make(tmp_path: Path, kind: str, locator: str, text: str = "def target_symbol():\n    return 'needle'\n", path: str = "src.py") -> Path:
    spec = tmp_path / ".builder" / "specs" / "demo"
    spec.mkdir(parents=True)
    if path != "missing.py":
        (tmp_path / path).write_text(text, encoding="utf-8")
    (spec / "traceability.yaml").write_text(
        f"task_links:\n  - task_id: T1\n    files:\n      - path: {path}\n        relevance: primary\n        anchors:\n          - id: A1\n            kind: {kind}\n            locator: {locator}\n",
        encoding="utf-8",
    )
    return spec


def test_literal_anchor_passes(tmp_path: Path) -> None:
    assert run(ValidationContext(make(tmp_path, "literal_string", "needle"))).errors == []


def test_literal_anchor_missing_errors(tmp_path: Path) -> None:
    assert "A1" in "\n".join(run(ValidationContext(make(tmp_path, "literal_string", "absent"))).errors)


def test_regex_anchor_passes(tmp_path: Path) -> None:
    assert run(ValidationContext(make(tmp_path, "regex_v1", "target_.*"))).errors == []


def test_regex_anchor_missing_errors(tmp_path: Path) -> None:
    assert run(ValidationContext(make(tmp_path, "regex_v1", "NO_MATCH"))).errors


def test_symbol_anchor_passes(tmp_path: Path) -> None:
    assert run(ValidationContext(make(tmp_path, "symbol_v1", "target_symbol"))).errors == []


def test_symbol_anchor_missing_errors(tmp_path: Path) -> None:
    assert run(ValidationContext(make(tmp_path, "symbol_v1", "other_symbol"))).errors


def test_missing_source_file_errors(tmp_path: Path) -> None:
    assert "file not found" in "\n".join(run(ValidationContext(make(tmp_path, "literal_string", "x", path="missing.py"))).errors)
