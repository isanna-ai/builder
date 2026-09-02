"""Pluggable dispatch notifier.

Keeps the core portable: by default a BLOCKED_HUMAN escalation or a plan-ready
gate writes a compact packet to a file under the queue (works anywhere). A repo
opts in to Telegram by adding a `pipeline.notify.telegram` block to dispatch.yaml;
the token is resolved from the environment, never stored in config.

Packet kinds:
  - "blocked_human"  a phase escalated to a human (predicate failed / stuck)
  - "plan_ready"     the plan phase completed and the plan-approval gate is holding
  - "pr_opened"      a verified spec opened a PR (CI-green auto-merge armed)
  - "spec_failed"    a spec hit terminal FAILED (retry/rate-limit exhausted) — the
                     queue dead-ends here; without this alert the stall is silent
  - "lane_cooled"    a lane entered rate-limit cooldown with queued work blocked;
                     debounced per cooldown_until so one alert per distinct cooldown
  - "attempt-interrupted"  a central attempt's session group was proven dead (e.g.
                     daemon restart) - the item was requeued with backoff or, once
                     max_attempts is exhausted, failed
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request
from pathlib import Path
from typing import Any, Protocol


def format_packet(kind: str, packet: dict[str, Any]) -> str:
    spec = packet.get("spec_id", "?")
    phase = packet.get("phase", "?")
    if kind == "plan_ready":
        gate_lane = packet.get("gate_lane")
        reason = packet.get("reason")
        if gate_lane == "A" and reason:
            return (
                f"📋 LANE A HELD — {spec}\n"
                f"{reason}\n"
                f"Review the artifacts, then approve explicitly if it is safe to continue:\n"
                f"    isanna dispatch approve {spec}\n"
            )
        return (
            f"📋 PLAN READY — {spec}\n"
            f"plan complete; the plan-approval gate is holding before implement.\n"
            f"Approve to run implement→verify autonomously:\n"
            f"    isanna dispatch approve {spec}\n"
        )
    if kind == "veto_window_opened":
        seconds = int(packet.get("quiet_period_seconds") or 0)
        return (
            f"⏳ VETO WINDOW OPEN — {spec}\n"
            f"Lane B opens automatically after {seconds}s of silence.\n"
            f"Hold it before then:\n"
            f"    isanna dispatch hold {spec} --reason \"<reason>\"\n"
        )
    if kind == "veto_hold_recorded":
        return f"✋ VETO HOLD RECORDED — {spec}\nReason: {packet.get('reason') or '(none provided)'}\n"
    if kind == "driver_stalled":
        return (
            "🚨 BUILDER DRIVER STALLED\n"
            f"Reason: {packet.get('reason') or 'heartbeat unavailable'}\n"
            f"Heartbeat: {packet.get('heartbeat_path') or '(unknown)'}\n"
        )
    if kind == "pr_opened":
        return (
            f"🚀 PR OPENED — {spec}\n"
            f"{packet.get('pr_url') or '(PR url unavailable)'}\n"
            f"CI-green auto-merge armed; prod deploy stays gated by the protected production environment.\n"
        )
    if kind == "lane_cooled":
        lane = packet.get("lane", "?")
        project = packet.get("project", "?")
        queued = packet.get("queued_on_lane", 0)
        cooldown_seconds = packet.get("cooldown_seconds", 0)
        mins = max(1, round(cooldown_seconds / 60))
        return (
            f"⏳ {lane.upper()} rate-limited — {project}: {queued} queued spec(s) blocked, ~{mins}m cooldown.\n"
            "Reroute to claude? Update the queued work's lane through the current dispatch workflow."
        )
    if kind == "spec_failed":
        lane = packet.get("lane", "?")
        reason = packet.get("reason", "")
        attempt = packet.get("attempt")
        maxa = packet.get("max_attempts")
        tail = packet.get("log_tail", "")
        body = (
            f"❌ FAILED — {spec} / {phase} (lane {lane})\n"
            f"Attempts exhausted ({attempt}/{maxa}); reason: {reason}\n"
            f"The queue is stalled on this spec — review, re-queue, or abandon.\n"
        )
        if tail:
            body += f"--- log tail ---\n{tail}\n"
        return body
    if kind == "attempt-interrupted":
        disposition = packet.get("disposition", "?")
        reason = packet.get("detail", "")
        work_id = packet.get("work_id", "?")
        return (
            f"🔁 ATTEMPT INTERRUPTED — {spec} (work {work_id})\n"
            f"Reason: {reason}\n"
            f"Disposition: {disposition}\n"
        )
    lane = packet.get("lane", "?")
    reason = packet.get("reason", "")
    decision = packet.get("one_decision") or "Review the phase output and decide whether to continue, fix, or abandon."
    tail = packet.get("log_tail", "")
    body = (
        f"🚧 BLOCKED_HUMAN — {spec} / {phase} (lane {lane})\n"
        f"Reason: {reason}\n"
        f"Decision needed: {decision}\n"
    )
    if tail:
        body += f"--- log tail ---\n{tail}\n"
    return body


class Notifier(Protocol):
    def notify(self, kind: str, packet: dict[str, Any]) -> None: ...


class FileNotifier:
    """Always-on: writes packets under <queue_root>/queue/notifications/ and keeps
    a per-spec marker under <queue_root>/queue/blocked/ for quick triage."""

    def __init__(self, queue_root: Path):
        self.dir = Path(queue_root) / "queue" / "notifications"
        self.blocked_dir = Path(queue_root) / "queue" / "blocked"
        self.failed_dir = Path(queue_root) / "queue" / "failed"

    def notify(self, kind: str, packet: dict[str, Any]) -> None:
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            stamp = str(packet.get("created_at") or "").replace(":", "").replace("-", "") or "now"
            wid = packet.get("work_id", "x")
            (self.dir / f"{stamp}-{kind}-{wid}.md").write_text(format_packet(kind, packet), encoding="utf-8")
            if kind in ("blocked_human", "plan_ready", "driver_stalled"):
                self.blocked_dir.mkdir(parents=True, exist_ok=True)
                (self.blocked_dir / f"{packet.get('spec_id','spec')}.md").write_text(
                    format_packet(kind, packet), encoding="utf-8")
            if kind == "spec_failed":
                self.failed_dir.mkdir(parents=True, exist_ok=True)
                (self.failed_dir / f"{packet.get('spec_id','spec')}.md").write_text(
                    format_packet(kind, packet), encoding="utf-8")
        except OSError:
            pass


class TelegramNotifier:
    """Best-effort Telegram delivery; never raises (notifications must not break dispatch)."""

    def __init__(self, token: str, chat_id: str, thread_id: int | None = None):
        self.token = token
        self.chat_id = chat_id
        self.thread_id = thread_id

    def notify(self, kind: str, packet: dict[str, Any]) -> None:
        payload: dict[str, Any] = {"chat_id": self.chat_id, "text": format_packet(kind, packet)}
        if self.thread_id is not None:
            payload["message_thread_id"] = self.thread_id
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{self.token}/sendMessage",
            data=data, headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                resp.read()
        except Exception:  # noqa: BLE001
            pass


class MultiNotifier:
    def __init__(self, notifiers: list[Notifier]):
        self._notifiers = notifiers

    def notify(self, kind: str, packet: dict[str, Any]) -> None:
        for n in self._notifiers:
            try:
                n.notify(kind, packet)
            except Exception:  # noqa: BLE001
                pass


def _resolve_token(cfg: dict[str, Any]) -> str | None:
    # Prefer an env var named in config; fall back to the conventional one.
    name = cfg.get("token_env")
    candidates = [name] if name else []
    candidates += ["TELEGRAM_BOT_TOKEN", "BUILDER_TELEGRAM_BOT_TOKEN"]
    for c in candidates:
        if c and os.environ.get(c):
            return os.environ[c].strip()
    return None


# Shared default: every dispatcher posts only when a deployment explicitly
# provides its Telegram destination. Token always comes from the environment.
DEFAULT_TELEGRAM_CHAT_ID = os.environ.get("BUILDER_TELEGRAM_CHAT_ID")
DEFAULT_TELEGRAM_THREAD_ID = None


def build_notifier(pipeline: dict[str, Any], queue_root: Path) -> Notifier:
    """Always use FileNotifier; Telegram requires an explicit destination.

    A project may set chat_id/thread_id via pipeline.notify.telegram or the
    deployment may provide BUILDER_TELEGRAM_CHAT_ID. With a token but no
    destination, Telegram is skipped with a diagnostic; without a token, only
    FileNotifier runs. `notify.telegram: false` explicitly opts out.
    """
    notifiers: list[Notifier] = [FileNotifier(queue_root)]
    notify_cfg = (pipeline or {}).get("notify")
    notify_cfg = notify_cfg if isinstance(notify_cfg, dict) else {}
    tg = notify_cfg.get("telegram", {})
    if tg is False:  # explicit opt-out
        return MultiNotifier(notifiers)
    if not isinstance(tg, dict):
        tg = {}
    # Per-project chat override keeps its own thread (or none); defaults do not
    # select a deployment-specific Telegram topic.
    if "chat_id" in tg:
        chat_id, thread = tg["chat_id"], tg.get("thread_id")
    else:
        chat_id, thread = DEFAULT_TELEGRAM_CHAT_ID, tg.get("thread_id", DEFAULT_TELEGRAM_THREAD_ID)
    token = _resolve_token(tg)
    if chat_id and token:
        notifiers.append(TelegramNotifier(token, str(chat_id),
                                          int(thread) if thread is not None else None))
    elif token and not chat_id:
        print("telegram skipped (no destination configured)", file=sys.stderr)
    return MultiNotifier(notifiers)
