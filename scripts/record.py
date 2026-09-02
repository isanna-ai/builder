#!/usr/bin/env python3
"""Static Builder flight recorder.

Reads .builder/ and dispatcher queue records, then writes self-contained
HTML. Verification stamps and chain results come from gate-coverage.py.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import html
import importlib.util
import json
import os
import sys
import webbrowser
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

# record.py dynamically imports gate-coverage.py and dispatcher helpers.  A build
# must remain read-only outside --out even when those modules have not been imported
# before, so disable bytecode before the first dynamic import below.
sys.dont_write_bytecode = True

_SCRIPT_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _SCRIPT_DIR.parent
_GC = _SCRIPT_DIR / "gate-coverage.py"
_GATE_EVIDENCE = _SCRIPT_DIR / "_dispatch_runtime" / "gate_evidence.py"

if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from _telemetry.common import SECRET_PATTERNS  # noqa: E402 - sibling import after path bootstrap
import planning  # noqa: E402 - sibling import after path bootstrap
from _dispatch_runtime.paths import runtime_dir  # noqa: E402 - sibling import after path bootstrap


def _load_gc():
    spec = importlib.util.spec_from_file_location("gate_coverage", _GC)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {_GC}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


gc = _load_gc()


def _load_dispatcher_version():
    """Load the shared dispatcher-version resolver from its sibling path."""
    spec = importlib.util.spec_from_file_location("record_gate_evidence", _GATE_EVIDENCE)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {_GATE_EVIDENCE}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod.dispatcher_version


def _record_version() -> str:
    """Return the shared dispatcher version without making CLI startup fragile."""
    try:
        token = str(_load_dispatcher_version()() or "").strip()
    except Exception:  # The version flag must remain available if its helper cannot load.
        token = ""
    return token or "unknown"

from _yaml import yaml

_YAML_IS_PYYAML = hasattr(yaml, "SafeLoader")

LEGEND = "Colour means the host ran it. Grey means the agent said it."
CHAIN_NOTE = (
    "The gate is sound; the ledger is tamper-evident. It catches accidental corruption, "
    "naive edits, and incidental tampering. Genuine tamper-resistance requires the evidence "
    "to leave the agent's reach."
)
GATES = ("host_verify", "source_diff", "red_baseline", "packet_contract")
IN_FLIGHT_STATES = {"QUEUED", "DISPATCHED", "RUNNING"}
BUCKETS = (
    ("Authoring", {"specifying", "specified", "designed", "reviewed"}),
    ("Ready", {"planned"}),
    ("In flight", {"implementing", "verifying", "syncing"}),
    ("Awaiting sync", {"verified-awaiting-sync"}),
    ("Verified", {"verified", "verified_with_tasks"}),
    ("Synced", {"synced"}),
    ("Archived", {"archived"}),
)
FORBIDDEN = (
    "forgery-proof",
    "cryptographically secure",
    "tamper-proof",
    "proves the agent didn't tamper",
)


CSS = """
:root {
  color-scheme: dark;
  --bg: #08181b; --bg-deep: #061215; --surface: #0d2428; --surface-2: #112d31;
  --surface-3: #17383c; --fg: #edf8f6; --muted: #91b1ae; --faint: #5f8582;
  --line: #214247; --line-bright: #2e5b60; --agent-bg: #182b2e; --agent-fg: #a6b8b6;
  --host-bg: #0b2023; --host-fg: #f2fbf9; --accent: #18c3b2;
  --blue: #5f91ff; --blue-soft: rgba(95,145,255,.14); --attention: #ff716c;
  --attention-soft: rgba(255,113,108,.13); --warn: #f0b957; --warn-soft: rgba(240,185,87,.13);
  --ok: #45d59d; --ok-soft: rgba(69,213,157,.13); --shadow: 0 18px 52px rgba(0,0,0,.25);
  --radius: 18px; --radius-sm: 12px; --header-h: 62px; --tab-h: 66px;
}
* { box-sizing: border-box; }
html { min-width: 320px; background: var(--bg-deep); overflow-x: hidden; }
body {
  margin: 0; min-height: 100vh; overflow-x: hidden; color: var(--fg);
  font: 14px/1.5 Inter, ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  background: radial-gradient(900px 520px at 8% -10%, rgba(24,195,178,.13), transparent 64%),
              radial-gradient(700px 460px at 92% 0%, rgba(95,145,255,.09), transparent 66%),
              linear-gradient(180deg, #0a1d20 0, var(--bg) 360px, var(--bg-deep) 100%);
}
a { color: inherit; text-decoration-color: var(--line-bright); text-underline-offset: 3px; }
a:hover { color: #fff; text-decoration-color: var(--accent); }
main { width: min(1440px, 100%); margin: 0 auto; padding: 36px 30px 72px; }
h1, h2, h3, p { margin-top: 0; }
h1, h2, h3 { line-height: 1.16; letter-spacing: -.025em; }
h1 { margin-bottom: 10px; font-size: clamp(34px, 4vw, 54px); }
h2 { margin: 0; font-size: 21px; }
h3 { margin: 0; font-size: 15px; }
code, pre, .mono { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
pre { margin: 8px 0 0; overflow-x: auto; white-space: pre-wrap; overflow-wrap: anywhere; font-size: 12px; }

.site-header { position: sticky; top: 0; z-index: 30; min-height: var(--header-h); border-bottom: 1px solid rgba(46,91,96,.72); background: rgba(6,18,21,.9); backdrop-filter: blur(18px); }
.site-header__inner { width: min(1440px,100%); min-height: var(--header-h); margin: 0 auto; padding: 10px 30px; display: flex; align-items: center; justify-content: space-between; }
.brand { display: flex; align-items: center; gap: 10px; color: var(--fg); text-decoration: none; font-weight: 760; }
.brand-mark { width: 29px; height: 29px; color: var(--accent); filter: drop-shadow(0 0 12px rgba(24,195,178,.3)); }
.brand-text small { display: block; color: var(--faint); font-size: 9px; line-height: 1; letter-spacing: .16em; text-transform: uppercase; }
.brand-text span { display: block; font-size: 16px; }
.site-mode { display: inline-flex; align-items: center; gap: 7px; color: var(--muted); font: 10px ui-monospace,monospace; letter-spacing: .11em; text-transform: uppercase; }
.site-mode::before { content:""; width: 7px; height: 7px; border-radius: 50%; background: var(--accent); box-shadow: 0 0 10px var(--accent); }
footer { border-top: 1px solid var(--line); color: var(--muted); }
.footer-inner { width: min(1440px,100%); margin: 0 auto; padding: 24px 30px 38px; display: flex; justify-content: space-between; gap: 20px; }
.legend { color: var(--fg); font-weight: 680; }
.footer-note { color: var(--faint); font: 10px ui-monospace,monospace; letter-spacing: .09em; text-transform: uppercase; }
.bottom-nav { display: none; }

.crumbs { margin: 0 0 28px; color: var(--muted); font-size: 12px; }
.crumbs a { text-decoration: none; }
.crumbs span { padding: 0 8px; color: var(--faint); }
.eyebrow, .section-kicker { color: var(--accent); font: 700 10px ui-monospace,monospace; letter-spacing: .14em; text-transform: uppercase; }
.eyebrow { margin: 0 0 9px; }
.page-head { display: flex; align-items: flex-end; justify-content: space-between; gap: 28px; margin-bottom: 30px; }
.page-head__copy { max-width: 760px; }
.page-head__copy p:last-child { margin-bottom: 0; color: var(--muted); font-size: 15px; }
.page-head__aside { flex: none; }
.quiet { color: var(--muted); }
.faint { color: var(--faint); }

.panel { margin-top: 24px; padding: 21px; border: 1px solid var(--line); border-radius: var(--radius); background: rgba(13,36,40,.78); box-shadow: var(--shadow); }
.panel--quiet { background: rgba(9,28,31,.6); box-shadow: none; }
.panel-head { display: flex; align-items: flex-end; justify-content: space-between; gap: 20px; margin-bottom: 17px; }
.panel-head p { margin: 5px 0 0; color: var(--muted); font-size: 12px; }
.hero-metrics { display: flex; align-items: stretch; gap: 1px; overflow: hidden; border: 1px solid var(--line); border-radius: var(--radius); background: var(--line); box-shadow: var(--shadow); }
.hero-metric { flex: 1 1 0; min-width: 0; padding: 18px 20px; background: rgba(13,36,40,.9); }
.hero-metric--lead { flex: 1.35 1 0; }
.hero-metric span { display: block; color: var(--faint); font: 700 9px ui-monospace,monospace; letter-spacing: .1em; text-transform: uppercase; }
.hero-metric b { display: block; margin-top: 7px; overflow-wrap: anywhere; font-size: clamp(20px,2.4vw,34px); line-height: 1; letter-spacing: -.045em; }
.hero-metric small { display: block; margin-top: 8px; color: var(--muted); }
.hero-metric .bad { color: var(--attention); }
.hero-metric .ok { color: var(--ok); }

.project-grid { display: grid; grid-template-columns: repeat(2,minmax(0,1fr)); gap: 14px; }
.project-card, .repo-card { display: block; min-width: 0; padding: 20px; border: 1px solid var(--line); border-radius: var(--radius); background: linear-gradient(145deg,rgba(17,45,49,.92),rgba(9,28,31,.96)); text-decoration: none; box-shadow: 0 9px 28px rgba(0,0,0,.17); }
.project-card { position: relative; overflow: hidden; }
.project-card::before { content:""; position: absolute; inset: 0 auto 0 0; width: 3px; background: var(--accent); }
.project-card--attention::before, .repo-card--danger { border-left: 3px solid var(--attention); }
.card-top, .release-card__top, .repo-top { display: flex; justify-content: space-between; align-items: flex-start; gap: 18px; }
.card-title h3, .repo-title h3 { overflow-wrap: anywhere; font-size: 20px; }
.card-title p, .repo-title p { margin: 5px 0 0; color: var(--faint); font: 10px ui-monospace,monospace; letter-spacing: .08em; text-transform: uppercase; }
.big-percent, .repo-percent { flex: none; color: var(--ok); font-size: 31px; line-height: .9; font-weight: 790; letter-spacing: -.05em; }
.big-percent small, .repo-percent small { display: block; margin-top: 8px; color: var(--faint); font: 9px ui-monospace,monospace; letter-spacing: .1em; text-align: right; text-transform: uppercase; }
.progress { height: 8px; margin: 17px 0 15px; overflow: hidden; border-radius: 99px; background: #071719; box-shadow: inset 0 0 0 1px rgba(46,91,96,.5); }
.progress__fill { display: block; height: 100%; border-radius: inherit; background: linear-gradient(90deg,#22b889,var(--ok)); box-shadow: 0 0 12px rgba(69,213,157,.35); }
.progress--release { height: 9px; margin: 11px 0 9px; }
.card-summary { display: flex; flex-wrap: wrap; gap: 7px 18px; color: var(--muted); font-size: 12px; }
.card-summary b { color: var(--fg); }
.attention-line { color: var(--attention); }

.repo-list { display: grid; grid-template-columns: repeat(3,minmax(0,1fr)); gap: 11px; }
.repo-card { padding: 16px; }
.repo-card:hover, .project-card:hover { border-color: var(--line-bright); transform: translateY(-1px); }
.lane-bar { display: flex; height: 22px; gap: 2px; padding: 2px; overflow: hidden; border: 1px solid var(--line); border-radius: 8px; background: #071719; }
.lane-seg { min-width: 3px; border-radius: 5px; background: var(--faint); }
.lane-seg--authoring { background: #647b7c; } .lane-seg--ready { background: var(--warn); }
.lane-seg--in-flight { background: var(--blue); } .lane-seg--verified { background: #6c8585; }
.lane-seg--archived { background: #315057; } .lane-empty { width: 100%; background: repeating-linear-gradient(90deg,#10282b 0 8px,#0c2023 8px 16px); }
.lane-counts { display: flex; flex-wrap: wrap; gap: 4px 12px; margin-top: 8px; color: var(--faint); font-size: 10px; }
.lane-counts b { color: var(--muted); font-weight: 650; }
.repo-metrics { display: grid; grid-template-columns: repeat(4,1fr); margin-top: 14px; padding-top: 13px; border-top: 1px solid var(--line); }
.repo-metric { min-width: 0; padding: 0 10px; border-right: 1px solid var(--line); }
.repo-metric:first-child { padding-left: 0; } .repo-metric:last-child { padding-right: 0; border-right: 0; }
.repo-metric span { display: block; color: var(--faint); font: 9px ui-monospace,monospace; letter-spacing: .06em; text-transform: uppercase; }
.repo-metric b { display: block; margin-top: 4px; overflow-wrap: anywhere; font-size: 13px; }
.repo-metric b.bad { color: var(--attention); } .repo-metric b.warn { color: var(--warn); }
details.disclosure { margin-top: 16px; border-top: 1px solid var(--line); }
details.disclosure summary { min-height: 44px; padding: 13px 2px 0; color: var(--muted); cursor: pointer; font-weight: 650; }

.badge, .chip, .stamp { display: inline-flex; align-items: center; width: fit-content; max-width: 100%; border-radius: 999px; white-space: normal; }
.badge { gap: 6px; padding: 4px 9px; border: 1px solid var(--line); color: var(--muted); background: rgba(7,23,25,.65); font: 700 9px ui-monospace,monospace; letter-spacing: .08em; text-transform: uppercase; }
.badge::before { content:""; flex: none; width: 6px; height: 6px; border-radius: 50%; background: currentColor; }
.badge--host { color: var(--ok); border-color: rgba(69,213,157,.35); background: var(--ok-soft); }
.badge--active { color: #83a8ff; border-color: rgba(95,145,255,.35); background: var(--blue-soft); }
.badge--blocked { color: var(--attention); border-color: rgba(255,113,108,.4); background: var(--attention-soft); }
.badge--claimed { color: var(--agent-fg); }
.chip { padding: 2px 7px; border: 1px solid var(--line); color: var(--muted); font: 700 9px ui-monospace,monospace; letter-spacing: .05em; text-transform: uppercase; }
.claimed { color: var(--agent-fg); background: rgba(82,105,106,.16); }
.stamp { padding: 3px 8px; border: 1px solid var(--line-bright); color: var(--host-fg); font: 650 10px ui-monospace,monospace; }
.stamp.host-verified { color: var(--ok); border-color: rgba(69,213,157,.45); }
.stamp.in-flight { color: #83a8ff; border-color: rgba(95,145,255,.45); }

.release-grid { display: grid; grid-template-columns: repeat(auto-fit,minmax(260px,1fr)); gap: 12px; }
.release-card { padding: 17px; border: 1px solid var(--line); border-radius: 15px; background: linear-gradient(145deg,rgba(17,45,49,.82),rgba(9,28,31,.9)); }
.release-card__top h3 { font-size: 17px; }
.release-card__product { margin: 5px 0 0; color: var(--faint); font: 9px ui-monospace,monospace; letter-spacing: .09em; text-transform: uppercase; }
.release-card__percent { color: var(--ok); font-size: 25px; line-height: 1; font-weight: 780; }
.release-card__meta { display: flex; justify-content: space-between; gap: 10px; color: var(--muted); font-size: 10px; }
.release-card__goal { margin: 12px 0 0; color: var(--muted); font-size: 12px; }
.release-detail { margin-top: 18px; padding: 19px; border: 1px solid var(--line); border-radius: var(--radius); background: rgba(13,36,40,.75); }

.roadmap-list { display: grid; gap: 8px; margin-top: 13px; }
.roadmap-row { display: grid; grid-template-columns: 42px minmax(180px,1.15fr) minmax(110px,.55fr) minmax(160px,1fr) minmax(190px,1.2fr); gap: 12px; align-items: center; min-width: 0; padding: 13px; border: 1px solid var(--line); border-left: 3px solid var(--faint); border-radius: var(--radius-sm); background: rgba(7,23,25,.5); }
.roadmap-row--host { border-left-color: var(--ok); } .roadmap-row--active { border-left-color: var(--blue); }
.roadmap-row--blocked { border-left-color: var(--attention); background: linear-gradient(90deg,var(--attention-soft),rgba(7,23,25,.55) 30%); }
.roadmap-step { color: var(--faint); font: 700 11px ui-monospace,monospace; }
.roadmap-title { min-width: 0; }
.roadmap-title strong, .roadmap-title a { display: block; overflow-wrap: anywhere; font-size: 14px; font-weight: 700; }
.roadmap-title small, .roadmap-cell small { display: block; margin-top: 3px; color: var(--faint); overflow-wrap: anywhere; }
.roadmap-cell { min-width: 0; color: var(--muted); font-size: 11px; overflow-wrap: anywhere; }
.roadmap-cell b { color: var(--fg); }
.dep-ok { color: var(--muted); } .dep-wait { color: var(--attention); }

.columns { display: grid; grid-template-columns: repeat(5,minmax(0,1fr)); gap: 10px; padding: 2px; }
.column { min-width: 0; min-height: 250px; padding: 11px; border: 1px solid var(--line); border-top: 2px solid var(--faint); border-radius: 15px; background: rgba(7,23,25,.48); }
.column:nth-child(2) { border-top-color: var(--warn); } .column:nth-child(3) { border-top-color: var(--blue); }
.column h2 { margin-bottom: 12px; color: var(--muted); font: 750 10px ui-monospace,monospace; letter-spacing: .1em; text-transform: uppercase; }
.card { min-width: 0; margin: 8px 0; padding: 11px; border: 1px solid var(--line); border-left: 3px solid var(--faint); border-radius: var(--radius-sm); background: var(--surface); }
.blocked-dep { border-left-color: var(--attention)!important; background: linear-gradient(90deg,var(--attention-soft),var(--surface) 38%); }
.blocked-human { border-left-color: var(--warn)!important; background: linear-gradient(90deg,var(--warn-soft),var(--surface) 38%); }
.agent { padding: 9px 10px; border: 1px dashed #395154; border-radius: 9px; color: var(--agent-fg); background: var(--agent-bg); }
.agent h3 { margin: 7px 0 8px; color: #d4dfdd; line-height: 1.3; overflow-wrap: anywhere; }
.agent p { margin: 4px 0 0; font-size: 11px; overflow-wrap: anywhere; }
.owner-chips { display: flex; flex-wrap: wrap; gap: 5px; margin-top: 9px; }
.attention { color: var(--attention)!important; } .warn { color: var(--warn)!important; }
.dag-wrap { overflow: hidden; border: 1px solid var(--line); border-radius: var(--radius-sm); background: rgba(7,23,25,.45); }
.svg-layer { display: block; width: 100%; height: 130px; color: var(--muted); border: 0; margin: 0; }
.edge-required { stroke: var(--muted); stroke-width: 2; marker-end: url(#arrow); }
.edge-hover { opacity: 0; } .card:hover ~ .edge-hover, .edge-hover:hover { opacity: .35; }

.host-seal { margin: 10px 0; padding: 13px; overflow-x: auto; border: 1px solid var(--line-bright); border-left: 3px solid var(--line-bright); border-radius: var(--radius-sm); color: var(--host-fg); background: var(--host-bg); }
.host-seal.host-ok { border-left-color: var(--ok); background: linear-gradient(90deg,var(--ok-soft),var(--host-bg) 30%); }
.host-seal.host-bad, .rejected { border-left-color: var(--attention); background: linear-gradient(90deg,var(--attention-soft),var(--host-bg) 30%); }
.host-seal.host-unknown { border-left-color: var(--faint); }
.record-hero { display: grid; grid-template-columns: minmax(0,1fr) auto; gap: 24px; align-items: end; margin-bottom: 24px; }
.record-title { min-width: 0; } .record-title h1 { overflow-wrap: anywhere; }
.record-verdict { text-align: right; } .record-verdict strong { display: block; font-size: 30px; }
.claim-grid { display: grid; grid-template-columns: repeat(2,minmax(0,1fr)); gap: 8px; margin-bottom: 24px; }
.record-sections { display: grid; grid-template-columns: repeat(2,minmax(0,1fr)); gap: 14px; align-items: start; }
.record-section { min-width: 0; padding: 18px; border: 1px solid var(--line); border-radius: var(--radius); background: rgba(13,36,40,.75); }
.record-section h2 { margin-bottom: 13px; font-size: 16px; } .record-section--wide { grid-column: 1/-1; }
.timeline { display: flex; gap: 8px; overflow-x: auto; }
.segment { min-width: 180px; padding: 9px; border: 1px solid var(--line); border-radius: 10px; }
.number { margin: 8px 0; font-size: 42px; font-weight: 800; }
.stub { background: repeating-linear-gradient(45deg,transparent,transparent 6px,rgba(255,113,108,.15) 6px,rgba(255,113,108,.15) 12px); }
.grid { display: grid; grid-template-columns: repeat(auto-fit,minmax(220px,1fr)); gap: 10px; }
.finding-list { margin: 0; padding-left: 20px; color: var(--attention); }

@media (max-width: 1100px) {
  .repo-list { grid-template-columns: repeat(2,minmax(0,1fr)); }
  .columns { grid-template-columns: repeat(2,minmax(0,1fr)); }
  .roadmap-row { grid-template-columns: 36px minmax(160px,1fr) minmax(100px,.55fr) minmax(150px,1fr); }
  .roadmap-row .roadmap-cell:last-child { grid-column: 2/-1; }
}
@media (max-width: 760px) {
  body { padding-bottom: calc(var(--tab-h) + env(safe-area-inset-bottom)); }
  main { padding: 24px 15px 42px; }
  .site-header__inner, .footer-inner { padding-left: 15px; padding-right: 15px; }
  .site-mode { font-size: 0; } .site-mode::after { content:"Offline"; font-size: 9px; }
  .page-head { display: block; margin-bottom: 22px; } .page-head__aside { margin-top: 16px; }
  .hero-metrics { display: grid; grid-template-columns: repeat(2,1fr); }
  .hero-metric { padding: 15px; } .hero-metric--lead { grid-column: 1/-1; }
  .hero-metric:last-child { grid-column: 1/-1; }
  .project-grid, .repo-list, .columns, .claim-grid, .record-sections { grid-template-columns: 1fr; }
  .column { min-height: 0; }
  .project-card, .panel { padding: 17px; }
  .repo-metrics { grid-template-columns: repeat(2,1fr); }
  .repo-metric:nth-child(2) { border-right: 0; }
  .repo-metric:nth-child(n+3) { margin-top: 10px; padding-top: 10px; border-top: 1px solid var(--line); }
  .roadmap-row { grid-template-columns: 30px minmax(0,1fr); gap: 8px 10px; align-items: start; }
  .roadmap-row .roadmap-cell { grid-column: 2; }
  .record-hero { display: block; } .record-verdict { margin-top: 16px; text-align: left; }
  .record-section--wide { grid-column: auto; }
  .timeline { display: grid; overflow: visible; } .segment { min-width: 0; }
  .bottom-nav { position: fixed; inset: auto 0 0; z-index: 40; display: grid; grid-template-columns: repeat(3,1fr); min-height: var(--tab-h); padding: 6px max(8px,env(safe-area-inset-right)) calc(6px + env(safe-area-inset-bottom)) max(8px,env(safe-area-inset-left)); border-top: 1px solid var(--line-bright); background: rgba(6,18,21,.96); backdrop-filter: blur(18px); }
  .bottom-nav a { min-height: 48px; display: grid; place-items: center; align-content: center; gap: 1px; border-radius: 10px; color: var(--muted); text-decoration: none; font-size: 10px; }
  .bottom-nav a b { color: var(--fg); font-size: 16px; line-height: 1; }
  footer { display: none; }
}
@media (max-width: 410px) {
  h1 { font-size: 34px; }
  .card-top, .release-card__top { gap: 10px; }
  .big-percent { font-size: 27px; }
}
"""


class OperationalExit(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def _h(value) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _redact(value) -> str:
    text = "" if value is None else str(value)
    for _label, pattern in SECRET_PATTERNS:
        text = pattern.sub("[redacted]", text)
    return text


def _shown(value, redact: bool = False) -> str:
    return _redact(value) if redact else ("" if value is None else str(value))


def _mapping(value) -> dict:
    return value if isinstance(value, dict) else {}


def _safe_int(value) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _load_yaml_text(text: str):
    try:
        if not _YAML_IS_PYYAML and text.lstrip().startswith(("{", "[")):
            return json.loads(text)
        return yaml.safe_load(text)
    except Exception:
        return None


def _load_yaml(path: Path):
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    data = _load_yaml_text(text)
    return data if isinstance(data, dict) else None


def _as_list(value):
    return value if isinstance(value, list) else []


def _first(*values, default=""):
    for value in values:
        if value not in (None, ""):
            return value
    return default


def _rel(from_path: Path, to_path: Path) -> str:
    return os.path.relpath(to_path, from_path.parent).replace(os.sep, "/")


# Kept OUT of the f-string expression below deliberately: a backslash inside an f-string
# EXPRESSION is a syntax error before Python 3.12 (PEP 701 relaxed it), and pyproject
# declares requires-python = ">=3.11". Inlining the escaped quotes made this whole module
# fail to IMPORT on 3.11 -- taking `make gate` and `isanna record build` down with it.
# These fallbacks live at module scope because a backslash inside an f-string EXPRESSION is
# a syntax error before Python 3.12 (PEP 701 relaxed it), and pyproject declares
# requires-python = ">=3.11". Escaped inline, they made this module fail to IMPORT on 3.11.
_QUIET_NO_DECOMPOSITION = '<p class="quiet">accepted, not yet decomposed</p>'
_QUIET_NO_ACTIVE_RELEASES = '<p class="quiet">No active releases.</p>'
_QUIET_NO_RELEASE_SPECS = '<p class="quiet">No specs declared for this release.</p>'
_QUIET_NO_RELEASE_TARGETS = '<p class="quiet">No active release targets.</p>'
_QUIET_AUTHOR_A_RELEASE = '<p class="quiet">Author a release target to populate the roadmap.</p>'
_QUIET_NO_PRODUCTS = '<p class="quiet">No declared products in this workspace.</p>'


_EMPTY_INTENT_BACKLOG = '<p class="quiet">No intent backlog objects declared.</p>'


def _page(title: str, body: str, *, root_href: str = "index.html", context_label: str = "Current") -> str:
    shell = (
        "<!doctype html><html><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        "<title></title><style></style></head><body>"
        "<main></main><footer><span class=\"legend\"></span></footer></body></html>"
    )
    # Inspect only recorder-owned template copy.  Agent text is allowed to contain
    # these phrases; it is escaped and rendered in the AGENT register, not treated
    # as generator copy or as an operational failure.
    lowered = (shell + CSS + LEGEND + CHAIN_NOTE).lower()
    for token in FORBIDDEN:
        if token in lowered:
            raise RuntimeError(f"forbidden string in generated HTML: {token}")
    return (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1, viewport-fit=cover\">"
        "<meta name=\"theme-color\" content=\"#061215\"><meta name=\"mobile-web-app-capable\" content=\"yes\">"
        "<meta name=\"apple-mobile-web-app-capable\" content=\"yes\">"
        "<meta name=\"apple-mobile-web-app-status-bar-style\" content=\"black-translucent\">"
        "<link rel=\"manifest\" href=\"data:application/manifest+json,%7B%22name%22%3A%22The%20Record%22%2C%22short_name%22%3A%22Record%22%2C%22display%22%3A%22standalone%22%2C%22theme_color%22%3A%22%23061215%22%2C%22background_color%22%3A%22%23061215%22%7D\">"
        f"<title>{_h(title)}</title><style>{CSS}</style></head><body>"
        '<header class="site-header"><div class="site-header__inner">'
        f'<a class="brand" href="{_h(root_href)}"><svg class="brand-mark" viewBox="0 0 32 32" aria-hidden="true">'
        '<g transform="translate(16 16)" fill="none" stroke="currentColor" stroke-linecap="round">'
        '<circle r="12" stroke-width="1.4" opacity=".42"/><circle r="7.3" stroke-width="1.4" opacity=".62"/>'
        '<path d="M0 0V-12" stroke-width="2.2"/><circle r="2.4" fill="currentColor" stroke="none"/>'
        '<circle cx="6.2" cy="-6.8" r="1.8" fill="currentColor" stroke="none"/></g></svg>'
        '<span class="brand-text"><small>Isanna</small><span>The Record</span></span></a>'
        '<span class="site-mode">Static · read only</span></div></header>'
        f"<main id=\"content\">{body}</main>"
        '<footer id="trust"><div class="footer-inner">'
        f'<span class="legend">{LEGEND}</span><span class="footer-note">Host-observed truth · zero network</span>'
        '</div></footer>'
        '<nav class="bottom-nav" aria-label="Page navigation">'
        f'<a href="{_h(root_href)}"><b>⌂</b>Projects</a>'
        f'<a href="#content"><b>◎</b>{_h(context_label)}</a>'
        '<a href="#trust"><b>◇</b>Trust</a></nav></body></html>'
    )


def _write(out_root: Path, rel_path: str, content: str) -> Path:
    out_root = out_root.resolve()
    dest = (out_root / rel_path).resolve()
    try:
        dest.relative_to(out_root)
    except ValueError as exc:
        raise OperationalExit(f"refusing to write outside --out: {dest}") from exc
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(content, encoding="utf-8")
    return dest


def _write_file(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _recordignore(root: Path) -> list[str]:
    patterns: list[str] = []
    for path in (root / ".recordignore", runtime_dir(root) / ".recordignore"):
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    patterns.append(line)
        except OSError:
            pass
    return patterns


def _ignored(spec_id: str, rel_path: str, patterns: list[str]) -> bool:
    if spec_id.startswith("ab-bench-"):
        return True
    return any(fnmatch.fnmatch(spec_id, pat) or fnmatch.fnmatch(rel_path, pat) for pat in patterns)


def _spec_dirs(root: Path) -> dict[str, Path]:
    specs_root = runtime_dir(root) / "specs"
    patterns = _recordignore(root)
    out: dict[str, Path] = {}
    if not specs_root.is_dir():
        return out
    for entry in sorted(specs_root.iterdir(), key=lambda p: p.name):
        if entry.name == "archive" and entry.is_dir():
            for archived in sorted(entry.iterdir(), key=lambda p: p.name):
                if archived.is_dir() and not _ignored(archived.name, f"archive/{archived.name}", patterns):
                    out.setdefault(archived.name, archived)
            continue
        if entry.is_dir() and not _ignored(entry.name, entry.name, patterns):
            out.setdefault(entry.name, entry)
    return out


def _report_specs(report: dict, allowed: set[str]) -> list[dict]:
    return [s for s in _as_list(report.get("specs")) if isinstance(s, dict) and s.get("spec") in allowed]


def _queue_roots(report: dict) -> list[Path]:
    return [Path(p) for p in _as_list(report.get("queue_roots")) if isinstance(p, (str, os.PathLike))]


def _attempt_records(report: dict) -> list[dict]:
    records: dict[str, dict] = {}
    for queue_root in _queue_roots(report):
        attempts = queue_root / "queue" / "attempts"
        if not attempts.is_dir():
            continue
        for path in sorted(attempts.iterdir(), key=lambda p: p.name):
            if path.suffix.lower() not in (".yaml", ".yml", ".json"):
                continue
            data = _load_yaml(path)
            if not isinstance(data, dict):
                continue
            attempt_id = str(data.get("attempt_id") or path.stem)
            data["_path"] = str(path)
            records.setdefault(attempt_id, data)
    return sorted(
        records.values(),
        key=lambda d: str(d.get("created_at") or _mapping(d.get("metadata")).get("started_at") or ""),
    )


def _queue_items(report: dict) -> list[dict]:
    items: dict[str, dict] = {}
    for queue_root in _queue_roots(report):
        item_dir = queue_root / "queue" / "items"
        if not item_dir.is_dir():
            continue
        for path in sorted(item_dir.iterdir(), key=lambda p: p.name):
            if path.suffix.lower() not in (".yaml", ".yml", ".json"):
                continue
            data = _load_yaml(path)
            if not isinstance(data, dict):
                continue
            item_id = str(data.get("id") or path.stem)
            data["_path"] = str(path)
            items.setdefault(item_id, data)
    return list(items.values())


def _spec_id_from_item(item: dict) -> str:
    task_ref = item.get("task_ref") if isinstance(item.get("task_ref"), dict) else {}
    return str(item.get("spec_id") or task_ref.get("spec_id") or "")


def _is_in_flight_item(item: dict) -> bool:
    return str(item.get("state") or "").upper() in IN_FLIGHT_STATES


def _blocked_items(items: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = defaultdict(list)
    for item in items:
        state = str(item.get("state") or item.get("status") or "").upper()
        if state in {"BLOCKED_DEP", "BLOCKED_HUMAN"}:
            spec_id = _spec_id_from_item(item)
            if spec_id:
                out[spec_id].append(item)
    return out


def _spec_info(spec_dir: Path | None) -> dict:
    data = _load_yaml(spec_dir / "spec.yaml") if spec_dir else None
    return data if isinstance(data, dict) else {}


def _status_bucket(status: str, in_flight: bool) -> str:
    if in_flight:
        return "In flight"
    status = str(status or "").strip()
    for name, statuses in BUCKETS:
        if status in statuses:
            return name
    return "Authoring"


def _intent_card(intent_row, *, diagnostic: bool = False) -> str:
    if diagnostic:
        findings = "".join(f"<p class=\"attention\">{_h(item)}</p>" for item in intent_row.findings)
        return (
            '<div class="card blocked-dep"><div class="agent"><span class="chip claimed">claimed</span>'
            f'<h3>{_h(intent_row.path)}</h3><p>invalid intent file</p>{findings}</div></div>'
        )
    intent = intent_row.intent
    criteria = "".join(f"<li>{_h(item.id)}: {_h(item.statement)}</li>" for item in intent.success_criteria)
    non_goals = "".join(f"<li>{_h(item)}</li>" for item in intent.non_goals)
    delta_parts = []
    for category, items in intent.ssot_delta.items():
        joined = ", ".join(f"{item.target}:{item.change}" for item in items) or "none"
        delta_parts.append(f"<li>{_h(category)}: {_h(joined)}</li>")
    members = "".join(f"<li>{_h(member.canonical_ref)}" + (f" ({_h(member.status or 'unknown')})" if member.status else "") + "</li>" for member in intent_row.members)
    findings = "".join(f"<p class=\"attention\">{_h(item)}</p>" for item in intent_row.findings)
    return (
        '<div class="card"><div class="agent"><span class="chip claimed">claimed</span>'
        f'<h3>{_h(intent.title)}</h3>'
        f'<p>intent: {_h(intent.intent)} · lifecycle: {_h(intent_row.visible_state)}</p>'
        f'<p>problem: {_h(intent.problem)}</p>'
        f'<p>why: {_h(intent.why)}</p>'
        f'<p>path: {_h(intent.repo_relpath)}</p>'
        f'<p>members: {len(intent.specs)}</p>'
        f'<h3>Success criteria</h3><ul>{criteria}</ul>'
        f'<h3>Non-goals</h3><ul>{non_goals}</ul>'
        f'<h3>Declared delta</h3><ul>{"".join(delta_parts)}</ul>'
        f'<h3>Member specs</h3><ul>{members or "<li>none</li>"}</ul>'
        f'{findings}'
        '</div></div>'
    )


def _backlog_capability_panel(index: dict, diagnostics: list[str], *, redact: bool = False) -> str:
    """Render declared backlog capability state in the agent/claimed register only."""
    cards = []
    collision_count = 0
    for target, owners in index.items():
        collided = len(owners.collision_intent_ids) >= 2
        collision_count += int(collided)
        owner_rows = "".join(
            "<li>"
            f"intent <strong>{_h(_shown(row.intent_id, redact))}</strong> · "
            f"release {_h(_shown(row.release_id, redact))} · "
            f"lifecycle {_h(_shown(row.visible_state, redact))} · "
            f"change {_h(_shown(row.change, redact))}"
            "</li>"
            for row in owners.rows
        )
        collision_badge = '<span class="chip attention">collision</span>' if collided else ""
        cards.append(
            f'<article class="card{" blocked-dep" if collided else ""}"><div class="agent">'
            f'<span class="chip claimed">claimed</span>{collision_badge}'
            f'<h3>{_h(_shown(target, redact))}</h3><ul>{owner_rows}</ul></div></article>'
        )
    diagnostic_cards = "".join(
        '<article class="card blocked-dep"><div class="agent">'
        '<span class="chip claimed">claimed planning finding</span>'
        f'<p class="attention">{_h(_shown(finding, redact))}</p></div></article>'
        for finding in diagnostics
    )
    if not cards and not diagnostic_cards:
        return ""
    empty = '<p class="quiet">No active backlog capability touches declared.</p>' if not cards else ""
    return (
        '<section class="panel backlog-capabilities"><div class="panel-head"><div>'
        '<p class="section-kicker">Active backlog · claimed register</p>'
        '<h2>Declared capability touches</h2>'
        '<p>Planning declarations only; these are not host verification verdicts.</p></div>'
        f'<span class="badge badge--claimed">{collision_count} collisions</span></div>'
        f'{"".join(cards)}{diagnostic_cards}{empty}</section>'
    )


def _verification_for(report: dict, spec_id: str):
    for row in _as_list(report.get("specs")):
        if isinstance(row, dict) and row.get("spec") == spec_id:
            return row.get("verification") or "-"
    return "unknown"


def _stamp(text) -> str:
    return f"<span class=\"stamp\">{_h(text or 'unknown')}</span>"


def _agent_claim(label: str, value, *, redact: bool = False) -> str:
    return (
        "<div class=\"agent\">"
        f"<span class=\"chip claimed\">claimed</span> <strong>{_h(label)}</strong>: "
        f"{_h(_shown(value if value not in (None, '') else '—', redact))}"
        "</div>"
    )


def _command(value) -> str:
    if isinstance(value, list):
        return json.dumps(value, ensure_ascii=False, default=str)
    return str(value or "")


def _attempt_id(attempt: dict) -> str:
    if attempt.get("attempt_id") not in (None, ""):
        return str(attempt["attempt_id"])
    path = attempt.get("_path")
    return Path(path).stem if isinstance(path, str) and path else ""


def _bundle_sha(data: dict) -> str:
    cleaned = dict(data)
    cleaned.pop("bundle_sha256", None)
    encoded = json.dumps(cleaned, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _evidence_files(gate_dir: Path) -> list[Path]:
    paths: list[Path] = []
    for base, dirs, files in os.walk(gate_dir, followlinks=False):
        dirs[:] = sorted(d for d in dirs if not (Path(base) / d).is_symlink())
        for name in sorted(files):
            paths.append(Path(base) / name)
    return paths


def _bundle_authentication(spec_dir: Path, attempts: list[dict]) -> tuple[list[dict], list[str]]:
    """Return host-authenticated bundles and reasoned AGENT-register warnings.

    A disk file is never host evidence by location alone.  Authentication requires
    an attempt reference, confinement, the recomputed digest, and matching spec /
    attempt identity.
    """
    gate_dir = spec_dir / "gate-evidence"
    authenticated: list[dict] = []
    warnings: list[str] = []
    referenced_lexical: set[str] = set()
    authenticated_lexical: set[str] = set()
    seen_auth: set[str] = set()

    gate_dir_symlink = gate_dir.is_symlink()
    gate_root: Path | None = None
    if gate_dir_symlink:
        warnings.append("gate-evidence directory is a symlink")
    elif gate_dir.is_dir():
        try:
            gate_root = gate_dir.resolve(strict=True)
        except OSError:
            warnings.append("gate-evidence directory cannot be resolved")

    for attempt in attempts:
        if not isinstance(attempt, dict):
            continue
        metadata = _mapping(attempt.get("metadata"))
        attempt_id = _attempt_id(attempt)
        for ref in _as_list(metadata.get("gate_evidence")):
            if not isinstance(ref, dict):
                warnings.append("attempt gate_evidence entry is not a mapping")
                continue
            raw_path = ref.get("path")
            expected_sha = ref.get("sha256")
            if not isinstance(raw_path, str) or not raw_path.strip():
                warnings.append("attempt gate_evidence entry has no path")
                continue
            raw = Path(raw_path)
            candidate = spec_dir / raw
            lexical_key = os.path.abspath(candidate)
            if not raw.is_absolute() and ".." not in raw.parts and raw.parts and raw.parts[0] == "gate-evidence":
                referenced_lexical.add(lexical_key)
            if raw.is_absolute():
                warnings.append(f"{raw_path}: absolute paths are forbidden")
                continue
            if ".." in raw.parts:
                warnings.append(f"{raw_path}: path traversal is forbidden")
                continue
            if not raw.parts or raw.parts[0] != "gate-evidence":
                warnings.append(f"{raw_path}: path is outside gate-evidence")
                continue
            if gate_dir_symlink:
                warnings.append(f"{raw_path}: gate-evidence directory is a symlink")
                continue
            if gate_root is None:
                warnings.append(f"{raw_path}: gate-evidence directory is unavailable")
                continue
            try:
                resolved = candidate.resolve(strict=True)
                resolved.relative_to(gate_root)
            except FileNotFoundError:
                warnings.append(f"{raw_path}: referenced bundle is missing")
                continue
            except (OSError, ValueError):
                warnings.append(f"{raw_path}: resolved path escapes gate-evidence")
                continue
            if not resolved.is_file():
                warnings.append(f"{raw_path}: referenced path is not a file")
                continue
            if not isinstance(expected_sha, str) or not expected_sha:
                warnings.append(f"{raw_path}: attempt reference has no sha256")
                continue
            data = _load_yaml(resolved)
            if not isinstance(data, dict):
                warnings.append(f"{raw_path}: bundle is not a YAML mapping")
                continue
            try:
                actual_sha = _bundle_sha(data)
            except (TypeError, ValueError, OverflowError):
                warnings.append(f"{raw_path}: bundle digest cannot be computed")
                continue
            if actual_sha != expected_sha:
                warnings.append(f"{raw_path}: sha256 mismatch")
                continue
            metadata_spec = metadata.get("spec_id")
            if data.get("spec_id") != metadata_spec or metadata_spec != spec_dir.name:
                warnings.append(f"{raw_path}: bundle names a different spec")
                continue
            bundle_attempt = data.get("attempt_id")
            if bundle_attempt not in (None, "") and str(bundle_attempt) != attempt_id:
                warnings.append(f"{raw_path}: bundle names a different attempt")
                continue
            if lexical_key in seen_auth:
                continue
            seen_auth.add(lexical_key)
            authenticated_lexical.add(lexical_key)
            authenticated.append({"path": resolved, "data": data, "attempt": attempt})

    if gate_root is not None:
        for path in _evidence_files(gate_dir):
            lexical_key = os.path.abspath(path)
            if lexical_key not in authenticated_lexical and lexical_key not in referenced_lexical:
                warnings.append(f"{path.relative_to(spec_dir)}: unreferenced bundle")
    return authenticated, warnings


def _unauthenticated_warning(reason: str, *, redact: bool = False) -> str:
    message = f"unauthenticated — not referenced by any host record ({_shown(reason, redact)})"
    return _agent_claim("warning", message)


def _host_evidence_blocks(authenticated: list[dict], warnings: list[str], *, redact: bool = False) -> tuple[str, bool]:
    blocks = []
    for record in authenticated:
        data = _mapping(record.get("data"))
        verdict = str(data.get("verdict") or "").strip().lower()
        cls = "host-ok" if verdict == "pass" else "host-bad" if verdict == "fail" else "host-unknown"
        exit_code = _safe_int(data.get("exit_code"))
        polarity = str(data.get("polarity") or "unknown").strip().lower()
        lines = [
            f"HOST-EXECUTED {data.get('started_at') or ''}",
            f"gate: {data.get('gate') or 'unknown'}",
            f"verdict: {verdict if verdict in {'pass', 'fail'} else 'unknown'}",
            f"polarity: {polarity or 'unknown'}",
            f"mode: {data.get('mode') if data.get('mode') not in (None, '') else 'unknown'}",
            f"blocking: {data.get('blocking') if isinstance(data.get('blocking'), bool) else 'unknown'}",
            f"failure_class: {data.get('failure_class') if data.get('failure_class') not in (None, '') else '—'}",
            f"command: {_command(data.get('command'))}",
            f"exit_code: {exit_code if exit_code is not None else 'unknown'}",
            f"duration_ms: {_safe_int(data.get('duration_ms')) if _safe_int(data.get('duration_ms')) is not None else 'unknown'}",
            f"git_head_sha: {data.get('git_head_sha') or 'unknown'}",
            f"diff_stat: {json.dumps(data.get('diff_stat') if isinstance(data.get('diff_stat'), dict) else {}, sort_keys=True, default=str)}",
        ]
        if polarity == "red":
            lines.append("RED POLARITY: the command is EXPECTED to fail; failing is the pass condition")
        for key in ("stdout_tail", "stderr_tail", "diff_patch_tail"):
            if data.get(key) not in (None, ""):
                lines.append(f"{key}:")
                lines.append(str(data.get(key)))
        rendered = _shown(chr(10).join(lines), redact)
        blocks.append(f"<section class=\"host-seal {cls}\"><pre>{_h(rendered)}</pre></section>")
    blocks.extend(_unauthenticated_warning(reason, redact=redact) for reason in warnings)
    if not blocks:
        return _agent_claim("host evidence", "No authenticated host evidence bundles visible."), False
    return "\n".join(blocks), bool(authenticated)


def _acceptance_items(spec_dir: Path) -> list[dict]:
    requirements = _load_yaml(spec_dir / "requirements.yaml")
    if not isinstance(requirements, dict):
        return []
    result = []
    for requirement in _as_list(requirements.get("requirements")):
        if not isinstance(requirement, dict):
            continue
        result.extend(item for item in _as_list(requirement.get("acceptance")) if isinstance(item, dict))
    return result


def _oracle_type(ac: dict) -> str:
    oracle = ac.get("oracle")
    if isinstance(oracle, dict):
        oracle = oracle.get("type")
    return str(oracle or ac.get("oracle_type") or "").strip().lower()


def _recorded_commands(bundle: dict) -> list[tuple[str, int | None]]:
    def normalized(value) -> str:
        if isinstance(value, list):
            if len(value) >= 3 and value[-2] == "-c":
                return str(value[-1]).strip()
            return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
        return str(value or "").strip()

    records = [(normalized(bundle.get("command")), _safe_int(bundle.get("exit_code")))]
    for item in _as_list(bundle.get("commands")):
        if isinstance(item, dict):
            records.append((normalized(item.get("command")), _safe_int(item.get("exit_code"))))
    return [(command, code) for command, code in records if command]


def _bundle_task_ids(bundle: dict) -> set[str]:
    ids = {str(item) for item in _as_list(bundle.get("task_ids")) if item not in (None, "")}
    if bundle.get("task_id") not in (None, ""):
        ids.add(str(bundle["task_id"]))
    return ids


def _host_anchored_acceptance_ids(spec_dir: Path, authenticated: list[dict]) -> set[str]:
    tasks = _load_yaml(spec_dir / "tasks.yaml")
    if not isinstance(tasks, dict):
        return set()
    successful: dict[str, set[str]] = defaultdict(set)
    for record in authenticated:
        bundle = _mapping(record.get("data"))
        for task_id in _bundle_task_ids(bundle):
            for command, exit_code in _recorded_commands(bundle):
                if exit_code == 0:
                    successful[task_id].add(command)
    anchored: set[str] = set()
    for task in _as_list(tasks.get("tasks")):
        if not isinstance(task, dict):
            continue
        task_id = str(task.get("id") or "")
        if not task_id or task_id not in successful:
            continue
        for verify in _as_list(task.get("verify")):
            if not isinstance(verify, dict):
                continue
            command = str(verify.get("command") or "").strip()
            if command and command in successful[task_id]:
                anchored.update(str(ac_id) for ac_id in _as_list(verify.get("proves")) if ac_id not in (None, ""))
    return anchored


def _integrity(spec_dir: Path, authenticated: list[dict]) -> tuple[str, str, bool]:
    acs = _acceptance_items(spec_dir)
    if not acs:
        return "—", "no structured acceptance criteria", False
    must = []
    human_only = 0
    for ac in acs:
        if _oracle_type(ac) == "human_only":
            human_only += 1
            continue
        priority = str(ac.get("priority") or "").strip().lower()
        if priority == "must":
            must.append(ac)
    anchored_ids = _host_anchored_acceptance_ids(spec_dir, authenticated)
    proven = sum(1 for ac in must if str(ac.get("id") or "") in anchored_ids)
    suffix = f" +{human_only} human-only" if human_only else ""
    if proven:
        return f"{proven}/{len(must)}", f"must-criteria host-anchored{suffix}", True
    return f"0/{len(must)}", f"must-criteria claimed; no authenticated host command records{suffix}", False


def _phase_timeline(spec_dir: Path, attempts: list[dict], *, redact: bool = False) -> str:
    phase_log = _load_yaml(spec_dir / "phase-log.yaml") or {}
    parts = []
    for phase in _as_list(phase_log.get("phases")):
        if not isinstance(phase, dict):
            continue
        parts.append(
            "<div class=\"segment agent\"><span class=\"chip claimed\">claimed</span><br>"
            f"<strong>{_h(_shown(phase.get('phase'), redact))}</strong><br>"
            f"duration: {_h(_shown(phase.get('duration_ms') or '—', redact))}<br>"
            f"lane: {_h(_shown(phase.get('lane') or '—', redact))}<br>"
            f"model: {_h(_shown(phase.get('used_model') or phase.get('model') or '—', redact))}<br>"
            f"outcome: {_h(_shown(phase.get('outcome') or '—', redact))}"
            "</div>"
        )
    for attempt in attempts:
        metadata = attempt.get("metadata") if isinstance(attempt.get("metadata"), dict) else {}
        gates = metadata.get("gates") if isinstance(metadata.get("gates"), dict) else {}
        rejected = any(str(v).startswith("fail:") for v in gates.values()) or metadata.get("decision") not in {"phase-complete", None, ""}
        cls = "segment stub rejected" if rejected else "segment"
        parts.append(
            f"<div class=\"{cls}\"><strong>{_h(_shown(metadata.get('phase'), redact))}</strong><br>"
            f"duration: {_h(_shown(metadata.get('cli_duration_ms') or metadata.get('duration_ms') or '—', redact))}<br>"
            f"lane: {_h(_shown(metadata.get('lane') or attempt.get('lane') or '—', redact))}<br>"
            f"model: {_h(_shown(metadata.get('model') or metadata.get('used_model') or '—', redact))}<br>"
            f"outcome: {_h(_shown(metadata.get('decision') or '—', redact))}</div>"
        )
    return "<div class=\"timeline\">" + "".join(parts or ["<p class=\"agent\">No phase timeline visible.</p>"]) + "</div>"


def _gate_ledger(attempts: list[dict], *, redact: bool = False) -> str:
    rows = []
    for attempt in attempts:
        metadata = attempt.get("metadata") if isinstance(attempt.get("metadata"), dict) else {}
        gates = metadata.get("gates") if isinstance(metadata.get("gates"), dict) else {}
        rejected = any(str(v).startswith("fail:") for v in gates.values()) or metadata.get("decision") not in {"phase-complete", None, ""}
        row_class = " class=\"rejected\"" if rejected else ""
        rows.append(
            f"<tr{row_class}><td>{_h(_shown(attempt.get('created_at') or metadata.get('started_at'), redact))}</td>"
            f"<td>{_h(_shown(metadata.get('lane') or attempt.get('lane') or '—', redact))}</td>"
            f"<td>{_h(_shown(metadata.get('decision') or '—', redact))}{' rejected' if rejected else ''}</td>"
            + "".join(f"<td><code>{_h(_shown(gates.get(gate, 'unknown'), redact))}</code></td>" for gate in GATES)
            + "</tr>"
        )
    head = "<tr><th>timestamp</th><th>lane</th><th>decision</th>" + "".join(f"<th>{g}</th>" for g in GATES) + "</tr>"
    return "<div class=\"table-wrap\"><table>" + head + "".join(rows) + "</table></div>"


def _tdd_ledger(spec_dir: Path, *, redact: bool = False) -> str:
    evidence = spec_dir / "evidence"
    if not evidence.is_dir():
        return "<p class=\"agent\">No agent-written TDD evidence visible.</p>"
    rows = []
    for path in sorted(evidence.glob("task-*.yaml")) + sorted(evidence.glob("task-*.yml")):
        data = _load_yaml(path) or {}
        entries = _as_list(data.get("entries")) or [data]
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            rows.append(
                "<tr class=\"agent\"><td><span class=\"chip claimed\">claimed</span></td>"
                f"<td>{_h(_shown(data.get('task_id') or path.stem, redact))}</td>"
                f"<td>{_h(_shown(entry.get('step') or entry.get('id') or '—', redact))}</td>"
                f"<td><code>{_h(_shown(entry.get('command') or '—', redact))}</code></td>"
                f"<td>{_h(_shown(entry.get('exit_code') if entry.get('exit_code') is not None else '—', redact))}</td></tr>"
            )
    if not rows:
        return "<p class=\"agent\">No agent-written TDD evidence visible.</p>"
    return (
        "<div class=\"table-wrap\"><table><tr><th>register</th><th>task</th><th>step</th><th>command</th><th>exit</th></tr>"
        + "".join(rows)
        + "</table></div>"
    )


def _cost(attempts: list[dict]) -> str:
    tin = tout = wall = 0
    for attempt in attempts:
        metadata = attempt.get("metadata") if isinstance(attempt.get("metadata"), dict) else {}
        for key in ("tokens_in", "input_tokens", "plan_tokens_in"):
            try:
                tin += int(metadata.get(key) or 0)
            except (TypeError, ValueError):
                pass
        for key in ("tokens_out", "output_tokens", "plan_tokens_out"):
            try:
                tout += int(metadata.get(key) or 0)
            except (TypeError, ValueError):
                pass
        for key in ("duration_ms", "cli_duration_ms"):
            try:
                wall += int(metadata.get(key) or 0)
            except (TypeError, ValueError):
                pass
    return f"<p class=\"mono\">tokens: in {tin}, out {tout} · wall-clock ms: {wall}</p>"


def _chain(row: dict, has_bundles: bool, *, redact: bool = False) -> str:
    chain = row.get("chain") if isinstance(row.get("chain"), dict) else {}
    if not has_bundles and not chain.get("bundles"):
        return "<p class=\"agent\">No gate-evidence chain visible.</p>"
    violations = _as_list(chain.get("violations"))
    if chain.get("checked") is True and not violations:
        count = _safe_int(chain.get("bundles"))
        line = f"intact ({count if count is not None else 'unknown'} bundles)"
    elif violations:
        line = "violations: " + "; ".join(str(v) for v in violations)
    else:
        line = "unknown"
    return f"<div class=\"host-seal\"><pre>{_h(_shown(line, redact))}</pre></div><p>{_h(CHAIN_NOTE)}</p>"


def _run_record(
    repo_slug: str,
    root: Path,
    report: dict,
    spec_id: str,
    spec_dir: Path,
    out_rel_prefix: str = "../..",
    *,
    redact: bool = False,
) -> str:
    info = _spec_info(spec_dir)
    row = next(
        (s for s in _as_list(report.get("specs")) if isinstance(s, dict) and s.get("spec") == spec_id),
        {"verification": "unknown", "chain": {"checked": False}},
    )
    attempts = [a for a in _attempt_records(report) if _mapping(a.get("metadata")).get("spec_id") == spec_id]
    authenticated, auth_warnings = _bundle_authentication(spec_dir, attempts)
    integrity_num, integrity_label, _ = _integrity(spec_dir, authenticated)
    host_blocks, has_bundles = _host_evidence_blocks(authenticated, auth_warnings, redact=redact)
    verification = "unknown" if auth_warnings else (row.get("verification") or "unknown")
    integrity = _agent_claim(
        "integrity",
        f"{integrity_num} {integrity_label}",
        redact=redact,
    )
    body = [
        f"<p><a href=\"{_h(out_rel_prefix)}/roadmap.html\">Roadmap</a></p>",
        "<h1>Run record</h1>",
        _agent_claim("spec.yaml name", info.get("name") or spec_id, redact=redact),
        f"<p>{_stamp(verification)} totals: {_h(row.get('accepted_turns', 0))} accepted turns</p>",
        _agent_claim("spec.yaml lane", info.get("lane") or info.get("used_model_class") or "—", redact=redact),
        _agent_claim("spec.yaml current_phase", info.get("current_phase") or "—", redact=redact),
        _agent_claim("spec.yaml status", info.get("status") or row.get("claim") or "unknown", redact=redact),
        _agent_claim("spec.yaml next_action", info.get("next_action") or "—", redact=redact),
        f"<section><h2>Integrity</h2>{integrity}</section>",
        f"<section><h2>Phase Timeline</h2>{_phase_timeline(spec_dir, attempts, redact=redact)}</section>",
        f"<section><h2>Gate Ledger</h2>{_gate_ledger(attempts, redact=redact)}</section>",
        f"<section><h2>Host Evidence</h2>{host_blocks}</section>",
        f"<section><h2>TDD Ledger</h2>{_tdd_ledger(spec_dir, redact=redact)}</section>",
        f"<section><h2>Cost</h2>{_cost(attempts)}</section>",
        f"<section><h2>Chain</h2>{_chain(row if not auth_warnings else {'chain': {'checked': False}}, has_bundles, redact=redact)}</section>",
    ]
    return _page(
        _shown(f"{repo_slug} / {spec_id}", redact),
        "\n".join(body),
        root_href=f"{out_rel_prefix}/index.html",
        context_label="Record",
    )


def _dependencies(root: Path) -> list[dict]:
    data = _load_yaml(runtime_dir(root) / "dependencies.yaml")
    if not isinstance(data, dict):
        return []
    raw = data.get("dependencies") or data.get("edges") or []
    return [item for item in raw if isinstance(item, dict)]


def _edge_parts(edge: dict) -> tuple[str, str, str]:
    src = _first(edge.get("from"), edge.get("spec"), edge.get("dependent"), edge.get("source"))
    dst = _first(edge.get("to"), edge.get("requires"), edge.get("dependency"), edge.get("target"))
    kind = str(edge.get("type") or edge.get("kind") or "required")
    return str(src), str(dst), kind


def _roadmap(
    repo_slug: str,
    root: Path,
    report: dict,
    spec_dirs: dict[str, Path],
    *,
    has_releases: bool = False,
    spec_owners: dict[str, list[dict]] | None = None,
    declared_products: list[dict] | None = None,
    ) -> str:
    items = _queue_items(report)
    attempts = _attempt_records(report)
    blocked = _blocked_items(items)
    in_flight = {_spec_id_from_item(item) for item in items if _is_in_flight_item(item)}
    columns: dict[str, list[str]] = {name: [] for name, _ in BUCKETS}
    infos: dict[str, dict] = {}
    allowed = set(spec_dirs)
    report_rows = {row["spec"]: row for row in _report_specs(report, allowed)}
    for spec_id, spec_dir in spec_dirs.items():
        info = _spec_info(spec_dir)
        infos[spec_id] = info
        host_verdict = report_rows.get(spec_id, {}).get("verification")
        sync_state = planning.sync_visibility(spec_dir) if host_verdict == planning.HOST_VERIFIED else None
        bucket = _status_bucket(
            str(sync_state or info.get("status") or report_rows.get(spec_id, {}).get("claim") or ""),
            spec_id in in_flight,
        )
        columns.setdefault(bucket, []).append(spec_id)

    col_html = []
    bucket_index = {name: idx for idx, (name, _) in enumerate(BUCKETS)}
    spec_bucket = {spec_id: name for name, specs in columns.items() for spec_id in specs}
    for name, _ in BUCKETS:
        cards = []
        for spec_id in sorted(columns.get(name, [])):
            info = infos.get(spec_id, {})
            row = report_rows.get(spec_id, {})
            spec_attempts = [a for a in attempts if _mapping(a.get("metadata")).get("spec_id") == spec_id]
            _authenticated, auth_warnings = _bundle_authentication(spec_dirs[spec_id], spec_attempts)
            verification = "unknown" if auth_warnings else (row.get("verification") or ("–" if spec_id in in_flight else "unknown"))
            cls = "card"
            chips = []
            for item in blocked.get(spec_id, []):
                state = str(item.get("state") or "").upper()
                if state == "BLOCKED_DEP":
                    cls += " blocked-dep"
                    deps = item.get("unmet_deps") or item.get("blocked_on") or item.get("reason") or item.get("detail") or ""
                    if isinstance(deps, list):
                        deps = ", ".join(str(dep) for dep in deps)
                    chips.append(f"<span class=\"chip attention\">BLOCKED_DEP {_h(deps)}</span>")
                elif state == "BLOCKED_HUMAN":
                    cls += " blocked-human"
                    reason = item.get("reason") or item.get("detail") or ""
                    chips.append(f"<span class=\"chip warn\">BLOCKED_HUMAN {_h(reason)}</span>")
            owners = (spec_owners or {}).get(spec_id, [])
            owner_chips = "".join(
                f'<a class="chip claimed" href="../projects/{_h(owner["product"])}.html">'
                f'{_h(owner["title"])}</a>'
                for owner in owners
            )
            ownership = f'<div class="owner-chips">{owner_chips}</div>' if owner_chips else ""
            cards.append(
                f"<div class=\"{cls}\" id=\"card-{_h(spec_id)}\">"
                f"<p>{_stamp(verification)}</p>"
                "<div class=\"agent\"><span class=\"chip claimed\">claimed</span>"
                f"<h3><a href=\"spec/{_h(spec_id)}.html\">{_h(info.get('name') or spec_id)}</a></h3>"
                f"<p>current_phase: {_h(info.get('current_phase') or '—')}</p>"
                f"<p>next_action: {_h(info.get('next_action') or '—')}</p>"
                f"<p>lane: {_h(info.get('lane') or info.get('used_model_class') or '—')}</p></div>"
                + ownership
                + "".join(chips)
                + "</div>"
            )
        col_html.append(f"<section class=\"column\"><h2>{_h(name)}</h2>{''.join(cards)}</section>")

    required_edges = []
    hover_edges = []
    for edge in _dependencies(root):
        src, dst, kind = _edge_parts(edge)
        if src not in spec_bucket or dst not in spec_bucket:
            continue
        x1 = 80 + bucket_index.get(spec_bucket[src], 0) * 210
        x2 = 80 + bucket_index.get(spec_bucket[dst], 0) * 210
        y = 35 + len(required_edges + hover_edges) * 16
        markup = (
            f"<line class=\"edge-required\" x1=\"{x1}\" y1=\"{y}\" x2=\"{x2}\" y2=\"{y}\" "
            f"data-from=\"{_h(src)}\" data-to=\"{_h(dst)}\" />"
        )
        if kind == "required":
            required_edges.append(markup)
        else:
            hover_edges.append(
                f"<g class=\"edge-hover\" data-kind=\"{_h(kind)}\" data-from=\"{_h(src)}\" data-to=\"{_h(dst)}\"></g>"
            )
    svg = (
        "<svg class=\"svg-layer\" viewBox=\"0 0 940 130\" role=\"img\" aria-label=\"required dependency edges\">"
        "<defs><marker id=\"arrow\" markerWidth=\"10\" markerHeight=\"10\" refX=\"8\" refY=\"3\" orient=\"auto\">"
        "<path d=\"M0,0 L0,6 L9,3 z\" fill=\"currentColor\" /></marker></defs>"
        + "".join(required_edges)
        + "".join(hover_edges)
        + "</svg>"
    )
    links = '<a href="../index.html">Projects</a>'
    if has_releases:
        links += ' · <a href="releases.html">Releases</a>'
    declared = declared_products or []
    declared_html = "".join(
        f'<a class="badge badge--claimed" href="../projects/{_h(row["product"])}.html">{_h(row["title"])}</a>'
        for row in declared
    ) or '<span class="badge badge--claimed">Unassigned repository</span>'
    owners = sorted(
        {owner["product"]: owner for rows in (spec_owners or {}).values() for owner in rows}.values(),
        key=lambda row: row["product"],
    )
    owner_html = "".join(
        f'<a class="badge badge--claimed" href="../projects/{_h(row["product"])}.html">{_h(row["title"])}</a>'
        for row in owners
    ) or '<span class="quiet">No release roadmap currently references this repository.</span>'
    registry = planning._registry(root, projects_root=None)
    inventory, diagnostics = planning.intent_inventory(root, registry)
    backlog_index, backlog_diagnostics = planning.active_backlog_capability_index(root, registry)
    intent_cards = "".join(_intent_card(item) for item in inventory) + "".join(
        _intent_card(item, diagnostic=True) for item in diagnostics
    )
    body = (
        f'<p class="crumbs">{links}</p><div class="page-head"><div class="page-head__copy">'
        f'<p class="eyebrow">Repository · physical view</p><h1>{_h(repo_slug)}</h1>'
        '<p>Lifecycle lanes show the repository as it exists on disk. Declared status stays grey; '
        'only host verdicts turn green.</p></div></div>'
        '<section class="panel panel--quiet"><div class="panel-head"><div><p class="section-kicker">Project context</p>'
        f'<h2>Declared product span</h2></div></div><div class="owner-chips">{declared_html}</div>'
        f'<details class="disclosure"><summary>Owned through specs by</summary><div class="owner-chips">{owner_html}</div></details></section>'
        f'<section class="panel"><div class="panel-head"><div><p class="section-kicker">Repository planner</p>'
        f'<h2>Lifecycle lanes</h2></div><span class="badge badge--claimed">{len(spec_dirs)} specs</span></div>'
        f'<div class="columns">{"".join(col_html)}</div></section>'
        f'<section class="panel"><div class="panel-head"><div><p class="section-kicker">Intent backlog</p>'
        f'<h2>Claimed-only backlog items</h2></div><span class="badge badge--claimed">{len(inventory) + len(diagnostics)} intents</span></div>'
        f'{intent_cards or _EMPTY_INTENT_BACKLOG}</section>'
        f'{_backlog_capability_panel(backlog_index, backlog_diagnostics)}'
        f'<section class="panel panel--quiet"><div class="panel-head"><div><p class="section-kicker">Dependencies</p>'
        f'<h2>DAG</h2></div></div><div class="dag-wrap">{svg}</div></section>'
    )
    return _page(f"{repo_slug} roadmap", body, root_href="../index.html", context_label="Repository")


def _releases(repo_slug: str, root: Path, spec_dirs: dict[str, Path], *, redact: bool = False) -> str:
    """Render authored releases without promoting agent intent to a host verdict."""
    registry = planning._registry(root, projects_root=None)
    backlog_index, backlog_diagnostics = planning.active_backlog_capability_index(root, registry)
    sections = []
    archived_sections = []
    for release in planning.load_releases(root):
        comp = planning.completeness(release, registry)
        counts = Counter(member.verification for member in comp.members)
        if comp.intents:
            breakdown = (
                f"[fulfilled {comp.claimed_states.get('fulfilled', 0)} · "
                f"in-flight {comp.claimed_states.get('in-flight', 0)} · "
                f"decomposed {comp.claimed_states.get('decomposed', 0)} · "
                f"accepted {comp.claimed_states.get('accepted', 0)} · blocked {comp.dangling}]"
            )
        else:
            breakdown = planning._segments(comp)
        members = []
        for member in comp.members:
            ref = member.ref
            label = ref.canonical
            if not ref.alias and ref.spec_id in spec_dirs:
                label = f'<a href="spec/{_h(ref.spec_id)}.html">{_h(_shown(ref.spec_id, redact))}</a>'
                title = _spec_info(spec_dirs[ref.spec_id]).get("name") or ref.spec_id
            else:
                label = _h(_shown(ref.canonical, redact))
                title = ref.canonical
            claim = (
                '<div class="agent"><span class="chip claimed">claimed</span>'
                f"<h3>{label}</h3><p>spec: {_h(_shown(title, redact))}</p></div>"
            )
            if member.verification in {planning.SYNCED, planning.HOST_VERIFIED}:
                stamp = "synced" if member.verification == planning.SYNCED else "host-verified"
                members.append(
                    '<div class="card"><div class="host-seal host-ok">'
                    f"{_stamp(stamp)}</div>{claim}</div>"
                )
            elif member.verification == planning.VERIFIED_AWAITING_SYNC:
                members.append(
                    f'<div class="card blocked-dep"><div class="host-seal host-bad">{_stamp("host-verified")} '
                    '<strong class="attention">VERIFIED — AWAITING SYNC</strong></div>'
                    f'{claim}<p>Human resolution required: amend the intent delta; fix the SSOT; or file a narrowing task.</p></div>'
                )
            elif (member.status or "") in planning.PRE_IMPLEMENTATION_STATUSES:
                members.append(
                    f'<div class="card">{claim}<p><span class="chip claimed">{_h(member.status or "planned")}</span> '
                    "intentional roadmap member</p></div>"
                )
            elif member.verification == planning.SELF_REPORTED:
                members.append(
                    f'<div class="card">{claim}<p><span class="chip claimed">self-reported</span></p></div>'
                )
            elif not member.resolved:
                # Truly dangling: the referenced spec dir does not exist. That is broken.
                error = _shown(member.error or "unresolved release member", redact)
                members.append(
                    f'<div class="card blocked-dep"><div class="host-seal host-bad">{_stamp("unknown")} '
                    f'<strong class="attention">BROKEN RELEASE REF</strong><p>{_h(error)}</p></div>{claim}</div>'
                )
            else:
                # Resolved but not host-verified: an honest agent-claimed "unknown", NOT broken —
                # the spec exists, the host simply has not stamped it. Conflating this with a dangling
                # ref (as the prior single else-branch did) mislabels every not-yet-host-verified
                # member as a BROKEN RELEASE REF.
                members.append(
                    f'<div class="card">{claim}<p><span class="chip claimed">unknown</span> '
                    "resolved · not host-verified</p></div>"
                )
        if comp.intents:
            intent_cards = []
            for intent in comp.intents:
                intent_members = []
                for member in intent.members:
                    title = member.ref.canonical
                    if not member.ref.alias and member.ref.spec_id in spec_dirs:
                        title = _spec_info(spec_dirs[member.ref.spec_id]).get("name") or member.ref.spec_id
                        spec_label = f'<a href="spec/{_h(member.ref.spec_id)}.html">{_h(_shown(title, redact))}</a>'
                    else:
                        spec_label = _h(_shown(member.ref.canonical, redact))
                    host = (
                        f'<div class="host-seal host-ok">{_stamp("host-verified")}</div>'
                        if member.verification == planning.HOST_VERIFIED
                        else f'<p><span class="chip claimed">verification: {_h(member.verification or "unknown")}</span></p>'
                    )
                    broken = (
                        f'<div class="host-seal host-bad"><strong class="attention">DANGLING SPEC REF</strong>'
                        f'<p>{_h(_shown(member.error or "unresolved intent member", redact))}</p></div>'
                        if not member.resolved else ""
                    )
                    intent_members.append(
                        '<div class="card">'
                        f'{broken}<div class="agent"><span class="chip claimed">claimed spec</span><h4>{spec_label}</h4>'
                        f'<p>canonical status: {_h(member.status or "unknown")}</p></div>{host}</div>'
                    )
                diagnostic = (
                    f'<div class="host-seal host-bad"><strong class="attention">INTENT OWNERSHIP ERROR</strong>'
                    f'<p>{_h(_shown(intent.error, redact))}</p></div>' if intent.error else ""
                )
                intent_cards.append(
                    '<article class="card intent-release-row"><div class="agent">'
                    '<span class="chip claimed">claimed intent</span>'
                    f'<h3>{_h(_shown(intent.title, redact))}</h3>'
                    f'<p>{_h(intent.intent_id)} · lifecycle: {_h(intent.declared_status or "unknown")} · '
                    f'projection: {_h(intent.visible_state)}</p></div>{diagnostic}'
                    f'<div class="grid">{"".join(intent_members) or _QUIET_NO_DECOMPOSITION}</div></article>'
                )
            members_html = "".join(intent_cards)
        else:
            members_html = '<div class="grid">' + "".join(members) + "</div>"
        errors = "".join(
            f'<div class="host-seal host-bad"><strong class="attention">RELEASE PARSE ERROR</strong>'
            f"<p>{_h(_shown(error, redact))}</p></div>"
            for error in release.parse_errors
        )
        (archived_sections if _release_archived(release) else sections).append(
            '<section class="card">'
            '<div class="agent"><span class="chip claimed">claimed</span>'
            f"<h2>{_h(_shown(release.title, redact))}</h2>"
            f"<p>product: {_h(_shown(release.product, redact))} · status: {_h(_shown(release.status, redact))}</p>"
            f"<p>goal: {_h(_shown(release.goal or '—', redact))}</p></div>"
            '<div class="host-seal host-ok">'
            f"{_stamp('host-verified completeness')} <strong>{comp.fraction} · {comp.percent}% done</strong>"
            f"<p>{_h(breakdown)}</p></div>{errors}{members_html}</section>"
        )
    archived_block = (
        f'<details class="disclosure archived-reveal"><summary>Show {len(archived_sections)} archived release'
        f'{"s" if len(archived_sections) != 1 else ""}</summary>{"".join(archived_sections)}</details>'
    ) if archived_sections else ""
    body = (
        f'<p><a href="roadmap.html">Roadmap</a> · <a href="../index.html">Fleet</a></p>'
        f"<h1>{_h(_shown(repo_slug, redact))} releases</h1>"
        f"{_backlog_capability_panel(backlog_index, backlog_diagnostics, redact=redact)}"
        f"{''.join(sections) or _QUIET_NO_ACTIVE_RELEASES}{archived_block}"
    )
    return _page(
        _shown(f"{repo_slug} releases", redact), body,
        root_href="../index.html", context_label="Releases",
    )


def _last_activity(report: dict, spec_dirs: dict[str, Path]) -> str:
    stamps = []
    for attempt in _attempt_records(report):
        stamps.append(str(attempt.get("created_at") or _mapping(attempt.get("metadata")).get("started_at") or ""))
    for spec_dir in spec_dirs.values():
        try:
            stamps.append(datetime.fromtimestamp(spec_dir.stat().st_mtime, tz=timezone.utc).isoformat())
        except OSError:
            pass
    stamp = max([s for s in stamps if s], default="")
    if not stamp:
        return "unknown"
    try:
        dt = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        delta = datetime.now(timezone.utc) - dt
        days = max(0, delta.days)
        return f"{days}d"
    except ValueError:
        return stamp


def _age_number(value: str) -> int:
    try:
        return int(value[:-1]) if value.endswith("d") else 10**9
    except (TypeError, ValueError):
        return 10**9


def _lane_markup(repo: dict) -> str:
    counts = repo["buckets"]
    total = sum(counts.values())
    segments = "".join(
        f'<span class="lane-seg lane-seg--{_h(name.lower().replace(" ", "-"))}" '
        f'style="flex:{counts.get(name, 0)}" title="{_h(name)}: {counts.get(name, 0)}"></span>'
        for name, _ in BUCKETS if counts.get(name, 0)
    )
    if not segments:
        segments = '<span class="lane-seg lane-empty" title="No specs"></span>'
    labels = "".join(
        f'<span><b>{_h(name)}</b> {counts.get(name, 0)}</span>' for name, _ in BUCKETS
    )
    return f'<div class="lane-bar">{segments}</div><div class="lane-counts">{labels}</div>'


def _repo_card(repo: dict, *, prefix: str = "") -> str:
    attention = repo["blocked"] + repo.get("failed", 0)
    cls = "repo-card repo-card--danger" if attention else "repo-card"
    tally = f'{repo["verified"]} / {repo["self_reported"]} / {repo["unknown"]}'
    return (
        f'<a class="{cls}" href="{_h(prefix + repo["slug"] + "/roadmap.html")}">'
        f'<div class="repo-top"><div class="repo-title"><h3>{_h(repo["slug"])}</h3>'
        f'<p>Repository · {sum(repo["buckets"].values())} specs</p></div>'
        f'<span class="badge {"badge--blocked" if attention else "badge--claimed"}">'
        f'{attention if attention else "physical view"}</span></div>'
        f'{_lane_markup(repo)}'
        '<div class="repo-metrics">'
        f'<div class="repo-metric"><span>Attention</span><b class="{"bad" if attention else ""}">{attention}</b></div>'
        f'<div class="repo-metric"><span>Host verify</span><b>{_h(repo["host_cov"])}</b></div>'
        f'<div class="repo-metric"><span>Host / self / unknown</span><b>{_h(tally)}</b></div>'
        f'<div class="repo-metric"><span>Last activity</span><b>{_h(repo["age"])}</b></div>'
        '</div></a>'
    )


def _release_dependencies(release, comp, registry: planning.Registry) -> tuple[list, dict, bool]:
    """Stable topological order over release members; authored order breaks ties.

    This mirrors the dispatcher's dependency semantics: required edges order work, contextual and
    optional edges remain annotations, and a dependency is satisfied at lifecycle status verified
    or archived. Cross-repo targets are resolved only through planning.Registry.
    """
    authored = [member.ref.canonical for member in comp.members]
    by_key = {member.ref.canonical: member for member in comp.members}
    dependencies: dict[str, list[dict]] = {key: [] for key in authored}
    required_internal: dict[str, set[str]] = {key: set() for key in authored}
    for member in comp.members:
        spec_dir, _err = registry.spec_dir(member.ref)
        data = _load_yaml(spec_dir / "dependencies.yaml") if spec_dir and spec_dir.is_dir() else None
        for raw in _as_list(_mapping(data).get("dependencies")):
            if not isinstance(raw, dict):
                continue
            kind = str(raw.get("kind", "required")).strip().lower() or "required"
            target_ref, parse_error = planning.parse_spec_ref(raw.get("spec"))
            if target_ref is not None and target_ref.alias is None and member.ref.alias:
                target_ref = planning.SpecRef(
                    alias=member.ref.alias, spec_id=target_ref.spec_id,
                    weight=target_ref.weight, raw=target_ref.raw,
                )
            target = target_ref.canonical if target_ref else str(raw.get("spec") or "")
            target_dir, resolve_error = registry.spec_dir(target_ref) if target_ref else (None, parse_error)
            target_info = _spec_info(target_dir) if target_dir and target_dir.is_dir() else {}
            status = str(target_info.get("status") or "").strip().lower()
            satisfied = status in {"verified", "archived"}
            dependencies[member.ref.canonical].append({
                "target": target,
                "kind": kind,
                "satisfied": satisfied,
                "status": status or "unknown",
                "error": parse_error or resolve_error,
            })
            if kind not in {"contextual", "optional"} and target in by_key:
                required_internal[member.ref.canonical].add(target)

    order_index = {key: index for index, key in enumerate(authored)}
    pending = {key: set(deps) for key, deps in required_internal.items()}
    ordered_keys: list[str] = []
    while pending:
        ready = sorted((key for key, deps in pending.items() if not deps), key=order_index.get)
        if not ready:
            ordered_keys.extend(sorted(pending, key=order_index.get))
            return [by_key[key] for key in ordered_keys], dependencies, True
        for key in ready:
            ordered_keys.append(key)
            pending.pop(key)
        for deps in pending.values():
            deps.difference_update(ready)
    return [by_key[key] for key in ordered_keys], dependencies, False


def _verification_badge(value: str) -> str:
    if value == planning.HOST_VERIFIED:
        return '<span class="badge badge--host">host-verified</span>'
    if value == planning.PLANNED:
        return '<span class="badge badge--claimed">planned</span>'
    if value == planning.SELF_REPORTED:
        return '<span class="badge badge--claimed">self-reported</span>'
    return '<span class="badge badge--claimed">unknown</span>'


def _render_release_view(
    view: dict, registry: planning.Registry, repos_by_root: dict[str, dict], home_repo_name: str
) -> tuple[str, str]:
    """Render one release view as (summary card, roadmap detail section)."""
    release = view["release"]
    comp = view["completeness"]
    ordered, dependencies, cyclic = _release_dependencies(release, comp, registry)
    card = (
        '<article class="release-card"><div class="release-card__top"><div>'
        f'<h3>{_h(release.title)}</h3><p class="release-card__product">{_h(release.release_id)} · {_h(release.status)}</p>'
        f'</div><strong class="release-card__percent">{comp.percent}%</strong></div>'
        f'<div class="progress progress--release"><span class="progress__fill" style="width:{comp.percent}%"></span></div>'
        f'<div class="release-card__meta"><span>{comp.fraction} host done</span><span>{comp.dangling} unresolved</span></div>'
        f'<p class="release-card__goal">{_h(release.goal or "No goal declared.")}</p></article>'
    )
    rows = []
    entries = []
    if comp.intents:
        entries = []
        for intent in comp.intents:
            entries.append((intent, None))
            entries.extend((intent, member) for member in intent.members)
    else:
        entries = [(None, member) for member in ordered]
    spec_index = 0
    for intent, member in entries:
            if member is None:
                assert intent is not None
                diagnostic = (
                    f'<small class="attention-line">{_h(intent.error)}</small>' if intent.error else ""
                )
                rows.append(
                    '<div class="roadmap-row roadmap-row--intent agent">'
                    '<span class="roadmap-step">I</span>'
                    f'<div class="roadmap-title"><strong>{_h(intent.title)}</strong><small>{_h(intent.intent_id)}</small></div>'
                    f'<div class="roadmap-cell"><span class="badge badge--claimed">claimed intent</span>'
                    f'<small>{_h(intent.declared_status or "unknown")} lifecycle · {_h(intent.visible_state)} projection</small></div>'
                    f'<div class="roadmap-cell"><b>{len(intent.members)} specs</b>{diagnostic}</div>'
                    '<div class="roadmap-cell"><b>Ownership</b><small>planning register</small></div></div>'
                )
                continue
            spec_index += 1
            ref = member.ref
            repo_root, resolve_error = registry.resolve(ref)
            spec_dir, spec_error = registry.spec_dir(ref)
            info = _spec_info(spec_dir) if spec_dir and spec_dir.is_dir() else {}
            repo = repos_by_root.get(str(repo_root)) if repo_root else None
            queue_blocks = repo.get("_blocked_specs", {}).get(ref.spec_id, []) if repo else []
            failed = ref.spec_id in repo.get("_failed_specs", set()) if repo else False
            dep_rows = dependencies.get(ref.canonical, [])
            unmet = [dep for dep in dep_rows if dep["kind"] not in {"contextual", "optional"} and not dep["satisfied"]]
            is_active = ref.spec_id in repo.get("_in_flight", set()) if repo else False
            is_blocked = bool(queue_blocks or failed or unmet or member.error or resolve_error or spec_error)
            row_class = "roadmap-row"
            if is_blocked:
                row_class += " roadmap-row--blocked"
            elif member.verification == planning.HOST_VERIFIED:
                row_class += " roadmap-row--host"
            elif is_active:
                row_class += " roadmap-row--active"
            title = info.get("name") or ref.spec_id
            if repo:
                title_html = f'<a href="../{_h(repo["slug"])}/spec/{_h(ref.spec_id)}.html">{_h(title)}</a>'
                repo_label = repo["slug"]
            else:
                title_html = f'<strong>{_h(title)}</strong>'
                repo_label = ref.alias or home_repo_name
            dep_html = "No declared dependencies"
            if dep_rows:
                dep_html = " · ".join(
                    f'<span class="{"dep-ok" if dep["satisfied"] else "dep-wait"}">'
                    f'{_h(dep["target"])} ({_h(dep["kind"])} · {_h(dep["status"])})</span>'
                    for dep in dep_rows
                )
            if queue_blocks:
                next_text = "Blocked by dispatcher: " + ", ".join(
                    str(item.get("reason") or item.get("unmet_deps") or item.get("state") or "dependency")
                    for item in queue_blocks
                )
            elif failed:
                next_text = "Latest queue item failed"
            elif member.error or resolve_error or spec_error:
                next_text = member.error or resolve_error or spec_error
            elif unmet:
                next_text = "Waiting on " + ", ".join(dep["target"] for dep in unmet)
            else:
                next_text = info.get("next_action") or "No next action declared"
            rows.append(
                f'<div class="{row_class}"><span class="roadmap-step">{spec_index:02d}</span>'
                f'<div class="roadmap-title">{title_html}<small>{_h(ref.canonical)}</small></div>'
                f'<div class="roadmap-cell">{_verification_badge(member.verification)}'
                f'<small>canonical status: {_h(member.status or info.get("status") or "unknown")}</small></div>'
                f'<div class="roadmap-cell"><b>{_h(repo_label)}</b><small>{dep_html}</small></div>'
                f'<div class="roadmap-cell {"attention-line" if is_blocked else ""}"><b>Next</b><small>{_h(next_text)}</small></div></div>'
            )
    cycle_note = '<p class="attention-line">Dependency cycle detected; cyclic members retain authored order.</p>' if cyclic else ""
    detail = (
        '<section class="release-detail"><div class="panel-head"><div>'
        f'<p class="section-kicker">{_h(release.release_id)} · {_h(release.status)}</p><h2>{_h(release.title)}</h2>'
        f'</div><span class="badge badge--host">{comp.fraction} host done</span></div>{cycle_note}'
        f'<div class="roadmap-list">{"".join(rows) or _QUIET_NO_RELEASE_SPECS}</div></section>'
    )
    return card, detail


_ARCHIVED_RELEASE_STATUSES = {"archived", "abandoned"}


def _release_archived(release) -> bool:
    return str(getattr(release, "status", "") or "").strip().lower() in _ARCHIVED_RELEASE_STATUSES


def _split_release_views(views: list[dict]) -> tuple[list[dict], list[dict]]:
    """Partition release views into (active, archived). Archived/abandoned targets are parked:
    excluded from headline metrics and hidden behind a reveal toggle."""
    active = [v for v in views if not _release_archived(v["release"])]
    archived = [v for v in views if _release_archived(v["release"])]
    return active, archived


def _project_page(project: dict, repos_by_root: dict[str, dict]) -> str:
    active_views, archived_views = _split_release_views(project["releases"])
    total = sum(view["completeness"].total for view in active_views)
    verified = sum(view["completeness"].verified for view in active_views)
    percent = round(100 * verified / total) if total else 0
    physical_roots = set(project["declared_roots"]) | set(project["touched_roots"])
    physical = [repos_by_root[str(root)] for root in physical_roots if str(root) in repos_by_root]
    blocked = sum(repo["blocked"] + repo.get("failed", 0) for repo in physical)
    dangling = sum(view["completeness"].dangling for view in active_views)
    errors = list(project["errors"])
    attention = blocked + dangling + len(errors)
    ages = [repo["age"] for repo in physical]
    age = min(ages, key=_age_number, default="unknown")
    resolved = total - dangling

    registry = project["registry"]
    backlog_index, backlog_diagnostics = planning.active_backlog_capability_index(
        project["entity"].home_repo,
        registry,
        product_context=project["product"],
    )
    home_repo_name = project["entity"].home_repo.name
    active = [_render_release_view(v, registry, repos_by_root, home_repo_name) for v in active_views]
    archived = [_render_release_view(v, registry, repos_by_root, home_repo_name) for v in archived_views]
    release_cards = [c for c, _ in active]
    release_details = [d for _, d in active]
    _plural = "s" if len(archived_views) != 1 else ""
    archived_targets_block = (
        f'<details class="disclosure archived-reveal"><summary>Show {len(archived_views)} archived release target{_plural}</summary>'
        f'<div class="release-grid">{"".join(c for c, _ in archived)}</div></details>'
    ) if archived_views else ""
    archived_roadmap_block = (
        f'<details class="disclosure archived-reveal"><summary>Show {len(archived_views)} archived roadmap{_plural}</summary>'
        f'{"".join(d for _, d in archived)}</details>'
    ) if archived_views else ""

    repo_cards = "".join(_repo_card(repo, prefix="../") for repo in sorted(physical, key=lambda row: row["slug"]))
    declared_names = {str(root) for root in project["declared_roots"]}
    repo_note = (
        f'{len(project["declared_roots"])} declared span · '
        f'{len([root for root in project["touched_roots"] if str(root) not in declared_names])} additionally touched by roadmap refs'
    )
    findings = ""
    if errors:
        findings = (
            '<section class="panel"><div class="panel-head"><div><p class="section-kicker">Registry findings</p>'
            f'<h2>Needs declaration repair</h2></div></div><ul class="finding-list">'
            + "".join(f'<li>{_h(error)}</li>' for error in errors) + '</ul></section>'
        )
    body = (
        '<p class="crumbs"><a href="../index.html">Projects</a><span>/</span>Declared project</p>'
        '<div class="page-head"><div class="page-head__copy"><p class="eyebrow">Project · Product declaration</p>'
        f'<h1>{_h(project["title"])}</h1><p>Project → release target → roadmap, resolved from '
        '<code>product.yaml</code> and <code>release.yaml</code>.</p></div></div>'
        '<div class="hero-metrics">'
        f'<div class="hero-metric hero-metric--lead"><span>Host done</span><b class="ok">{percent}%</b><small>{verified}/{total} release members</small></div>'
        f'<div class="hero-metric"><span>Needs attention</span><b class="{"bad" if attention else ""}">{attention}</b><small>blocked, failed, unresolved</small></div>'
        f'<div class="hero-metric"><span>Coverage</span><b>{resolved}/{total}</b><small>roadmap refs resolved</small></div>'
        f'<div class="hero-metric"><span>Last activity</span><b>{_h(age)}</b><small>across touched repos</small></div></div>'
        '<section class="panel"><div class="panel-head"><div><p class="section-kicker">Release targets</p>'
        f'<h2>{len(active_views)} declared</h2></div></div><div class="release-grid">'
        f'{"".join(release_cards) or _QUIET_NO_RELEASE_TARGETS}</div>{archived_targets_block}</section>'
        '<section class="panel"><div class="panel-head"><div><p class="section-kicker">Roadmap</p>'
        '<h2>Dependency-ordered roadmap</h2><p>Required dependencies appear before dependents when the data exists.</p>'
        f'</div></div>{"".join(release_details) or _QUIET_AUTHOR_A_RELEASE}{archived_roadmap_block}</section>'
        f'{_backlog_capability_panel(backlog_index, backlog_diagnostics)}'
        '<section class="panel panel--quiet"><div class="panel-head"><div><p class="section-kicker">Physical footprint</p>'
        f'<h2>Project → repos → specs</h2><p>{_h(repo_note)}</p></div></div><div class="repo-list">{repo_cards}</div></section>'
        f'{findings}'
    )
    return _page(project["title"], body, root_href="../index.html", context_label="Project")


def _builder_operational_state(projects_root: Path) -> dict | None:
    """Read-only operational discovery; these records never affect verification."""
    home_root = projects_root / ".builder-home"
    if not (home_root / "builder.yaml").is_file():
        return None
    from _builder_project_model.home import load_builder_home
    from _builder_project_model.eligibility import inspect_scheduler_lock
    from _dispatch_runtime.paths import runtime_dir as _runtime_dir

    state = {"daemon": None, "providers": [], "sessions": [], "findings": []}
    daemon_path = home_root / "state" / "daemon.json"
    if daemon_path.is_file():
        try:
            state["daemon"] = json.loads(daemon_path.read_text(encoding="utf-8"))
            state["findings"].extend(state["daemon"].get("findings") or [])
        except Exception as exc:  # noqa: BLE001
            state["findings"].append(f"daemon metadata unreadable: {exc}")
    try:
        home = load_builder_home(home_root)
    except Exception as exc:  # noqa: BLE001
        state["findings"].append(f"home declaration unreadable: {exc}")
        return state
    providers_dir = home_root / "state" / "providers"
    for provider, policy in sorted(home.policy.providers.items()):
        row = {"provider": provider, "cooldown_until": None}
        path = providers_dir / f"{provider}.json"
        if path.is_file():
            try:
                row.update(json.loads(path.read_text(encoding="utf-8")))
            except Exception as exc:  # noqa: BLE001
                state["findings"].append(f"provider {provider} unreadable: {exc}")
        row["max_sessions"] = policy.max_sessions
        state["providers"].append(row)
    sessions_dir = home_root / "state" / "sessions"
    if sessions_dir.is_dir():
        for path in sorted(sessions_dir.glob("*.json")):
            if path.name.endswith(".launcher.json"):
                continue
            try:
                state["sessions"].append(json.loads(path.read_text(encoding="utf-8")))
            except Exception as exc:  # noqa: BLE001
                state["findings"].append(f"session {path.name} unreadable: {exc}")
    for provider in state["providers"]:
        provider["used_sessions"] = sum(
            1 for row in state["sessions"]
            if row.get("provider") == provider.get("provider")
            and row.get("state") in {"starting", "active", "reaping"}
        )
    for repo_id in home.policy.drain_repos:
        root = home.repo_roots_by_id.get(repo_id)
        if root is None:
            state["findings"].append(f"ownership: unknown allow-listed repo {repo_id}")
            continue
        lock = inspect_scheduler_lock(_runtime_dir(root) / "dispatch-queue" / "queue" / ".scheduler.lock")
        if lock.state == "locked-live" and not str(lock.owner or "").startswith("central-"):
            state["findings"].append(f"ownership: {repo_id} has live non-central owner {lock.owner}")
    return state


def _operational_panel(state: dict | None) -> str:
    if state is None:
        return ""
    daemon = state.get("daemon") or {}
    daemon_text = "not running" if not daemon else f"pid {daemon.get('pid', '?')} · heartbeat {daemon.get('heartbeat_at', '?')}"
    provider_rows = "".join(
        f"<li><b>{_h(row.get('provider'))}</b> {_h(row.get('used_sessions', 0))}/{_h(row.get('max_sessions'))} used · cooldown {_h(row.get('cooldown_until') or 'open')}</li>"
        for row in state.get("providers", [])
    )
    session_rows = "".join(
        f"<li>{_h(row.get('provider'))} · {_h(row.get('repo_id'))} · {_h(row.get('state'))} · slot {_h(row.get('slot_id'))}</li>"
        for row in state.get("sessions", [])
    )
    findings = "".join(f"<li class=\"attention-line\">{_h(item)}</li>" for item in state.get("findings", []))
    return (
        '<section class="panel"><div class="panel-head"><div><p class="section-kicker">Operational state</p>'
        '<h2>Central daemon, providers, and sessions</h2><p>Operational data — not a host verdict and not part of either provenance register.</p>'
        f'</div></div><p><b>Daemon:</b> {_h(daemon_text)}</p><h3>Provider capacity / cooldown</h3><ul>{provider_rows or "<li>none</li>"}</ul>'
        f'<h3>Live session records</h3><ul>{session_rows or "<li>none</li>"}</ul>'
        f'<h3>Ownership findings</h3><ul>{findings or "<li>none</li>"}</ul></section>'
    )


def _portfolio(projects: list[dict], unassigned: list[dict], registry_findings: list[str], repos_by_root: dict[str, dict], operational: dict | None = None) -> str:
    project_cards = []
    for project in projects:
        release_views, archived_views = _split_release_views(project["releases"])
        total = sum(view["completeness"].total for view in release_views)
        verified = sum(view["completeness"].verified for view in release_views)
        percent = round(100 * verified / total) if total else 0
        roots = set(project["declared_roots"]) | set(project["touched_roots"])
        repos = [repos_by_root[str(root)] for root in roots if str(root) in repos_by_root]
        blocked = sum(repo["blocked"] + repo.get("failed", 0) for repo in repos)
        dangling = sum(view["completeness"].dangling for view in release_views)
        attention = blocked + dangling + len(project["errors"])
        resolved = total - dangling
        age = min((repo["age"] for repo in repos), key=_age_number, default="unknown")
        release_names = ", ".join(view["release"].title for view in release_views) or "No release targets yet"
        if archived_views:
            release_names += f" · +{len(archived_views)} archived (parked)"
        project_cards.append(
            f'<article class="project-card {"project-card--attention" if attention else ""}">'
            '<div class="card-top"><div class="card-title">'
            f'<h3><a href="projects/{_h(project["product"])}.html">{_h(project["title"])}</a></h3>'
            f'<p>Declared project · {len(project["declared_roots"])} repo span</p>'
            f'</div><strong class="big-percent">{percent}%<small>host done</small></strong></div>'
            f'<div class="progress"><span class="progress__fill" style="width:{percent}%"></span></div>'
            f'<div class="card-summary"><span><b>{len(release_views)}</b> releases</span>'
            f'<span><b>{resolved}/{total}</b> refs resolved</span><span class="{"attention-line" if attention else ""}"><b>{attention}</b> attention</span>'
            f'<span><b>{_h(age)}</b> activity</span></div>'
            f'<details class="disclosure"><summary>Release targets</summary><span class="quiet">{_h(release_names)}</span></details></article>'
        )
    unassigned_cards = "".join(_repo_card(repo) for repo in unassigned)
    findings = ""
    if registry_findings:
        findings = (
            '<section class="panel"><div class="panel-head"><div><p class="section-kicker">Registry findings</p>'
            '<h2>Ambiguity or invalid declarations</h2></div></div><ul class="finding-list">'
            + "".join(f'<li>{_h(item)}</li>' for item in registry_findings) + '</ul></section>'
        )
    total_specs = sum(sum(repo["buckets"].values()) for repo in repos_by_root.values())
    total_attention = sum(repo["blocked"] + repo.get("failed", 0) for repo in repos_by_root.values())
    body = (
        '<div class="page-head"><div class="page-head__copy"><p class="eyebrow">Workspace portfolio · by project</p>'
        '<h1>The Planner</h1><p>Declared products are projects. Releases are targets. Their ordered spec refs are the roadmap.</p>'
        '</div></div><div class="hero-metrics">'
        f'<div class="hero-metric hero-metric--lead"><span>Declared projects</span><b>{len(projects)}</b><small>discovered by planning.Registry</small></div>'
        f'<div class="hero-metric"><span>Unassigned repos</span><b>{len(unassigned)}</b><small>need product.yaml</small></div>'
        f'<div class="hero-metric"><span>Needs attention</span><b class="{"bad" if total_attention else ""}">{total_attention}</b><small>blocked or failed</small></div>'
        f'<div class="hero-metric"><span>Visible specs</span><b>{total_specs}</b><small>physical inventory</small></div></div>'
        '<section class="panel"><div class="panel-head"><div><p class="section-kicker">Declared products</p>'
        f'<h2>Project portfolio</h2><p>{len(projects)} project declarations found.</p></div></div>'
        f'<div class="project-grid">{"".join(project_cards) or _QUIET_NO_PRODUCTS}</div></section>'
        '<section class="panel panel--quiet"><div class="panel-head"><div><p class="section-kicker">Declaration gap</p>'
        '<h2>Unassigned repositories — not yet under a project</h2><p>These repositories have runtime data but are not listed by any discovered <code>product.yaml</code>.</p>'
        f'</div><span class="badge badge--claimed">{len(unassigned)}</span></div><div class="repo-list">{unassigned_cards}</div></section>{_operational_panel(operational)}{findings}'
    )
    return _page("The Record · Projects", body, root_href="index.html", context_label="Portfolio")


def _fleet(repos: list[dict]) -> str:
    rows = []
    sorted_repos = sorted(
        repos,
        key=lambda r: (-(r["blocked"]), -r["unknown"], r["slug"]),
    )
    for repo in sorted_repos:
        counts = repo["buckets"]
        count_values = list(counts.values())
        max_count = max(count_values) if count_values else 1
        bar = "<div class=\"bar\">" + "".join(
            f"<span title=\"{_h(name)}: {counts.get(name, 0)}\" style=\"opacity:{0.35 + 0.65 * (counts.get(name, 0) / max_count):.2f}\"></span>"
            for name, _ in BUCKETS
        ) + "</div>"
        blocked_cls = " class=\"attention\"" if repo["blocked"] else ""
        rows.append(
            f"<tr><td><a href=\"{_h(repo['slug'])}/roadmap.html\">{_h(repo['slug'])}</a></td>"
            f"<td><div class=\"agent\"><span class=\"chip claimed\">claimed</span>{bar}</div></td>"
            f"<td{blocked_cls}>{repo['blocked']}</td><td>{repo['host_cov']}</td>"
            f"<td>{repo['verified']} / {repo['self_reported']} / {repo['unknown']}</td>"
            f"<td><span class=\"agent\"><span class=\"chip claimed\">claimed</span> {_h(repo['age'])}</span></td></tr>"
        )
    body = (
        "<h1>Fleet</h1><div class=\"table-wrap\"><table><tr><th>repo</th><th>specs by lifecycle bucket</th>"
        "<th>blocked count</th><th>host_verify coverage</th><th>verification tally</th><th>last-activity age</th></tr>"
        + "".join(rows)
        + "</table></div>"
    )
    return _page("Fleet", body)


def _build_repo(
    root: Path,
    out: Path,
    *,
    spec_owners: dict[str, list[dict]] | None = None,
    declared_products: list[dict] | None = None,
) -> dict:
    if not runtime_dir(root).is_dir():
        raise OperationalExit(f"no runtime directory (.builder/) under {root}")
    report = gc.scan_repo(root, check_chain=True)
    spec_dirs = _spec_dirs(root)
    allowed = set(spec_dirs)
    report_specs = _report_specs(report, allowed)
    repo_slug = root.name or "repo"
    repo_dir = repo_slug
    releases = planning.load_releases(root)
    _write(
        out,
        f"{repo_dir}/roadmap.html",
        _roadmap(
            repo_slug, root, report, spec_dirs, has_releases=bool(releases),
            spec_owners=spec_owners, declared_products=declared_products,
        ),
    )
    if releases:
        _write(out, f"{repo_dir}/releases.html", _releases(repo_slug, root, spec_dirs))
    for spec_id, spec_dir in spec_dirs.items():
        _write(out, f"{repo_dir}/spec/{spec_id}.html", _run_record(repo_slug, root, report, spec_id, spec_dir))

    items = _queue_items(report)
    blocked = sum(len(v) for v in _blocked_items(items).values())
    failed_specs = {
        _spec_id_from_item(item) for item in items
        if str(item.get("state") or item.get("status") or "").upper() == "FAILED" and _spec_id_from_item(item)
    }
    in_flight = {_spec_id_from_item(item) for item in items if _is_in_flight_item(item)}
    buckets = Counter()
    for spec_id, spec_dir in spec_dirs.items():
        info = _spec_info(spec_dir)
        row = next((s for s in report_specs if s.get("spec") == spec_id), {})
        buckets[_status_bucket(str(info.get("status") or row.get("claim") or ""), spec_id in in_flight)] += 1
    attempts = _attempt_records(report)
    verification = Counter()
    for spec_id, spec_dir in spec_dirs.items():
        value = _verification_for(report, spec_id)
        spec_attempts = [a for a in attempts if _mapping(a.get("metadata")).get("spec_id") == spec_id]
        _authenticated, auth_warnings = _bundle_authentication(spec_dir, spec_attempts)
        if auth_warnings:
            value = "unknown"
        elif value in (None, "", "-"):
            value = "-" if spec_id in in_flight else "unknown"
        verification[value] += 1
    hv = _mapping(_mapping(report.get("coverage")).get("host_verify"))
    return {
        "slug": repo_slug,
        "root": root.resolve(),
        "blocked": blocked,
        "failed": len(failed_specs),
        "unknown": verification.get("unknown", 0),
        "verified": verification.get("host-verified", 0),
        "self_reported": verification.get("self-reported", 0),
        "buckets": buckets,
        "host_cov": f"{hv.get('adjudicated', 0)}/{hv.get('claimed', 0)}",
        "age": _last_activity(report, spec_dirs),
        "_report": report,
        "_spec_dirs": spec_dirs,
        "_blocked_specs": _blocked_items(items),
        "_failed_specs": failed_specs,
        "_in_flight": in_flight,
    }


def build(args) -> int:
    if args.all:
        # Skip symlinked repo dirs: a symlink to a sibling checkout is an alias, not a separate
        # repo, and would render the same specs twice.
        roots = sorted(
            [p for p in Path(args.all).iterdir() if not p.is_symlink() and runtime_dir(p).is_dir()],
            key=lambda p: p.name,
        )
        if not roots:
            raise OperationalExit(f"no child repos with a runtime directory (.builder/) under {args.all}")
    else:
        roots = [Path(args.root).resolve()]
    out = Path(args.out).resolve() if args.out else (runtime_dir(Path(args.root).resolve()) / "record")
    projects_root = Path(args.all).resolve() if args.all else planning.default_projects_root(roots[0])
    discovery = planning.Registry(projects_root, roots[0])
    visible = {str(root.resolve()) for root in roots}
    projects: list[dict] = []
    seen_products: set[str] = set()
    for product in discovery.products:
        if not product.product or product.product in seen_products:
            continue
        seen_products.add(product.product)
        registry = planning.Registry(projects_root, product.home_repo, product_context=product.product)
        declared_roots = {
            registry.alias_to_root[alias].resolve()
            for alias in product.repo_aliases if alias in registry.alias_to_root
        }
        releases = []
        errors = list(product.parse_errors)
        for release in planning.load_releases(product.home_repo, product_context=product.product):
            if release.product != product.product:
                errors.append(
                    f"{release.path}: release belongs to product {release.product!r}, not {product.product!r}"
                )
                continue
            releases.append({
                "release": release,
                "completeness": planning.completeness(release, registry),
            })
            errors.extend(release.parse_errors)
        touched_roots: set[Path] = set()
        for view in releases:
            for member in view["completeness"].members:
                repo_root, err = registry.resolve(member.ref)
                if not err and repo_root is not None:
                    touched_roots.add(repo_root.resolve())
        if not args.all and not any(str(root) in visible for root in declared_roots | touched_roots):
            continue
        projects.append({
            "product": product.product,
            "title": product.title or product.product,
            "entity": product,
            "registry": registry,
            "releases": releases,
            "declared_roots": declared_roots,
            "touched_roots": touched_roots,
            "errors": errors,
        })

    declared_by_root: dict[str, list[dict]] = defaultdict(list)
    owners_by_root: dict[str, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
    for project in projects:
        identity = {"product": project["product"], "title": project["title"]}
        for repo_root in project["declared_roots"]:
            declared_by_root[str(repo_root)].append(identity)
        for view in project["releases"]:
            for member in view["completeness"].members:
                repo_root, err = project["registry"].resolve(member.ref)
                if err or repo_root is None:
                    continue
                owners = owners_by_root[str(repo_root.resolve())][member.ref.spec_id]
                if not any(row["product"] == project["product"] for row in owners):
                    owners.append(identity)

    repos = [
        _build_repo(
            root, out,
            spec_owners=owners_by_root.get(str(root.resolve()), {}),
            declared_products=declared_by_root.get(str(root.resolve()), []),
        )
        for root in roots
    ]
    repos_by_root = {str(repo["root"]): repo for repo in repos}
    for project in projects:
        _write(out, f'projects/{project["product"]}.html', _project_page(project, repos_by_root))
    assigned_roots = set(declared_by_root)
    unassigned = sorted(
        (repo for repo in repos if str(repo["root"]) not in assigned_roots),
        key=lambda repo: (-(repo["blocked"] + repo.get("failed", 0)), repo["slug"]),
    )
    operational = _builder_operational_state(projects_root)
    index = _write(out, "index.html", _portfolio(projects, unassigned, discovery.findings, repos_by_root, operational))
    # Say where it went. The Record is a static site with no server, so the ONLY way to read it is
    # to open this file -- and a command that exits 0 in silence leaves the reader with nothing to
    # open and no path to guess.
    print(f"The Record: {index}")
    print(f"  {len(repos)} repo(s), {len(projects)} product(s). Open the file above in a browser.")
    if args.open:
        webbrowser.open(index.as_uri())
    return 0


def export(args) -> int:
    root = Path(args.root).resolve()
    if not runtime_dir(root).is_dir():
        raise OperationalExit(f"no runtime directory (.builder/) under {root}")
    spec_dirs = _spec_dirs(root)
    spec_dir = spec_dirs.get(args.spec_id)
    if spec_dir is None:
        raise OperationalExit(f"unknown spec: {args.spec_id}")
    report = gc.scan_repo(root, check_chain=True)
    out = Path(args.out).resolve() if args.out else Path.cwd() / f"{args.spec_id}-record.html"
    _write_file(out, _run_record(root.name or "repo", root, report, args.spec_id, spec_dir, ".", redact=True))
    return 0


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="record.py")
    p.add_argument("--version", action="version", version=f"record.py {_record_version()}")
    sub = p.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--root", default=".")
    b.add_argument("--all")
    b.add_argument("--out")
    b.add_argument("--open", action="store_true")
    e = sub.add_parser("export")
    e.add_argument("spec_id")
    e.add_argument("--root", default=".")
    e.add_argument("--out")
    return p


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.cmd == "build":
            return build(args)
        if args.cmd == "export":
            return export(args)
    except OperationalExit as exc:
        print(exc.message, file=sys.stderr)
        return 2
    except Exception as exc:  # Malformed input and I/O failures are operational, never tracebacks.
        print(f"record.py: {exc}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
