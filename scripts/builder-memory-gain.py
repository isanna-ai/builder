#!/usr/bin/env python3
"""The memory-gain A/B harness CLI.

Subcommands:
  report  — load memory_eval records, render the memory_gain_report markdown,
            write it to --out, and optionally POST to Telegram (--telegram).
  status  — count records per memory_mode (R3 baseline status check).

Empty input is a hard error for `report`: exits non-zero with a clear message
BEFORE writing any file (R4). Telegram delivery is best-effort: a missing token
or a network failure still leaves the rendered report on disk and exits zero (R5).

HARD INVARIANT: imports NO hivemind module. memory_mode defaults to "off"; the
off rows are the permanent control arm.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from _dispatch_runtime.paths import runtime_dir
from typing import Any, Callable

from _dispatch_runtime.notifier import _resolve_token
from _telemetry.memory_eval import load_memory_evals
from _telemetry.memory_gain_report import build_gain_report, render_markdown

# Delivery is opt-in: deployments provide their destination through the
# environment, while the neutral default makes accidental delivery impossible.
TELEGRAM_CHAT_ID = os.environ.get("BUILDER_TELEGRAM_CHAT_ID")
TELEGRAM_THREAD_ID = os.environ.get("BUILDER_TELEGRAM_THREAD_ID")  # forum topic; None = no topic

HttpSender = Callable[..., Any]


def _default_out(root: Path) -> Path:
    return runtime_dir(root) / "telemetry" / "reports" / "memory-gain-report.md"


def _telegram_post(token: str, chat_id: str, text: str, thread_id: int | None = None) -> None:
    """Real Telegram sender (urllib, like TelegramNotifier). Best-effort; the
    caller swallows any error. Injectable so tests never hit the network."""
    import json
    import urllib.request

    payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
    if thread_id is not None:
        payload["message_thread_id"] = thread_id
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=data,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        resp.read()


def _cmd_report(args: argparse.Namespace, http_sender: HttpSender) -> int:
    root = Path(args.root).resolve()
    records = load_memory_evals(root)
    if not records:
        print("error: no memory_eval records found in the sink", file=sys.stderr)
        return 1

    report = build_gain_report(records)
    markdown = render_markdown(report)

    out_path = Path(args.out) if args.out else _default_out(root)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(markdown, encoding="utf-8")
    print(f"wrote {out_path}")

    if args.telegram:
        token = _resolve_token({})
        if not token:
            print("telegram skipped (no token)")
            return 0
        if not TELEGRAM_CHAT_ID:
            print("telegram skipped (no destination configured)")
            return 0
        try:
            http_sender(token, TELEGRAM_CHAT_ID, markdown, thread_id=TELEGRAM_THREAD_ID)
            print(f"telegram posted to {TELEGRAM_CHAT_ID}")
        except Exception as exc:  # noqa: BLE001 - delivery never fails report generation
            print(f"telegram delivery failed: {exc} (report still written to {out_path})")
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    records = load_memory_evals(root)
    counts: dict[str, int] = {}
    for rec in records:
        mode = str(rec.get("memory_mode", "off"))
        counts[mode] = counts.get(mode, 0) + 1
    # Always report the off arm (even when 0), then any other observed modes.
    print(f"memory_mode=off: {counts.get('off', 0)}")
    for mode in sorted(m for m in counts if m != "off"):
        print(f"memory_mode={mode}: {counts[mode]}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Aggregate memory_eval records into a memory_gain_report.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_report = sub.add_parser("report", help="render the memory_gain_report and optionally post to Telegram")
    p_report.add_argument("--root", required=True, help="isanna root containing active runtime telemetry")
    p_report.add_argument("--out", default=None, help="output markdown path (default: active runtime telemetry report path)")
    tg = p_report.add_mutually_exclusive_group()
    tg.add_argument("--telegram", dest="telegram", action="store_true", help="POST the report to Telegram")
    tg.add_argument("--no-telegram", dest="telegram", action="store_false", help="render locally only (default)")
    p_report.set_defaults(telegram=False)

    p_status = sub.add_parser("status", help="count memory_eval records per memory_mode")
    p_status.add_argument("--root", required=True, help="isanna root containing active runtime telemetry")

    return parser


def run(argv: list[str], *, http_sender: HttpSender | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    sender = http_sender or _telegram_post
    if args.command == "report":
        return _cmd_report(args, sender)
    if args.command == "status":
        return _cmd_status(args)
    parser.error("unknown command")
    return 2


def main() -> int:
    return run(sys.argv[1:])


if __name__ == "__main__":
    sys.exit(main())
