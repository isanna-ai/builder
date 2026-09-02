"""Safety contract for the one-repository runtime directory migration."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import sys
from pathlib import Path

from _dispatch_runtime import lane_common


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


def _load(script: str, name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


migrate = _load("migrate.py", "isanna_migrate_under_test")
isanna = _load("isanna.py", "isanna_migrate_cli_under_test")


def _run(argv: list[str]) -> tuple[int, str]:
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = isanna.main(argv)
    return code, out.getvalue()


def _runtime(root: Path) -> Path:
    specpilot = root / ".specpilot"
    specpilot.mkdir()
    (specpilot / "keep.txt").write_text("durable runtime data\n", encoding="utf-8")
    return specpilot


def test_migrate_moves_one_runtime_directory(tmp_path: Path) -> None:
    _runtime(tmp_path)

    code, out = _run(["migrate", "--dir", "--target", str(tmp_path)])

    assert code == 0
    assert not (tmp_path / ".specpilot").exists()
    assert (tmp_path / ".builder" / "keep.txt").read_text(encoding="utf-8") == "durable runtime data\n"
    assert "resolver now uses .builder/ automatically" in out


def test_migrate_refuses_existing_builder_even_with_force(tmp_path: Path) -> None:
    _runtime(tmp_path)
    (tmp_path / ".builder").mkdir()

    code, out = _run(["migrate", "--dir", "--target", str(tmp_path), "--force"])

    assert code != 0
    assert "already migrated, or a .builder already present" in out
    assert (tmp_path / ".specpilot").exists()


def test_migrate_refuses_specpilot_symlink(tmp_path: Path) -> None:
    actual = tmp_path / "actual"
    actual.mkdir()
    (tmp_path / ".specpilot").symlink_to(actual, target_is_directory=True)

    code, out = _run(["migrate", "--dir", "--target", str(tmp_path)])

    assert code != 0
    assert "symlink" in out.lower()
    assert (tmp_path / ".specpilot").is_symlink()


def test_migrate_refuses_when_nothing_exists_to_migrate(tmp_path: Path) -> None:
    code, out = _run(["migrate", "--dir", "--target", str(tmp_path)])

    assert code != 0
    assert "nothing to migrate" in out


def test_migrate_refuses_live_scheduler_but_allows_stale_lock(tmp_path: Path) -> None:
    specpilot = _runtime(tmp_path)
    lock = specpilot / "dispatch-queue" / "queue" / ".scheduler.lock"
    lock.parent.mkdir(parents=True)
    lock.write_text(f"dispatch-{os.getpid()}\n", encoding="utf-8")

    code, out = _run(["migrate", "--dir", "--target", str(tmp_path)])

    assert code != 0
    assert "dispatcher is running; stop the daemon first" in out
    assert specpilot.exists()

    lock.write_text("dispatch-99999999\n", encoding="utf-8")
    code, _ = _run(["migrate", "--dir", "--target", str(tmp_path)])
    assert code == 0
    assert (tmp_path / ".builder").exists()


def test_migrate_refuses_live_pgid_but_ignores_dead_pgid(tmp_path: Path) -> None:
    specpilot = _runtime(tmp_path)
    pgids = specpilot / "dispatch-queue" / "live-pgids"
    pgids.mkdir(parents=True)
    (pgids / str(os.getpgrp())).write_text("{}", encoding="utf-8")

    code, out = _run(["migrate", "--dir", "--target", str(tmp_path)])

    assert code != 0
    assert "work is in flight" in out
    assert specpilot.exists()

    (pgids / str(os.getpgrp())).unlink()
    (pgids / "99999999").write_text("{}", encoding="utf-8")
    code, _ = _run(["migrate", "--dir", "--target", str(tmp_path)])
    assert code == 0
    assert (tmp_path / ".builder").exists()


def test_pgid_liveness_probe_only_uses_signal_zero() -> None:
    signals: list[tuple[int, int]] = []
    original = lane_common.os.killpg
    lane_common.os.killpg = lambda pgid, sig: signals.append((pgid, sig))
    try:
        assert lane_common._pgid_group_alive(4242)
    finally:
        lane_common.os.killpg = original
    assert signals == [(4242, 0)]


def test_migrate_dry_run_reports_action_without_moving(tmp_path: Path) -> None:
    specpilot = _runtime(tmp_path)

    code, out = _run(["migrate", "--dir", "--target", str(tmp_path), "--dry-run"])

    assert code == 0
    assert "WOULD MOVE" in out
    assert "guards passed" in out
    assert specpilot.exists()
    assert not (tmp_path / ".builder").exists()


def test_migrate_dry_run_does_not_materialize_a_missing_runtime_directory(tmp_path: Path) -> None:
    target = tmp_path / "empty-repo"
    target.mkdir()

    code, out = _run(["migrate", "--dir", "--target", str(target), "--dry-run"])

    assert code != 0
    assert "nothing to migrate" in out
    assert not (target / ".specpilot").exists()
    assert not (target / ".builder").exists()


def test_migrate_leaves_representative_reader_on_builder(tmp_path: Path) -> None:
    _runtime(tmp_path)

    code, _ = _run(["migrate", "--dir", "--target", str(tmp_path)])

    assert code == 0
    assert migrate.runtime_dir(tmp_path) == tmp_path / ".builder"
