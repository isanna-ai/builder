from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import sys
from pathlib import Path

from _telemetry.memory_eval import append_memory_eval, build_memory_eval


def _load_cli():
    # The CLI lives at scripts/builder-memory-gain.py (hyphenated, not importable by name).
    path = Path(__file__).resolve().parents[2] / "scripts" / "builder-memory-gain.py"
    spec = importlib.util.spec_from_file_location("builder_memory_gain", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["builder_memory_gain"] = module
    spec.loader.exec_module(module)
    return module


def _seed(root: Path, n: int = 5):
    for i in range(n):
        rec = build_memory_eval(
            run_id=f"w{i}",
            spec_id=f"s{i}",
            lane="claude",
            plan_tokens_in=100,
            plan_tokens_out=1000 + i,
            plan_wall_ms=5000 + i,
            spec_outcome="unknown",
        )
        append_memory_eval(root, rec)


def _clear_token_env():
    for var in ("TELEGRAM_BOT_TOKEN", "BUILDER_TELEGRAM_BOT_TOKEN"):
        os.environ.pop(var, None)


def test_report_renders_file(tmp_path):
    cli = _load_cli()
    _seed(tmp_path)
    out = tmp_path / "report.md"
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cli.run(["report", "--root", str(tmp_path), "--no-telegram", "--out", str(out)])
    assert rc == 0
    assert out.exists()
    text = out.read_text(encoding="utf-8")
    assert "off" in text
    assert "Mann-Whitney" in text
    assert "Cohen" in text


def test_status_counts_off(tmp_path):
    cli = _load_cli()
    _seed(tmp_path, n=3)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cli.run(["status", "--root", str(tmp_path)])
    assert rc == 0
    assert "memory_mode=off: 3" in buf.getvalue()


def test_report_empty_root_exits_nonzero_and_writes_nothing(tmp_path):
    cli = _load_cli()
    out = tmp_path / "report.md"
    buf = io.StringIO()
    err = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
        rc = cli.run(["report", "--root", str(tmp_path), "--no-telegram", "--out", str(out)])
    assert rc != 0
    assert not out.exists()
    combined = (buf.getvalue() + err.getvalue()).lower()
    assert "no memory_eval records" in combined


def test_telegram_missing_token_skips_and_exits_zero(tmp_path):
    cli = _load_cli()
    _seed(tmp_path)
    out = tmp_path / "report.md"
    _clear_token_env()
    sent = []
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cli.run(
            ["report", "--root", str(tmp_path), "--telegram", "--out", str(out)],
            http_sender=lambda *a, **k: sent.append((a, k)),
        )
    assert rc == 0
    assert out.exists()
    assert sent == []
    assert "telegram skipped" in buf.getvalue().lower()


def test_telegram_with_explicit_destination_calls_injected_sender(tmp_path):
    os.environ["BUILDER_TELEGRAM_CHAT_ID"] = "-1001234567890"  # publish-ok: schema-valid demo destination
    cli = _load_cli()
    _seed(tmp_path)
    out = tmp_path / "report.md"
    _clear_token_env()
    os.environ["TELEGRAM_BOT_TOKEN"] = "test-token"
    sent = []
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli.run(
                ["report", "--root", str(tmp_path), "--telegram", "--out", str(out)],
                http_sender=lambda token, chat_id, text, thread_id=None: sent.append((token, chat_id, text, thread_id)),
            )
    finally:
        os.environ.pop("TELEGRAM_BOT_TOKEN", None)
        os.environ.pop("BUILDER_TELEGRAM_CHAT_ID", None)
    assert rc == 0
    assert out.exists()
    assert len(sent) == 1
    assert sent[0][0] == "test-token"
    assert sent[0][1] == "-1001234567890"  # publish-ok: schema-valid demo destination


def test_telegram_with_token_but_no_destination_skips(tmp_path):
    os.environ.pop("BUILDER_TELEGRAM_CHAT_ID", None)
    cli = _load_cli()
    _seed(tmp_path)
    os.environ["TELEGRAM_BOT_TOKEN"] = "test-token"
    sent = []
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli.run(
                ["report", "--root", str(tmp_path), "--telegram"],
                http_sender=lambda *a, **k: sent.append((a, k)),
            )
    finally:
        os.environ.pop("TELEGRAM_BOT_TOKEN", None)
    assert rc == 0
    assert sent == []
    assert "no destination configured" in buf.getvalue().lower()
