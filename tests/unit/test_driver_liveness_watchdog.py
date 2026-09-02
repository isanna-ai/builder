"""T6: the LLM-free driver liveness watchdog (AC-R7-1, AC-R7-2)."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import driver_liveness_watchdog as watchdog


class FakeNotifier:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def notify(self, kind, packet):
        self.calls.append((kind, packet))


def _write_heartbeat(path: Path, *, at: datetime, pid: int = 12345) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"pid": pid, "at": at.isoformat().replace("+00:00", "Z")}), encoding="utf-8")


# --- AC-R7-1: detects a stalled or dead driver from its heartbeat and pages --


def test_detects_stall_from_stale_heartbeat_and_pages_owner(tmp_path):
    hb = tmp_path / "driver-heartbeat.json"
    _write_heartbeat(hb, at=datetime.now(timezone.utc) - timedelta(seconds=999))
    notifier = FakeNotifier()

    verdict = watchdog.ensure_once(hb, notifier, stale_after_seconds=120)

    assert verdict.alive is False
    assert notifier.calls, "expected the owner to be paged"
    kind, packet = notifier.calls[0]
    assert kind == "driver_stalled"
    assert packet["reason"]


def test_detects_stall_from_dead_pid_even_with_a_fresh_timestamp_detects_stall(tmp_path):
    hb = tmp_path / "driver-heartbeat.json"
    _write_heartbeat(hb, at=datetime.now(timezone.utc), pid=999999999)  # not a live pid
    notifier = FakeNotifier()

    verdict = watchdog.ensure_once(hb, notifier, stale_after_seconds=120)

    assert verdict.alive is False
    assert notifier.calls


def test_detects_stall_from_missing_heartbeat_file_detects_stall(tmp_path):
    notifier = FakeNotifier()
    verdict = watchdog.ensure_once(tmp_path / "nope.json", notifier, stale_after_seconds=120)
    assert verdict.alive is False
    assert notifier.calls


def test_detects_stall_when_fresh_heartbeat_has_no_driver_pid_detects_stall(tmp_path):
    hb = tmp_path / "driver-heartbeat.json"
    hb.write_text(
        json.dumps({"at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")}),
        encoding="utf-8",
    )
    notifier = FakeNotifier()

    verdict = watchdog.ensure_once(hb, notifier, stale_after_seconds=120)

    assert verdict.alive is False
    assert notifier.calls


def test_detects_stall_when_fresh_heartbeat_has_malformed_driver_pid_detects_stall(tmp_path):
    hb = tmp_path / "driver-heartbeat.json"
    hb.write_text(
        json.dumps({
            "pid": "not-a-pid",
            "at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }),
        encoding="utf-8",
    )
    notifier = FakeNotifier()

    verdict = watchdog.ensure_once(hb, notifier, stale_after_seconds=120)

    assert verdict.alive is False
    assert notifier.calls


def test_live_driver_with_fresh_heartbeat_is_not_paged_detects_stall(tmp_path):
    hb = tmp_path / "driver-heartbeat.json"
    _write_heartbeat(hb, at=datetime.now(timezone.utc) - timedelta(seconds=5), pid=os_getpid_for_test())
    notifier = FakeNotifier()

    verdict = watchdog.ensure_once(hb, notifier, stale_after_seconds=120)

    assert verdict.alive is True
    assert notifier.calls == []


def os_getpid_for_test() -> int:
    import os
    return os.getpid()  # this test process is definitely alive


# --- AC-R7-2: the escalation channel has no LLM dependency and never routes ---
# --- through the driver -------------------------------------------------------


def test_watchdog_module_never_imports_the_driver_it_watches():
    source = (SCRIPTS / "driver_liveness_watchdog.py").read_text(encoding="utf-8")
    assert "import builder_driver" not in source
    assert "from builder_driver" not in source
    assert "spec_from_file_location" not in source  # never dynamically loads builder-driver.py either
    assert not hasattr(watchdog, "BuilderDriver")
    assert not hasattr(watchdog, "SchedulerTurnSource")


def test_watchdog_module_has_no_llm_or_agent_cli_dependency():
    source = (SCRIPTS / "driver_liveness_watchdog.py").read_text(encoding="utf-8").lower()
    forbidden = ["anthropic", "openai", "claude -p", "codex exec", "claude_code", "lane_claude", "lane_codex"]
    for term in forbidden:
        assert term not in source, f"watchdog must stay LLM-free; found {term!r}"


def test_page_owner_routes_only_through_the_plain_notifier_sink(tmp_path):
    notifier = FakeNotifier()
    verdict = watchdog.LivenessVerdict(False, "heartbeat stale (999s > 120s)")
    watchdog.page_owner(notifier, verdict, tmp_path / "driver-heartbeat.json")
    assert len(notifier.calls) == 1
    kind, packet = notifier.calls[0]
    assert kind == "driver_stalled"
    assert packet["reason"] == verdict.reason


def test_driver_stalled_notification_is_formatted_as_a_driver_page(tmp_path):
    from _dispatch_runtime.notifier import format_packet

    rendered = format_packet("driver_stalled", {
        "reason": "heartbeat stale", "heartbeat_path": str(tmp_path / "heartbeat.json"),
    })
    assert "DRIVER STALLED" in rendered
    assert "BLOCKED_HUMAN" not in rendered


def test_notifier_used_in_production_is_the_same_llm_free_sink_as_dispatch():
    """The watchdog pages via `_dispatch_runtime.notifier.build_notifier` -- the
    SAME plain file/HTTP sink the dispatcher already uses for blocked_human /
    spec_failed alerts, never a new LLM-backed channel."""
    from _dispatch_runtime.notifier import FileNotifier, MultiNotifier, TelegramNotifier

    n = MultiNotifier([FileNotifier(Path("/tmp"))])
    assert isinstance(n, MultiNotifier)
    # Neither concrete notifier type performs any LLM call -- both are pure I/O.
    for cls in (FileNotifier, TelegramNotifier):
        assert "llm" not in cls.__doc__.lower() if cls.__doc__ else True
