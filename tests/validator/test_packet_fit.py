from __future__ import annotations

from pathlib import Path

from scripts._validators.common import ValidationContext
from scripts._validators.packet_fit import run


def root(tmp_path: Path, profile: str | None = "tiny_local") -> Path:
    (tmp_path / ".builder" / "specs" / "demo").mkdir(parents=True)
    (tmp_path / "schemas").mkdir()
    (tmp_path / "schemas" / "runner.schema.yaml").write_text(
        "properties:\n  model_profiles:\n    properties:\n      tiny_local:\n        effective_context_tokens: 12000\n        initial_packet_cap_tokens: 4000\n        max_full_read_files: 1\n        max_slice_files: 3\n",
        encoding="utf-8",
    )
    spec = "name: demo\n" + (f"target_model_profile: {profile}\n" if profile else "")
    (tmp_path / ".builder" / "specs" / "demo" / "spec.yaml").write_text(spec, encoding="utf-8")
    return tmp_path


def write(root: Path, tokens: list[int], declared: int | None = None, full: bool = True, status: str = "fit") -> None:
    spec = root / ".builder" / "specs" / "demo"
    pf = "" if declared is None else f"    packet_fit:\n      status: {status}\n      initial_packet_tokens: {declared}\n"
    (spec / "tasks.yaml").write_text(f"tasks:\n  - id: T1\n{pf}", encoding="utf-8")
    files = "".join(f"      - path: f{i}.txt\n        relevance: primary\n        estimated_tokens: {tok}\n        load_priority: must\n        full_read_eligible: {str(full).lower()}\n" for i, tok in enumerate(tokens))
    (spec / "traceability.yaml").write_text(f"task_links:\n  - task_id: T1\n    files:\n{files}", encoding="utf-8")


def ctx(root: Path) -> ValidationContext:
    return ValidationContext(root / ".builder" / "specs" / "demo")


def test_packet_sum_mismatch(tmp_path: Path) -> None:
    r = root(tmp_path); write(r, [100, 200], declared=1)
    assert "packet token sum mismatch" in "\n".join(run(ctx(r)).errors)


def test_context_overflow(tmp_path: Path) -> None:
    r = root(tmp_path); write(r, [15000], declared=15000)
    assert "packet exceeds context budget" in "\n".join(run(ctx(r)).errors)


def test_full_read_count_limit(tmp_path: Path) -> None:
    r = root(tmp_path); write(r, [1, 1], declared=2)
    assert "full_read_files count 2 exceeds limit 1" in "\n".join(run(ctx(r)).errors)


def test_tiny_local_two_full_files_errors(tmp_path: Path) -> None:
    r = root(tmp_path); write(r, [10, 10], declared=20)
    assert run(ctx(r)).errors


def test_not_fit_status_errors(tmp_path: Path) -> None:
    r = root(tmp_path); write(r, [10], declared=10, status="not_fit")
    assert "not_fit" in "\n".join(run(ctx(r)).errors)


def test_no_profile_skips(tmp_path: Path) -> None:
    r = root(tmp_path, None); write(r, [15000])
    assert run(ctx(r)).skipped
