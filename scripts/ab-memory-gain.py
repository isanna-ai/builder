#!/usr/bin/env python3
"""Controlled off-vs-hivemind A/B runner for the Builder memory-gain experiment.

WHAT THIS DOES
==============
The dispatcher emits exactly one ``memory_eval`` telemetry record per plan phase.
Its ``memory_mode`` is decided ENTIRELY by the dispatcher process env: it is
``"hivemind"`` IFF BOTH ``HIVEMIND_MCP_URL`` and ``HIVEMIND_API_KEY`` are present,
else ``"off"`` (see ``lane_common._memory_mode_for_dispatcher`` and
``memory_hook._hive_client``). So the A/B arm is a pure function of those two env
vars in the PLAN subprocess.

This runner orchestrates a controlled experiment by running each benchmark spec's
PLAN phase once per arm — with the two vars STRIPPED (the ``off`` control arm) or
PRESENT (the ``hivemind`` treatment arm, values read from the live container env).
Each run emits its own ``memory_eval`` with the correct ``memory_mode``
automatically; ``builder-memory-gain.py report`` then aggregates both arms into
an off-vs-hivemind delta + Cohen's d + Mann-Whitney U.

ISOLATION + PLAN-GATE (why this no longer contaminates the live queue)
=====================================================================
The PLAN phase is the ONLY phase we want to measure. But a live
``dispatch.yaml`` typically runs with ``pipeline.plan_gate: false`` so specs auto-advance
``spec -> plan -> implement -> verify`` unattended. If the benchmark plan phases
went onto the SHARED live queue, the scheduler would AUTO-ENQUEUE the implement
phase the moment a plan succeeded (scheduler ``_advance_after_success`` falls
through to ``_enqueue_phase`` when ``plan_gate`` is false) — and the next FIFO
``run --once`` would dispatch that IMPLEMENT phase instead of the next intended
plan phase, contaminating the arms and burning the claude lane on implement work.

To make the A/B a CLEAN, ISOLATED, PLAN-ONLY experiment this runner writes a TEMP
config (``build_temp_config``) derived from ``--config`` that overrides exactly
these keys for the run:

  * ``pipeline.plan_gate = true``   -> the scheduler STOPS after the plan phase. On
    plan success it writes a gate marker (``queue/gates/<spec>.json``) and returns
    WITHOUT enqueuing implement (see scheduler.DispatchScheduler._advance_after_success:
    ``if plan_gate and completed in ("plan","4-plan"): ... return`` — it never
    reaches the ``_enqueue_phase(... nxt ...)`` line). So NO implement/verify phase
    is ever dispatched for the benchmark specs.
  * ``queue_store.path = <root>/.builder/ab-queue-<runid>`` (ABSOLUTE) -> all
    benchmark queue items, gate markers, lanes and events land in an ISOLATED temp
    queue that NEVER enters a live ``dispatch-queue`` and NEVER races an
    autonomous daemon. (Absolute so draft/enqueue/run all resolve the same dir
    regardless of cwd.)
  * ``retry_policy.max_attempts = 1`` -> a failed benchmark plan attempt goes
    straight to a TERMINAL (FAILED) state instead of re-queuing a future-scheduled
    QUEUED item. The live policy (max_attempts:2, initial_seconds:30) would, on the
    first RETRYABLE_ERROR / RATE_LIMITED, leave a time-deferred QUEUED item that the
    drain loop's ``run --once`` cannot see — which would then run under the NEXT
    arm's env and corrupt the A/B. max_attempts=1 eliminates that re-queue at the
    source; the drain loop's direct store check is the backstop.

The temp config is placed at ``<root>/.builder/ab-dispatch-<runid>.yaml`` so its
``parent.parent`` still resolves to the isanna project root (the dispatch CLI
derives ``project_dir = config.parent.parent``, and specs live under
``<root>/.builder/specs``). draft + enqueue + run all use the TEMP config path.

CLEAN ARM SEPARATION (unpaired)
===============================
Arms are UNPAIRED: the off arm and the hivemind arm each get their OWN freshly
synthesized plan-ready specs (``ab-bench-<runid>-off-<i>`` vs
``ab-bench-<runid>-hive-<i>``). Rationale: the plan phase MUTATES the spec dir
(it writes ``tasks.yaml`` and sets ``spec.yaml status: planned / current_phase:
implement`` + a ``handoff.yaml``), so a spec is plan-ready ONCE; re-running plan on
the same spec would need an artifact reset and risks coupling the two arms through
shared on-disk state. Separate specs per arm keep the two measurements fully
independent and deterministic — the only difference between an off-spec and its
paired hive-spec is the intent text (identical modulo the arm token) and the
dispatcher env. The plan-recall global is also reset per plan-goal build, so no
recall stats leak across specs.

PLAN-READY PREPARATION (no claude spend on the spec phase)
==========================================================
A drafted spec starts at ``current_phase: spec`` and is NOT plan-ready. Rather than
spend the claude lane on a real spec phase, this runner SYNTHESIZES the plan-ready
artifacts directly (``prepare_plan_ready_spec``): ``spec.yaml`` (status=specified,
current_phase=plan, summary=<intent>), a ``requirements.yaml`` + ``design.yaml``
derived from the intent, and a ``phase-log.yaml`` recording a completed spec phase.
The claude lane resolves the plan phase from ``spec.yaml current_phase: plan`` (it
reads the runner packet best-effort; the phase-batch file need not exist for the
claude lane). So ONLY the plan phase ever runs ``claude -p``.

VALIDITY CAVEAT (read this)
===========================
hivemind only HELPS if it already holds relevant prior decisions to RECALL at plan
time. With an empty hive, ``recall_hits=0`` / ``decisions_reused=0`` and the gain
is structurally ~0. The benchmark intents are a small RELATED module family
(``finance value-object`` helpers sharing conventions/decisions), and ``--seed``
writes decision/learned memories that are ON-TOPIC for those intents so the
hivemind arm's plan-time recall plausibly returns them.

COST CONSTRAINT
===============
A real plan phase runs ``claude -p`` (Max subscription, minutes each). Use
``--dry-run`` to preview the temp config path + plan_gate + isolated queue + the
exact arms / per-spec commands / env deltas WITHOUT executing anything. Only run
the live path when you intend to spend that budget.

SUBCOMMANDS / FLAGS
===================
  --draft N            synthesize N plan-ready benchmark specs PER ARM (unpaired)
                       and record their ids (writes a manifest). No claude spend.
  --specs id1,id2,...  use an existing comma-separated spec set instead (applied
                       to every arm; for advanced/manual use).
  --seed               write related decision/learned memories into hivemind so
                       the hivemind arm has something to recall (do this BEFORE
                       the hivemind arm; it is a no-op without live hivemind env).
  --run                run each arm's plan phases under that arm's env.
  --arms off,hivemind  restrict / order which arms run (default: off,hivemind).
  --report             after the arms, invoke builder-memory-gain.py report.
  --telegram           with --report, POST the report to Telegram.
  --dry-run            print exactly what WOULD run; execute nothing. Safe.
  --cleanup            cancel queued work + archive the throwaway benchmark specs
                       and remove the temp config + isolated queue.

Reuses the existing substrate: the dispatch CLI (drafting / running phases), the
``_telemetry.memory_eval`` helpers, ``memory_hook`` for the seed write path, and
``builder-memory-gain.py`` for the report. It does NOT reinvent any of them and
NEVER touches the live ``dispatch.yaml`` or the live ``dispatch-queue``.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from _dispatch_runtime.paths import runtime_dir
from typing import Any, Sequence

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# The two env vars that, when both present in the dispatcher process, flip the
# arm from "off" to "hivemind". STRIPPING both => off; PRESENT => hivemind.
HIVEMIND_ENV_VARS = ("HIVEMIND_MCP_URL", "HIVEMIND_API_KEY")

ARM_OFF = "off"
ARM_HIVEMIND = "hivemind"

# Recall/distill arms (additive). Each maps to the env it sets in the
# PLAN subprocess (see ARM_ENV_FLAGS / arm_env). They are HIVEMIND-on treatment
# arms that differ only in MEMORY_RECALL_MODE / distill+budget knobs, so each emits
# memory_mode "hivemind" but a distinct recall_mode (and distilled/deduped) in the
# telemetry — letting the report separate push-raw vs push-distilled vs pull.
ARM_PUSH_RAW = "push-raw"
ARM_PUSH_DISTILLED = "push-distilled"
ARM_PULL = "pull"
# Hybrid: a small injected prior-art block (push, low budget) PLUS the on-demand
# pull tool — the best-of-both candidate from the off/push-distilled/pull A/B.
ARM_HYBRID = "push-pull"

# The legacy default keeps the original off-vs-hivemind A/B working unchanged.
DEFAULT_ARMS = (ARM_OFF, ARM_HIVEMIND)

# Every arm this runner understands (legacy + recall/distill). Anything else is rejected.
KNOWN_ARMS = (ARM_OFF, ARM_HIVEMIND, ARM_PUSH_RAW, ARM_PUSH_DISTILLED, ARM_PULL, ARM_HYBRID)

# Arms that require the HIVEMIND_* vars to be PRESENT in the dispatcher subprocess
# (the treatment arms). ARM_OFF is the only arm that STRIPS them.
HIVEMIND_ON_ARMS = (ARM_HIVEMIND, ARM_PUSH_RAW, ARM_PUSH_DISTILLED, ARM_PULL, ARM_HYBRID)

# A cheap distiller model for the push-distilled arm. Use the Claude Code model
# ALIAS ("haiku"), NOT a dated API id: the claude lane runs on the Max subscription
# with ANTHROPIC keys scrubbed, where dated ids like "claude-3-5-haiku-latest" 404.
# "haiku" resolves to the current Haiku. Override via the env if needed.
DEFAULT_DISTILL_MODEL = "haiku"

# Per-arm NON-HIVEMIND env knobs layered on top of the HIVEMIND_* presence toggle.
# These set MEMORY_RECALL_MODE and the distill/budget/gate flags per the SHARED
# CONTRACT arm->env mapping. ARM_OFF / ARM_HIVEMIND set NONE of these (legacy
# behavior: today's default env), so the original A/B is byte-for-byte unchanged.
ARM_ENV_FLAGS: dict[str, dict[str, str]] = {
    ARM_PUSH_RAW: {
        "MEMORY_RECALL_MODE": "push",
    },
    ARM_PUSH_DISTILLED: {
        "MEMORY_RECALL_MODE": "push",
        "MEMORY_DISTILL_MODEL": DEFAULT_DISTILL_MODEL,
        # The seeded prior-art block is ~1744 chars, so a 4000 budget NEVER binds and
        # the item-2 injection cut is never exercised. 800 binds and surfaces the cut.
        "PRIOR_ART_CHAR_BUDGET": "800",
        "PRIOR_ART_REL_GATE": "0.5",
    },
    ARM_PULL: {
        "MEMORY_RECALL_MODE": "pull",
    },
    ARM_HYBRID: {
        "MEMORY_RECALL_MODE": "hybrid",
        "MEMORY_DISTILL_MODEL": DEFAULT_DISTILL_MODEL,
        # Small injected seed (vs push-distilled's 800) so hybrid is a cheap recall
        # floor; the agent pulls more on demand via the recall-only MCP tool.
        "PRIOR_ART_CHAR_BUDGET": "400",
        "PRIOR_ART_REL_GATE": "0.5",
    },
}

# The non-hivemind env keys this runner may set for the recall/distill arms. Used by
# arm_env to STRIP any inherited value on arms that do not set it, so an arm's env
# is fully determined by its own mapping (no leakage from the parent process or a
# prior arm).
ARM_FLAG_KEYS = (
    "MEMORY_RECALL_MODE",
    "MEMORY_DISTILL_MODEL",
    "PRIOR_ART_CHAR_BUDGET",
    "PRIOR_ART_REL_GATE",
)

# Short, filesystem-safe per-arm spec-id tokens.
ARM_TOKEN = {
    ARM_OFF: "off",
    ARM_HIVEMIND: "hive",
    ARM_PUSH_RAW: "praw",
    ARM_PUSH_DISTILLED: "pdis",
    ARM_PULL: "pull",
    ARM_HYBRID: "ppul",
}

# Where this runner records the benchmark spec ids it drafted (so --run / --report
# / --cleanup operate on the same set without re-typing ids). The manifest now also
# carries the run id, the temp config path, and the isolated queue path so cleanup
# can tear them down.
MANIFEST_PARTS = Path("telemetry") / "ab-memory-gain" / "benchmark-manifest.json"

# The dispatch + report CLIs live as hyphenated scripts (not importable by name).
SCRIPTS_DIR = Path(__file__).resolve().parent
DISPATCH_CLI = SCRIPTS_DIR / "builder-dispatch.py"
GAIN_CLI = SCRIPTS_DIR / "builder-memory-gain.py"

# A bank of related decision/learned seeds. They share a topic ("immutable finance
# value-object helper module family with shared conventions") with the benchmark
# intents so a hivemind-arm plan recall actually returns them. The point is a
# non-empty, ON-TOPIC body of prior art that the plan phase can reuse.
SEED_MEMORIES: tuple[dict[str, str], ...] = (
    {
        "type": "decision",
        "content": "Finance value-object helpers (Money, Rate, Percentage) are "
        "IMMUTABLE: construct-and-return a new value, never mutate in place; expose "
        "no setters. This keeps ledger math referentially transparent.",
    },
    {
        "type": "decision",
        "content": "Represent monetary amounts as integer minor units (cents) inside "
        "value objects; never use binary floats for money. Conversions to/from major "
        "units happen only at the formatting boundary.",
    },
    {
        "type": "decision",
        "content": "Every finance value-object helper validates its inputs with Zod at "
        "the constructor boundary and raises a typed DomainError on invalid input — "
        "no silent coercion, no None/null returns from a total operation.",
    },
    {
        "type": "decision",
        "content": "Co-locate each value-object helper's unit test beside the module "
        "(src/finance/value/<name>.ts + tests/finance/value/<name>.test.ts) and cover "
        "the zero/identity case, one representative case, and one invalid-input raise.",
    },
    {
        "type": "decision",
        "content": "Currency mixing is a hard error: arithmetic across two Money values "
        "with different currency codes raises CurrencyMismatchError rather than "
        "implicitly converting; conversion goes through an explicit Rate value object.",
    },
    {
        "type": "learned",
        "content": "Past pitfall: a rounding helper that used float division shipped a "
        "half-cent drift across thousands of ledger rows. Always round in integer "
        "minor units with an explicit, documented rounding mode (banker's rounding).",
    },
    {
        "type": "learned",
        "content": "Past pitfall: adding a value-object helper without an invalid-input "
        "test let a negative-rate bug through. Always pair the helper with a test that "
        "asserts the DomainError raise for out-of-range input.",
    },
    {
        "type": "learned",
        "content": "Past pitfall: two value objects compared by reference instead of by "
        "value caused a confirmed-amount mismatch. Give every value object a structural "
        "equals() and test it; never rely on identity equality.",
    },
)

# The seed write is tagged with this module/spec so it is identifiable + idempotent
# to clean up; it does NOT collide with real spec provenance.
SEED_MODULE = "ab-memory-gain-seed"
SEED_SPEC_ID = "ab-memory-gain-seed"


# ---------------------------------------------------------------------------
# Pure logic (unit-tested) — arm -> env delta, command building, dry-run shape
# ---------------------------------------------------------------------------


def _set_hivemind(env: dict[str, str], base_env: dict[str, str], live_hivemind: dict[str, str] | None) -> None:
    """Set both HIVEMIND_* vars on ``env`` from ``live_hivemind`` (or ``base_env``),
    leaving a var untouched when neither source has it (orchestration validates
    presence separately and refuses to run a treatment arm without both vars)."""
    source = live_hivemind if live_hivemind is not None else base_env
    for var in HIVEMIND_ENV_VARS:
        value = source.get(var)
        if value is None:
            continue
        env[var] = value


def arm_env(
    arm: str,
    base_env: dict[str, str],
    live_hivemind: dict[str, str] | None = None,
) -> dict[str, str]:
    """Return the dispatcher subprocess env for ``arm``, derived from ``base_env``.

    OFF arm: both HIVEMIND_* vars are STRIPPED (guaranteed-absent control); the
    Recall/distill flag knobs are left exactly as in ``base_env`` (today's behavior).
    HIVEMIND arm (legacy): both HIVEMIND_* vars are PRESENT — taken from
    ``live_hivemind`` when provided, else carried through from ``base_env``; NO
    recall/distill knobs are touched, so the original off-vs-hivemind A/B is byte-for-byte
    unchanged.

    Recall/distill treatment arms (push-raw / push-distilled / pull): both HIVEMIND_* vars
    are PRESENT AND the arm's own MEMORY_RECALL_MODE / distill / budget / gate flags
    (``ARM_ENV_FLAGS``) are applied; every OTHER recall/distill flag key is STRIPPED so the
    arm's env is fully determined by its mapping (no leakage from the parent process
    or a previous arm). memory_mode for these is still "hivemind" (both vars set);
    they differ only in recall_mode / distilled / deduped telemetry.

    Pure: never mutates ``base_env``; returns a fresh dict. This is the single
    source of truth for the A/B toggle and is the function the unit tests pin.
    """
    env = dict(base_env)
    if arm == ARM_OFF:
        for var in HIVEMIND_ENV_VARS:
            env.pop(var, None)
        return env
    if arm == ARM_HIVEMIND:
        # Legacy arm: HIVEMIND on, no recall/distill knobs touched (today's default env).
        _set_hivemind(env, base_env, live_hivemind)
        return env
    if arm in ARM_ENV_FLAGS:
        _set_hivemind(env, base_env, live_hivemind)
        flags = ARM_ENV_FLAGS[arm]
        # Strip every recall/distill flag key first so a prior arm / inherited value cannot
        # leak, then apply this arm's mapping.
        for key in ARM_FLAG_KEYS:
            env.pop(key, None)
        for key, value in flags.items():
            env[key] = value
        return env
    raise ValueError(
        f"unknown arm: {arm!r} (expected one of {', '.join(repr(a) for a in KNOWN_ARMS)})"
    )


def env_delta(arm: str, base_env: dict[str, str], live_hivemind: dict[str, str] | None = None) -> dict[str, str]:
    """Human-readable summary of how ``arm``'s env differs from ``base_env`` for the
    two HIVEMIND_* vars only (the only vars this runner touches). Values are masked
    so a dry-run print never leaks the api key. Used by the dry-run renderer."""
    target = arm_env(arm, base_env, live_hivemind)
    delta: dict[str, str] = {}
    for var in HIVEMIND_ENV_VARS:
        in_base = var in base_env
        in_target = var in target
        if in_target and not in_base:
            delta[var] = "SET (was unset)"
        elif in_target and in_base:
            delta[var] = "kept SET"
        elif not in_target and in_base:
            delta[var] = "STRIPPED"
        else:
            delta[var] = "absent"
    return delta


def hivemind_available(env: dict[str, str]) -> bool:
    """True iff both HIVEMIND_* vars are present (and non-empty) in ``env``."""
    return all(bool(env.get(var)) for var in HIVEMIND_ENV_VARS)


def arm_memory_mode(arm: str) -> str:
    """The dispatcher ``memory_mode`` a given arm WILL emit. ``off`` => "off"; every
    HIVEMIND-on arm (legacy hivemind + push-raw / push-distilled / pull) => "hivemind"
    (both HIVEMIND_* vars present). The treatment arms are then distinguished in the
    telemetry by recall_mode / distilled / deduped, not by memory_mode."""
    if arm == ARM_OFF:
        return ARM_OFF
    if arm in HIVEMIND_ON_ARMS:
        return ARM_HIVEMIND
    raise ValueError(f"unknown arm: {arm!r}")


def arm_flag_delta(arm: str) -> dict[str, str]:
    """The recall/distill (non-HIVEMIND) env flags this arm sets, for the dry-run print.
    Empty for the legacy off / hivemind arms (which touch no recall/distill knobs). The
    distiller model and api-bearing values are NOT secrets here, but kept terse."""
    return dict(ARM_ENV_FLAGS.get(arm, {}))


def plan_phase_ref(root: Path | str, spec_id: str | None = None) -> str:
    """The dispatcher runner_task_ref for a spec's PLAN phase batch."""
    if spec_id is None:
        # Retain the original public helper shape used by callers that do not
        # need runtime-directory selection.
        spec_id = str(root)
        return f".builder/specs/{spec_id}/runs/phase-plan.yaml"
    return f"{runtime_dir(root).name}/specs/{spec_id}/runs/phase-plan.yaml"


def enqueue_plan_command(config_path: str, spec_id: str, lane: str = "claude") -> list[str]:
    """The dispatch CLI argv that ENQUEUES a spec's plan phase batch."""
    return [
        sys.executable, str(DISPATCH_CLI),
        "--config", config_path,
        "enqueue", plan_phase_ref(Path(config_path).resolve().parent.parent, spec_id),
        "--lane", lane,
    ]


def run_once_command(config_path: str) -> list[str]:
    """The dispatch CLI argv that DISPATCHES a single queued item, waits, exits.
    Run once per (spec, arm) so its env (and thus its memory_mode) is controlled."""
    return [sys.executable, str(DISPATCH_CLI), "--config", config_path, "run", "--once"]


def draft_command(config_path: str, intent: str, spec_id: str, lane: str = "claude") -> list[str]:
    """The dispatch CLI argv that drafts one benchmark spec from an intent.

    NOTE: with the synthesize-plan-ready preparation path this is retained only for
    the dry-run/manual record; the live ``--draft`` flow synthesizes plan-ready
    artifacts directly (prepare_plan_ready_spec) and never enqueues a spec phase."""
    return [
        sys.executable, str(DISPATCH_CLI),
        "--config", config_path,
        "draft", intent,
        "--spec", spec_id,
        "--lane", lane,
    ]


def benchmark_intent(index: int, arm: str | None = None) -> str:
    """A moderately substantial, COMPARABLE benchmark intent.

    The intents are a small RELATED family: immutable finance value-object helpers
    that share conventions/decisions (integer minor units, Zod-validated
    constructors, typed DomainError, co-located tests, currency-mismatch guard). The
    only thing that varies across ``index`` is which value object the spec adds, so
    the two arms measure the SAME class of work — and the family shares enough
    convention that plan-time recall of the seeded prior art can plausibly help.

    The optional ``arm`` is woven in only as a benign module-namespace token so an
    off-arm spec and a hive-arm spec are distinct on disk while staying comparable.
    """
    families = (
        ("Money", "a monetary amount in integer minor units with currency code",
         "add, subtract, and a currency-mismatch guard"),
        ("Rate", "a conversion/interest rate as a fixed-point decimal",
         "apply-to-Money and compose-with-another-Rate"),
        ("Percentage", "a percentage as basis points",
         "of-Money and clamp-to-[0,100] with validation"),
        ("Quantity", "a non-negative quantity with a unit label",
         "add same-unit, scale-by-Rate, and a unit-mismatch guard"),
    )
    name, what, ops = families[index % len(families)]
    ns = f"_{ARM_TOKEN.get(arm, '')}" if arm else ""
    return (
        f"Add an immutable finance value-object helper `{name}` to the value-object "
        f"module family under src/finance/value/{name.lower()}{ns}.ts. It represents "
        f"{what} and supports {ops}. Follow the family's established conventions: "
        f"immutable construct-and-return (no setters), integer minor units (never "
        f"floats) for money, a Zod-validated constructor that raises a typed "
        f"DomainError on invalid input, a structural equals(), and a co-located unit "
        f"test (zero/identity case, one representative case, one invalid-input raise). "
        f"Plan the tasks; do not implement them."
    )


def benchmark_spec_id(index: int, stamp: str, arm: str | None = None) -> str:
    """A stable, identifiable, throwaway benchmark spec id, namespaced per arm so the
    off and hivemind arms never share a spec directory (unpaired separation)."""
    token = ARM_TOKEN.get(arm, "") if arm else ""
    suffix = f"-{token}" if token else ""
    return f"ab-bench-{stamp}{suffix}-{index}"


def report_command(root: str, out: str | None, telegram: bool) -> list[str]:
    """The builder-memory-gain.py argv for the report subcommand."""
    cmd = [sys.executable, str(GAIN_CLI), "report", "--root", root]
    if out:
        cmd += ["--out", out]
    cmd += ["--telegram"] if telegram else ["--no-telegram"]
    return cmd


# ---------------------------------------------------------------------------
# Temp config override (plan_gate:true + isolated queue) — the isolation core
# ---------------------------------------------------------------------------


def _load_yaml(path: Path) -> dict[str, Any]:
    from _yaml import yaml  # type: ignore

    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: dispatch config must be a mapping")
    return data


def temp_config_path(root: Path, run_id: str) -> Path:
    """Where the temp config lives. MUST sit directly under ``<root>/.builder`` so
    the dispatch CLI's ``project_dir = config.parent.parent`` resolves to ``root``
    (specs live under ``<root>/.builder/specs``)."""
    return runtime_dir(root) / f"ab-dispatch-{run_id}.yaml"


def isolated_queue_path(root: Path, run_id: str) -> Path:
    """The ABSOLUTE isolated queue dir for this run — distinct from the live
    ``dispatch-queue`` so benchmark work never enters the live queue and never races
    the autonomous daemon."""
    return (runtime_dir(root) / f"ab-queue-{run_id}").resolve()


def build_temp_config(source_config: Path, root: Path, run_id: str) -> tuple[Path, Path]:
    """Write a TEMP dispatch config derived from ``source_config`` that overrides
    EXACTLY the keys the A/B run needs, leaving everything else (lanes, routing,
    notify, cooldown) intact:

      * ``pipeline.plan_gate = True``   -> the scheduler holds after the plan phase
        instead of auto-enqueuing implement (the live config has plan_gate: false).
      * ``queue_store.path = <abs isolated dir>`` -> all benchmark queue items / gate
        markers / lanes / events land in an isolated temp queue.
      * ``retry_policy.max_attempts = 1`` -> a benchmark plan attempt NEVER re-queues.
        The live policy (isanna: max_attempts:2, initial_seconds:30) would, on the
        FIRST RETRYABLE_ERROR / RATE_LIMITED (realistic on the Max subscription),
        re-queue the item as QUEUED with ``scheduled_after = now+30s`` (see
        ``backoff.py``) — a non-terminal, time-deferred item that ``run --once`` cannot see
        (it is filtered by scheduler._dispatchable_items) yet that becomes eligible
        again >30s later under the NEXT arm's env, contaminating the A/B. With
        max_attempts=1 the first failure goes straight to FAILED (terminal,
        ``scheduled_after=None``; backoff.py 39-43), so nothing ever lingers QUEUED
        across the arm boundary. The drain check below still verifies this.

    Returns ``(temp_config_path, isolated_queue_path)``. Does NOT touch the live
    config. The temp config is placed so its ``parent.parent == root``.
    """
    from _yaml import yaml  # type: ignore

    data = _load_yaml(source_config)
    queue_dir = isolated_queue_path(root, run_id)

    # Override queue_store.path (absolute) — isolation from the live dispatch-queue.
    qs = dict(data.get("queue_store") or {})
    qs["path"] = str(queue_dir)
    data["queue_store"] = qs

    # Override pipeline.plan_gate (True) — STOP after plan, never auto-advance.
    pipeline = dict(data.get("pipeline") or {})
    pipeline["plan_gate"] = True
    data["pipeline"] = pipeline

    # Override retry_policy.max_attempts (1) — a failed benchmark plan goes terminal
    # (FAILED) instead of re-queuing a future-scheduled QUEUED item that would leak
    # into the next arm's env. Keep the other retry knobs from the source config.
    retry = dict(data.get("retry_policy") or {})
    retry["max_attempts"] = 1
    data["retry_policy"] = retry

    cfg_path = temp_config_path(root, run_id)
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    # CLEAR + recreate the isolated queue so a prior --run's residue can NEVER survive
    # into this run. run_id is the manifest stamp on a --run-without---draft, so the
    # SAME dir is reused; without this, a deferred/retried QUEUED item left by an
    # earlier --run would be dispatched under whichever arm runs first. The dir is the
    # per-run ABSOLUTE ab-queue-<runid> (never the live dispatch-queue), so removing it
    # is safe. Best-effort: never let a stale-FS error abort the run.
    if queue_dir.exists():
        shutil.rmtree(queue_dir, ignore_errors=True)
    queue_dir.mkdir(parents=True, exist_ok=True)
    return cfg_path, queue_dir


def new_run_id() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H%M%S")


# ---------------------------------------------------------------------------
# Plan-ready spec synthesis (no claude spend on the spec phase)
# ---------------------------------------------------------------------------


def _yaml_dump(data: dict[str, Any]) -> str:
    from _yaml import yaml  # type: ignore

    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True)


def prepare_plan_ready_spec(specs_dir: Path, spec_id: str, intent: str) -> Path:
    """Synthesize a PLAN-READY spec directory directly (no spec phase / no claude).

    Writes the minimum artifacts the plan phase needs to start and to RECALL prior
    art for its intent:
      * spec.yaml      status=specified, current_phase=plan, summary=<intent>
      * requirements.yaml + design.yaml  (intent-derived, so the plan has real input)
      * phase-log.yaml  with a completed `spec` phase entry

    The claude lane resolves the plan phase from spec.yaml ``current_phase: plan``
    and recalls prior art using ``summary`` — so this is all that is required. Returns
    the spec dir.
    """
    spec_dir = specs_dir / spec_id
    spec_dir.mkdir(parents=True, exist_ok=True)
    today = datetime.date.today().isoformat()
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    flat = " ".join((intent or "").split()) or "(no intent provided)"

    (spec_dir / "spec.yaml").write_text(
        _yaml_dump({
            "name": spec_id,
            "created": today,
            "status": "specified",
            "current_phase": "plan",
            "next_action": f"/isanna-plan {spec_id}",
            "summary": flat,
        }),
        encoding="utf-8",
    )
    (spec_dir / "requirements.yaml").write_text(
        _yaml_dump({
            "artifact": "requirements",
            "title": f"{spec_id} — finance value-object helper",
            "spec": spec_id,
            "requirements": [
                {
                    "id": "R1",
                    "title": "Provide the immutable value-object helper described by the intent",
                    "user_story": f"As the finance value-object module family, I want: {flat}",
                    "acceptance": [
                        "WHEN constructed with valid input, the system SHALL return an "
                        "immutable value object exposing no setters.",
                        "WHEN constructed with invalid input, the system SHALL raise a "
                        "typed DomainError (no silent coercion, no null return).",
                        "The helper SHALL be pure and referentially transparent for "
                        "in-range inputs.",
                    ],
                },
                {
                    "id": "R2",
                    "title": "Co-locate a unit test covering identity, a representative case, and an invalid-input raise",
                    "user_story": "As a maintainer, I want the helper paired with an asserting unit test.",
                    "acceptance": [
                        "The unit test SHALL cover the zero/identity case, one "
                        "representative non-trivial case, and one invalid-input raise.",
                    ],
                },
            ],
        }),
        encoding="utf-8",
    )
    (spec_dir / "design.yaml").write_text(
        _yaml_dump({
            "artifact": "design",
            "title": f"{spec_id} — finance value-object helper",
            "spec": spec_id,
            "responsibility_allocation": [
                {
                    "surface": "Value-object helper module",
                    "keep": "The finance value-object family's conventions: immutability, "
                            "integer minor units, Zod-validated constructors, typed DomainError.",
                    "change": "Add the new helper module under src/finance/value/ per the intent.",
                    "why": "R1 needs a new immutable value object consistent with the family.",
                },
                {
                    "surface": "Unit test",
                    "keep": "The existing co-located test layout under tests/finance/value/.",
                    "change": "Add a co-located unit test for the new helper.",
                    "why": "R2 requires an asserting test paired with the helper.",
                },
            ],
            "decisions": [
                "Immutable construct-and-return; no setters.",
                "Integer minor units for money; never floats.",
                "Zod-validated constructor raising a typed DomainError on invalid input.",
                "Structural equals(); never identity equality.",
            ],
        }),
        encoding="utf-8",
    )
    (spec_dir / "phase-log.yaml").write_text(
        _yaml_dump({
            "phases": [
                {
                    "phase": "spec",
                    "completed": now,
                    "used_model": "opus",
                    "used_model_class": "deep_reasoner",
                    "files_written": [
                        f"{runtime_dir(root).name}/specs/{spec_id}/requirements.yaml",
                        f"{runtime_dir(root).name}/specs/{spec_id}/design.yaml",
                        f"{runtime_dir(root).name}/specs/{spec_id}/spec.yaml",
                    ],
                    "outcome": "SUCCEEDED",
                    "notes": "Synthesized plan-ready by ab-memory-gain (no claude spend; "
                             "spec phase pre-materialized so only the plan phase is measured).",
                },
            ],
        }),
        encoding="utf-8",
    )
    return spec_dir


# ---------------------------------------------------------------------------
# Dry-run plan (no side effects)
# ---------------------------------------------------------------------------


def build_dry_run_plan(
    *,
    config_path: str,
    root: str,
    arm_specs: dict[str, list[str]],
    arms: Sequence[str],
    base_env: dict[str, str],
    live_hivemind: dict[str, str] | None,
    do_seed: bool,
    do_report: bool,
    telegram: bool,
    temp_config: str | None = None,
    isolated_queue: str | None = None,
    plan_gate: bool = True,
) -> dict[str, Any]:
    """Build a fully-structured description of what the live run WOULD do, with no
    side effects. ``arm_specs`` maps each arm to ITS OWN (unpaired) spec ids. The
    plan surfaces the isolation guarantees (temp config path, plan_gate, isolated
    queue) so a dry-run proves the experiment is clean before any budget is spent."""
    steps: list[dict[str, Any]] = []

    if do_seed:
        steps.append({
            "kind": "seed",
            "memories": len(SEED_MEMORIES),
            "module": SEED_MODULE,
            "requires_hivemind_env": True,
            "note": "writes related decision/learned memories into hivemind so the "
                    "hivemind arm can recall them (else the gain is ~0)",
        })

    for arm in arms:
        for spec_id in arm_specs.get(arm, []):
            steps.append({
                "kind": "plan",
                "arm": arm,
                "spec_id": spec_id,
                "enqueue_command": enqueue_plan_command(config_path, spec_id),
                "run_command": run_once_command(config_path),
                "env_delta": env_delta(arm, base_env, live_hivemind),
                "flag_delta": arm_flag_delta(arm),
                "memory_mode_expected": arm_memory_mode(arm),
            })

    report: dict[str, Any] | None = None
    if do_report:
        report = {
            "kind": "report",
            "command": report_command(root, None, telegram),
            "telegram": telegram,
        }

    return {
        "arms": list(arms),
        "arm_specs": {a: list(arm_specs.get(a, [])) for a in arms},
        "specs": sorted({s for a in arms for s in arm_specs.get(a, [])}),
        "seed": do_seed,
        "steps": steps,
        "report": report,
        "isolation": {
            "config_path": config_path,
            "temp_config": temp_config,
            "isolated_queue": isolated_queue,
            "plan_gate": plan_gate,
            "note": "temp config overrides plan_gate:true (STOP after plan) + an "
                    "isolated queue (never touches the live dispatch-queue or daemon)",
        },
    }


def render_dry_run(plan: dict[str, Any]) -> str:
    """Render the dry-run plan to a readable, copy-pasteable text block."""
    lines: list[str] = []
    lines.append("=== A/B memory-gain DRY RUN (nothing executed) ===")
    iso = plan.get("isolation") or {}
    lines.append(f"config (used): {iso.get('config_path', '?')}")
    if iso.get("temp_config"):
        lines.append(f"temp config  : {iso['temp_config']}  (parent.parent => project root)")
    lines.append(f"plan_gate    : {iso.get('plan_gate')}  (true => STOP after plan; no auto-advance to implement)")
    if iso.get("isolated_queue"):
        lines.append(f"isolated queue: {iso['isolated_queue']}  (NOT the live dispatch-queue)")
    lines.append(f"arms : {', '.join(plan['arms'])}  (unpaired: separate specs per arm)")
    for arm in plan["arms"]:
        ids = plan.get("arm_specs", {}).get(arm, [])
        lines.append(f"  {arm:<8} specs: {', '.join(ids) or '(none)'}")
    lines.append(f"seed : {'yes' if plan['seed'] else 'no'}")
    lines.append("")
    for i, step in enumerate(plan["steps"], 1):
        if step["kind"] == "seed":
            lines.append(f"[{i}] SEED -> hivemind: write {step['memories']} memories "
                         f"(module={step['module']}, requires HIVEMIND_* env)")
            lines.append(f"      note: {step['note']}")
        elif step["kind"] == "plan":
            lines.append(f"[{i}] PLAN arm={step['arm']:<14} spec={step['spec_id']} "
                         f"=> expected memory_mode={step['memory_mode_expected']} (phase=4-plan only)")
            lines.append(f"      env delta: " + ", ".join(f"{k}={v}" for k, v in step["env_delta"].items()))
            flag_delta = step.get("flag_delta") or {}
            if flag_delta:
                lines.append(f"      flag delta: " + ", ".join(f"{k}={v}" for k, v in flag_delta.items()))
            lines.append(f"      enqueue:   {' '.join(step['enqueue_command'])}")
            lines.append(f"      run:       {' '.join(step['run_command'])}")
        lines.append("")
    if plan["report"]:
        lines.append("[report] " + " ".join(plan["report"]["command"]))
        lines.append(f"         telegram: {'yes' if plan['report']['telegram'] else 'no'}")
    else:
        lines.append("[report] (skipped — pass --report to render the gain report)")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Manifest (idempotent record of the throwaway benchmark spec set + isolation)
# ---------------------------------------------------------------------------


def _manifest_path(root: Path) -> Path:
    return runtime_dir(root) / MANIFEST_PARTS


def load_manifest(root: Path) -> dict[str, Any]:
    path = _manifest_path(root)
    if not path.exists():
        return {"spec_ids": [], "stamp": None}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"spec_ids": [], "stamp": None}


def save_manifest(
    root: Path,
    spec_ids: list[str],
    stamp: str | None,
    *,
    arm_specs: dict[str, list[str]] | None = None,
    temp_config: str | None = None,
    isolated_queue: str | None = None,
) -> Path:
    path = _manifest_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {"spec_ids": spec_ids, "stamp": stamp}
    if arm_specs is not None:
        payload["arm_specs"] = arm_specs
    if temp_config is not None:
        payload["temp_config"] = temp_config
    if isolated_queue is not None:
        payload["isolated_queue"] = isolated_queue
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def resolve_spec_ids(root: Path, explicit: str | None) -> list[str]:
    """Resolve the benchmark spec set: explicit --specs wins, else the manifest."""
    if explicit:
        return [s.strip() for s in explicit.split(",") if s.strip()]
    return list(load_manifest(root).get("spec_ids", []))


def resolve_arm_specs(root: Path, explicit: str | None, arms: Sequence[str]) -> dict[str, list[str]]:
    """Resolve the per-arm (unpaired) spec sets. Explicit --specs applies the SAME
    list to every arm (manual/advanced use); otherwise the manifest's recorded
    per-arm map wins, falling back to splitting a flat spec_ids list is NOT done —
    a flat list with no arm map means the caller must pass --specs explicitly."""
    if explicit:
        ids = [s.strip() for s in explicit.split(",") if s.strip()]
        return {arm: list(ids) for arm in arms}
    manifest = load_manifest(root)
    arm_specs = manifest.get("arm_specs")
    if isinstance(arm_specs, dict) and arm_specs:
        return {arm: list(arm_specs.get(arm, [])) for arm in arms}
    # No per-arm map recorded — apply the flat spec_ids to every arm as a fallback.
    flat = list(manifest.get("spec_ids", []))
    return {arm: list(flat) for arm in arms}


# ---------------------------------------------------------------------------
# Live orchestration (side-effecting; guarded by --dry-run everywhere it matters)
# ---------------------------------------------------------------------------


def _log(msg: str) -> None:
    print(f"[ab-memory-gain] {msg}", flush=True)


def _run_subprocess(cmd: list[str], *, env: dict[str, str], cwd: str) -> int:
    """Run a subprocess, streaming through; return its exit code."""
    _log(f"exec: {' '.join(cmd)}")
    proc = subprocess.run(cmd, env=env, cwd=cwd)
    return proc.returncode


def _run_subprocess_capture(cmd: list[str], *, env: dict[str, str], cwd: str) -> tuple[int, str]:
    """Run a subprocess CAPTURING stdout+stderr; return ``(returncode, combined_text)``.

    Used by the drain loop so we can inspect the dispatcher's ``dispatched [...]`` line
    to decide whether the isolated queue still has work. The captured text is logged
    (trimmed) so progress stays visible even though we are not streaming live."""
    _log(f"exec: {' '.join(cmd)}")
    proc = subprocess.run(cmd, env=env, cwd=cwd, capture_output=True, text=True)
    out = (proc.stdout or "") + (proc.stderr or "")
    tail = out.strip().splitlines()[-3:] if out.strip() else []
    for line in tail:
        _log(f"  | {line}")
    return proc.returncode, out


def _run_dispatched_nothing(output: str) -> bool:
    """True iff a ``run --once`` capture indicates the queue dispatched NOTHING (drained).

    The dispatcher prints a line like ``dispatched ['work-<id>']`` when it dispatched
    an item, or an empty/zero list / a "nothing" message when the queue had no eligible
    item. Treat empty output, ``dispatched []``, and any "nothing" mention as drained;
    a ``dispatched ['work-...']`` (a quoted work id inside the brackets) means keep
    going. Defaults to DRAINED on ambiguity so the loop always terminates."""
    text = (output or "").strip()
    if not text:
        return True
    lowered = text.lower()
    if "nothing" in lowered:
        return True
    # Look for the most recent ``dispatched [...]`` token in the output.
    marker = "dispatched ["
    idx = lowered.rfind(marker)
    if idx == -1:
        # No dispatch line at all -> nothing was dispatched.
        return True
    inside = text[idx + len(marker):]
    close = inside.find("]")
    bracket_body = inside[:close] if close != -1 else inside
    # A non-empty list contains a quoted work id (e.g. 'work-...'); empty -> drained.
    return bracket_body.strip() == ""


def _nonterminal_queue_items(isolated_queue: str) -> list[tuple[str, str]]:
    """Inspect the ISOLATED queue store DIRECTLY and return ``[(work_id, state)]`` for
    every item NOT in a terminal state (i.e. QUEUED / DISPATCHED / RUNNING — INCLUDING
    a QUEUED item whose ``scheduled_after`` is in the future).

    This is the authoritative drained check. ``run --once`` stdout cannot prove the
    queue empty: a RETRYABLE_ERROR / RATE_LIMITED attempt re-queues the item as QUEUED
    with a future ``scheduled_after`` (backoff.py), which ``scheduler._dispatchable_items``
    filters out — so the very next ``run --once`` prints "no eligible work" while a
    non-terminal item still sits in the queue, ready to be dispatched under the NEXT
    arm's env. We instead reconstruct the store and check item states against
    ``TERMINAL_STATES`` so a time-deferred QUEUED item is NOT mistaken for empty.

    NEVER raises (the experiment must not die on an inspection error): on any failure
    it returns a sentinel ``[("<inspect-error>", "<reason>")]`` so the caller treats
    the queue as NOT-provably-empty (fail safe) rather than silently declaring drained.
    """
    try:
        from _dispatch_runtime.queue_store import QueueStore
        from _dispatch_runtime.state_model import TERMINAL_STATES

        # The isolated queue path is ABSOLUTE; QueueStore(root) reads <root>/queue/items.
        # The dispatch CLI builds the same store via project_dir / queue_store.path,
        # which collapses to this absolute path (pathlib drops the left side).
        store = QueueStore(isolated_queue)
        snapshot = store.reconstruct()
        return [
            (item.id, item.state.value)
            for item in snapshot.items.values()
            if item.state not in TERMINAL_STATES
        ]
    except Exception as exc:  # noqa: BLE001 - never let inspection kill the experiment
        return [("<inspect-error>", str(exc))]


def _quarantine_queue_residue(isolated_queue: str) -> int:
    """CANCEL every non-terminal item still in the isolated queue so it can NEVER be
    dispatched under a later arm's env. Returns the number transitioned to terminal.

    Used when the drain loop did NOT reach a provably-empty queue (drain-cap hit, or a
    ``dispatch_once`` exception that left an item QUEUED). Cancelling moves QUEUED ->
    CANCELLED (a terminal state; legal per the state model), which removes it from
    ``_dispatchable_items`` permanently. NEVER raises — a quarantine failure is logged
    but must not abort the experiment. DISPATCHED/RUNNING items cannot be cancelled
    mid-flight by this path; they are surfaced so the leak is visible, but the temp
    queue is per-run and torn down by cleanup, so they cannot cross into a later run.
    """
    cancelled = 0
    try:
        from _dispatch_runtime.queue_store import QueueStore
        from _dispatch_runtime.state_model import TERMINAL_STATES, WorkItemState

        store = QueueStore(isolated_queue)
        for item in store.reconstruct().items.values():
            if item.state in TERMINAL_STATES:
                continue
            if item.state == WorkItemState.QUEUED:
                try:
                    store.transition_item(item.id, WorkItemState.CANCELLED)
                    cancelled += 1
                    _log(f"quarantine: cancelled leftover QUEUED item {item.id}")
                except Exception as exc:  # noqa: BLE001 - best effort per item
                    _log(f"quarantine: FAILED to cancel {item.id} (ignored): {exc}")
            else:
                _log(f"quarantine: leftover {item.state.value} item {item.id} "
                     "cannot be cancelled mid-flight (isolated to this run's queue)")
    except Exception as exc:  # noqa: BLE001 - never let quarantine kill the experiment
        _log(f"quarantine FAILED (ignored): {exc}")
    return cancelled


def do_draft(
    root: Path,
    count: int,
    arms: Sequence[str],
    dry_run: bool,
    *,
    stamp: str | None = None,
) -> dict[str, list[str]]:
    """Synthesize ``count`` plan-ready benchmark specs PER ARM (unpaired). No claude
    spend — the spec phase is pre-materialized. Returns the per-arm spec map."""
    stamp = stamp or new_run_id()
    specs_dir = runtime_dir(root) / "specs"
    arm_specs: dict[str, list[str]] = {arm: [] for arm in arms}
    for arm in arms:
        for i in range(count):
            spec_id = benchmark_spec_id(i, stamp, arm)
            intent = benchmark_intent(i, arm)
            arm_specs[arm].append(spec_id)
            if dry_run:
                _log(f"DRY prepare plan-ready spec ({arm}): {spec_id}")
                continue
            prepare_plan_ready_spec(specs_dir, spec_id, intent)
            _log(f"prepared plan-ready spec ({arm}): {spec_id}")
    return arm_specs


def do_seed(root: Path, dry_run: bool, *, client: Any | None = None) -> int:
    """Write the seed decision/learned memories into hivemind via the memory_hook
    write path. No-op (returns 0) when the live hivemind env is unset — seeding an
    unconfigured hive is meaningless. Returns the count written."""
    if dry_run:
        _log(f"DRY seed: would write {len(SEED_MEMORIES)} memories "
             f"(module={SEED_MODULE}) into hivemind")
        return 0
    if client is None and not hivemind_available(dict(os.environ)):
        _log("seed SKIPPED: HIVEMIND_MCP_URL / HIVEMIND_API_KEY not set in this env "
             "(seeding an unconfigured hive is a no-op)")
        return 0

    # Lazy import: reuse the dispatcher's write path, do not reinvent a hive client.
    from _dispatch_runtime.memory_hook import write_decision_memory

    decisions = [m["content"] for m in SEED_MEMORIES if m["type"] == "decision"]
    learned = [m["content"] for m in SEED_MEMORIES if m["type"] == "learned"]
    written = write_decision_memory(SEED_SPEC_ID, SEED_MODULE, decisions, learned, client=client)
    _log(f"seed: wrote {written}/{len(SEED_MEMORIES)} memories into hivemind "
         f"(module={SEED_MODULE})")
    return written


# Maximum ``run --once`` dispatches per spec before we declare the loop stuck. One
# plan item per spec normally drains in a single dispatch (plan_gate stops it from
# advancing); a small cap (> 1) tolerates a slow/empty first dispatch while still
# guaranteeing termination.
MAX_DRAIN_ITERS = 6


def _clear_seed_corpus(dry_run: bool) -> int:
    """Delete every seed memory tagged ``SEED_MODULE`` from the hive. No-op (returns 0)
    when no live hive client is configured. NEVER raises — the experiment must not die
    because a cleanup delete failed."""
    if dry_run:
        _log(f"DRY clear seed corpus: hive_memory_delete tags=[{SEED_MODULE}]")
        return 0
    try:
        from _dispatch_runtime.memory_hook import _hive_client

        client = _hive_client()
        if client is None:
            return 0
        result = client.call("bia_memory_delete", {"tags": [SEED_MODULE]})
        deleted = int((result or {}).get("deleted", 0)) if isinstance(result, dict) else 0
        _log(f"cleared seed corpus: deleted {deleted} memories (tags=[{SEED_MODULE}])")
        return deleted
    except Exception as exc:  # noqa: BLE001 - seed clear is best-effort; never fatal
        _log(f"clear seed corpus FAILED (ignored): {exc}")
        return 0


def _reseed_for_arm(arm: str, arm_runtime_env: dict[str, str], dry_run: bool) -> int:
    """RE-SEED the seed corpus with THIS arm's env applied to ``os.environ`` so the
    distill-at-write knobs (MEMORY_DISTILL_MODEL / budget / gate) are in effect for the
    write — push-distilled then stores a DISTILLED corpus, push-raw a RAW one. Restores
    ``os.environ`` afterward. No-op when no live hive client is configured. NEVER raises."""
    if dry_run:
        _log(f"DRY re-seed ({arm}): write {len(SEED_MEMORIES)} memories "
             f"(module={SEED_MODULE}) with arm env applied (distill in effect for push-distilled)")
        return 0
    try:
        from _dispatch_runtime.memory_hook import _hive_client, write_decision_memory

        # Skip cleanly when no live hive is configured (write would be a no-op anyway).
        if _hive_client() is None:
            return 0
        decisions = [m["content"] for m in SEED_MEMORIES if m["type"] == "decision"]
        learned = [m["content"] for m in SEED_MEMORIES if m["type"] == "learned"]
        saved_env = dict(os.environ)
        try:
            os.environ.clear()
            os.environ.update(arm_runtime_env)
            written = write_decision_memory(SEED_SPEC_ID, SEED_MODULE, decisions, learned)
        finally:
            os.environ.clear()
            os.environ.update(saved_env)
        _log(f"re-seed ({arm}): wrote {written}/{len(SEED_MEMORIES)} memories "
             f"(module={SEED_MODULE}, distill={'on' if arm_runtime_env.get('MEMORY_DISTILL_MODEL') else 'off'})")
        return written
    except Exception as exc:  # noqa: BLE001 - re-seed is best-effort; never fatal
        _log(f"re-seed ({arm}) FAILED (ignored): {exc}")
        return 0


def do_run_arms(
    config_path: str,
    root: Path,
    arm_specs: dict[str, list[str]],
    arms: Sequence[str],
    dry_run: bool,
    *,
    seed: bool = False,
    isolated_queue: str | None = None,
) -> int:
    """Run each (arm, spec) plan phase: enqueue the plan batch, then DRAIN the isolated
    queue (loop ``run --once``, verifying the STORE is empty) under the arm's env.
    Unpaired — each arm runs ITS OWN specs. Returns the number of plan runs attempted.
    ``config_path`` MUST be the temp config (plan_gate true + isolated queue) so no
    implement/verify is ever dispatched. ``isolated_queue`` is the ABSOLUTE isolated
    queue dir — required (in the live path) so the drain check can inspect the store
    DIRECTLY instead of inferring emptiness from ``run --once`` stdout.

    PER-ARM SEED ISOLATION (``seed=True``): before each HIVEMIND-ON arm's specs, the seed
    corpus is CLEARED then RE-SEEDED with THAT arm's env applied — so push-distilled stores
    a distilled corpus and push-raw a raw one, and the distillation-at-write is actually
    measured (no shared corpus across arms). The off arm seeds nothing. After ALL arms
    finish, the seed corpus is cleared once more so the hive is left clean. All seeding
    degrades to a no-op when HIVEMIND_* is unset and never raises.

    DRAIN-TO-EMPTY (provable): each spec fully drains the isolated queue before the next
    enqueue. Emptiness is proven by inspecting the STORE (``_nonterminal_queue_items``) —
    NOT by parsing the ``run --once`` dispatch line, which cannot see a time-deferred
    QUEUED item left by a RETRYABLE_ERROR / RATE_LIMITED retry (those are eliminated up
    front by retry_policy.max_attempts=1 in build_temp_config, and the store check is the
    backstop). If any non-terminal item remains after the drain cap, it is QUARANTINED
    (cancelled) before advancing, so the previous arm's residue can NEVER be dispatched
    under the next arm's env."""
    base_env = dict(os.environ)
    live_hivemind = {v: base_env[v] for v in HIVEMIND_ENV_VARS if v in base_env}
    attempted = 0
    for arm in arms:
        if arm in HIVEMIND_ON_ARMS and not dry_run and not hivemind_available(live_hivemind):
            _log(f"{arm} arm SKIPPED: HIVEMIND_* env not present in this process "
                 "(cannot run a treatment arm without it)")
            continue
        env = arm_env(arm, base_env, live_hivemind)

        # Per-arm seed isolation: clear + re-seed the corpus under THIS arm's env so the
        # distill-at-write is measured. Only HIVEMIND-on arms seed (off has no hive).
        if seed and arm in HIVEMIND_ON_ARMS:
            _clear_seed_corpus(dry_run)
            _reseed_for_arm(arm, env, dry_run)

        for spec_id in arm_specs.get(arm, []):
            attempted += 1
            if dry_run:
                _log(f"DRY plan arm={arm} spec={spec_id} "
                     f"delta={env_delta(arm, base_env, live_hivemind)} "
                     f"flags={arm_flag_delta(arm)}")
                _log(f"DRY   enqueue: {' '.join(enqueue_plan_command(config_path, spec_id))}")
                _log(f"DRY   run (drain loop, <= {MAX_DRAIN_ITERS}x): "
                     f"{' '.join(run_once_command(config_path))}")
                continue
            _log(f"PLAN arm={arm} spec={spec_id} (memory_mode expected: {arm_memory_mode(arm)})")
            rc_enq = _run_subprocess(enqueue_plan_command(config_path, spec_id), env=env, cwd=str(root))
            if rc_enq != 0:
                _log(f"warning: enqueue of {spec_id} exited {rc_enq}; skipping run")
                continue
            # Drain the isolated queue: dispatch repeatedly until the STORE holds NO
            # non-terminal item. Bounded so a stuck/never-empty queue cannot loop
            # forever. Emptiness is proven by inspecting the store directly (the
            # run --once stdout dispatch line cannot see a time-deferred QUEUED item),
            # so the previous arm's residue can never leak into the next arm's env.
            drained = False
            for it in range(1, MAX_DRAIN_ITERS + 1):
                rc_run, out = _run_subprocess_capture(
                    run_once_command(config_path), env=env, cwd=str(root)
                )
                if rc_run != 0:
                    _log(f"warning: run --once for {spec_id} ({arm}) exited {rc_run} (iter {it})")
                residue = _nonterminal_queue_items(isolated_queue) if isolated_queue else []
                if not residue:
                    drained = True
                    _log(f"drained queue for {spec_id} ({arm}) after {it} dispatch(es) "
                         "(store verified empty of non-terminal items)")
                    break
                _log(f"drain iter {it} for {spec_id} ({arm}): "
                     f"{len(residue)} non-terminal item(s) remain {residue}")
            if not drained:
                # The drain cap was hit OR a dispatch_once exception left an item QUEUED.
                # A leftover non-terminal item would be eligible under the NEXT arm's env
                # (wrong memory_mode). Quarantine it (cancel -> terminal) before advancing
                # so the cross-arm leak is impossible; the queue is per-run and isolated.
                _log(f"warning: drain cap ({MAX_DRAIN_ITERS}) hit for {spec_id} ({arm}); "
                     "quarantining residue so it cannot run under the next arm's env")
                if isolated_queue:
                    n = _quarantine_queue_residue(isolated_queue)
                    leftover = _nonterminal_queue_items(isolated_queue)
                    if leftover:
                        _log(f"warning: {len(leftover)} item(s) STILL non-terminal after "
                             f"quarantine for {spec_id} ({arm}): {leftover} (in-flight; "
                             "isolated to this run's queue)")
                    else:
                        _log(f"quarantine complete for {spec_id} ({arm}): "
                             f"cancelled {n} item(s); queue now clean")

    # Leave the hive clean: clear the seed corpus once more after all arms finish.
    if seed:
        any_hive_arm = any(a in HIVEMIND_ON_ARMS for a in arms)
        if any_hive_arm:
            _clear_seed_corpus(dry_run)
    return attempted


# ---------------------------------------------------------------------------
# Rubric scoring hook (agent-lift A/B, R1+R4) — BUILD-ONLY, NOT WIRED IN.
#
# This is the post-run attach point that scores each produced plan via the blind,
# k=2, pinned-model rubric judge and stamps `rubric_score` onto that run's
# memory_eval record. It is deliberately a standalone, clearly-named function that
# `do_run_arms` does NOT call: scoring spawns real `claude -p` judge turns and is
# the gated operator step (the run + GO/NO-GO are operator-driven). The arms,
# env mappings, and dispatch flow are UNCHANGED.
# ---------------------------------------------------------------------------

# The plan artifact names a Builder plan phase may produce, newest-convention first.
_PLAN_ARTIFACT_NAMES = ("plan.md", "plan.yaml", "tasks.md", "tasks.yaml")


def _read_plan_text(specs_dir: Path, spec_id: str) -> str | None:
    """Read the produced plan text for ``spec_id`` from its spec dir, trying the known
    plan-artifact names in order. Returns the first that exists, or None when no plan
    artifact is present (the plan run did not produce one). Never raises."""
    spec_dir = Path(specs_dir) / spec_id
    for name in _PLAN_ARTIFACT_NAMES:
        candidate = spec_dir / name
        try:
            if candidate.is_file():
                return candidate.read_text(encoding="utf-8")
        except OSError:
            continue
    return None


def attach_rubric_scores(
    records: list[dict[str, Any]],
    specs_dir: Path,
    *,
    model: str | None = None,
    passes: int = 2,
    scorer: Any | None = None,
) -> list[dict[str, Any]]:
    """Score each plan run's produced plan via the arm-blind rubric judge and return
    a NEW list of records with ``rubric_score`` stamped on (x10 int encoding).

    BUILD-ONLY: this is the documented post-run hook the operator-gated run would
    call to attach rubric scores. It is NOT invoked by ``do_run_arms`` — calling it
    with the default ``scorer`` spawns real ``claude -p`` judge turns, which is the
    gated step. ``scorer`` is injectable (matching ``plan_rubric_judge.score_plan``)
    so the wiring can be exercised without a model.

    For each record it reads the spec's produced plan (``_read_plan_text``), calls
    ``plan_rubric_judge.score_plan`` (blind, k=``passes``, pinned ``model``), and sets
    ``record['rubric_score'] = rubric_score_to_minor_units(result['rubric_score'])``.
    A record whose plan cannot be read, or that the judge leaves unscored (``None``),
    keeps ``rubric_score=0`` (the default). Pure w.r.t. the input list (returns
    copies); never mutates the originals."""
    from _telemetry.plan_rubric_judge import (
        rubric_score_to_minor_units,
        score_plan,
    )

    # Resolve at call time so a post-import env override of RUBRIC_JUDGE_MODEL is
    # respected. model param wins when given; else read fresh from os.environ.
    judge_model = model or os.environ.get("RUBRIC_JUDGE_MODEL", "sonnet")
    out: list[dict[str, Any]] = []
    for rec in records:
        updated = dict(rec)
        spec_id = str(rec.get("spec_id", ""))
        plan_text = _read_plan_text(specs_dir, spec_id) if spec_id else None
        if plan_text is not None:
            result = score_plan(
                plan_text, model=judge_model, passes=passes, scorer=scorer
            )
            updated["rubric_score"] = rubric_score_to_minor_units(result.get("rubric_score"))
        else:
            updated.setdefault("rubric_score", 0)
        out.append(updated)
    return out


# ---------------------------------------------------------------------------
# Arm-aware report (separates push-raw vs push-distilled vs pull — the legacy
# builder-memory-gain.py report only knows off-vs-hivemind)
# ---------------------------------------------------------------------------

# The memory_eval metric columns the arm report renders means over.
_ARM_REPORT_METRICS = (
    "plan_tokens_out",
    "plan_wall_ms",
    "decisions_reused",
    "prior_art_tokens",
    "decisions_distilled",
)


def _spec_to_arm(manifest: dict[str, Any]) -> dict[str, str]:
    """Build a ``spec_id -> arm`` map from a manifest's ``arm_specs`` (for THIS run).
    Empty when the manifest has no per-arm map."""
    arm_specs = manifest.get("arm_specs")
    mapping: dict[str, str] = {}
    if isinstance(arm_specs, dict):
        for arm, ids in arm_specs.items():
            for spec_id in (ids or []):
                mapping[str(spec_id)] = str(arm)
    return mapping


def _mean(values: Sequence[Any]) -> float:
    """Mean over non-None numeric values; 0.0 when there are none. Defensive: any value
    that does not coerce to float is skipped."""
    nums: list[float] = []
    for v in values:
        if v is None:
            continue
        try:
            nums.append(float(v))
        except (TypeError, ValueError):
            continue
    return (sum(nums) / len(nums)) if nums else 0.0


def render_arm_report(manifest: dict[str, Any], records: Sequence[dict[str, Any]]) -> str:
    """PURE: render a per-arm Markdown table from a manifest + memory_eval records.

    Groups ``records`` by arm via the manifest's ``spec_id -> arm`` map (records whose
    spec_id is not in the manifest are IGNORED — they belong to other runs). Renders:
    arm | n | mean plan_tokens_out | mean plan_wall_ms | recall_hit_rate | mean
    decisions_reused | mean prior_art_tokens | mean decisions_distilled.

    recall_hit_rate is the fraction of an arm's records with ``recall_hits > 0``. All
    means skip None / non-numeric values and are 0 for an empty arm. Pure + unit-testable:
    no I/O, no env, no time."""
    spec_to_arm = _spec_to_arm(manifest)

    # Preserve a stable arm order: the manifest's arm order, then any others observed.
    arm_order: list[str] = []
    arm_specs = manifest.get("arm_specs")
    if isinstance(arm_specs, dict):
        arm_order = list(arm_specs.keys())

    grouped: dict[str, list[dict[str, Any]]] = {a: [] for a in arm_order}
    for rec in records:
        spec_id = str(rec.get("spec_id", ""))
        arm = spec_to_arm.get(spec_id)
        if arm is None:
            continue  # not part of this run's manifest
        grouped.setdefault(arm, []).append(rec)
        if arm not in arm_order:
            arm_order.append(arm)

    lines: list[str] = []
    lines.append("# A/B memory-gain — per-arm report")
    lines.append("")
    lines.append(
        "Separates push-raw vs push-distilled vs pull (all `memory_mode=hivemind`, "
        "`recall_mode=push/pull`), which the legacy off-vs-hivemind report cannot."
    )
    lines.append("")
    header = (
        "| arm | n | mean plan_tokens_out | mean plan_wall_ms | recall_hit_rate "
        "| mean decisions_reused | mean prior_art_tokens | mean decisions_distilled |"
    )
    sep = "|---|---|---|---|---|---|---|---|"
    lines.append(header)
    lines.append(sep)

    if not any(grouped.get(a) for a in arm_order):
        lines.append("| _(no records matched this run's manifest)_ |  |  |  |  |  |  |  |")
        return "\n".join(lines)

    for arm in arm_order:
        recs = grouped.get(arm, [])
        n = len(recs)
        m_tokens = _mean([r.get("plan_tokens_out") for r in recs])
        m_wall = _mean([r.get("plan_wall_ms") for r in recs])
        hits = [r for r in recs if _as_int(r.get("recall_hits")) > 0]
        hit_rate = (len(hits) / n) if n else 0.0
        m_reused = _mean([r.get("decisions_reused") for r in recs])
        m_prior = _mean([r.get("prior_art_tokens") for r in recs])
        m_distilled = _mean([r.get("decisions_distilled") for r in recs])
        lines.append(
            f"| {arm} | {n} | {m_tokens:.1f} | {m_wall:.1f} | {hit_rate:.2f} "
            f"| {m_reused:.2f} | {m_prior:.1f} | {m_distilled:.2f} |"
        )
    return "\n".join(lines)


def _as_int(value: Any) -> int:
    """Coerce to int defensively; 0 on failure / None."""
    if value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return 0


def do_report(root: Path, telegram: bool, dry_run: bool) -> int:
    """Render the ARM-AWARE report (separates push-raw / push-distilled / pull), write it
    to ``<root>/.builder/telemetry/reports/ab-arm-report.md`` and PRINT it; then invoke
    the legacy off-vs-hivemind gain report best-effort for backward compat. Returns the
    legacy report's exit code (0 on dry / on best-effort failure)."""
    arm_report_path = runtime_dir(root) / "telemetry" / "reports" / "ab-arm-report.md"
    if dry_run:
        _log(f"DRY arm report: would write + print {arm_report_path}")
    else:
        # Best-effort: a report failure must never crash the orchestration.
        try:
            from _telemetry.memory_eval import load_memory_evals

            manifest = load_manifest(root)
            records = load_memory_evals(Path(root))
            arm_md = render_arm_report(manifest, records)
            arm_report_path.parent.mkdir(parents=True, exist_ok=True)
            arm_report_path.write_text(arm_md, encoding="utf-8")
            _log(f"wrote arm report: {arm_report_path}")
            print(arm_md, flush=True)
        except Exception as exc:  # noqa: BLE001 - arm report is best-effort
            _log(f"arm report FAILED (ignored): {exc}")

    # Legacy off-vs-hivemind report — best-effort, non-fatal, for backward compat.
    cmd = report_command(str(root), None, telegram)
    if dry_run:
        _log(f"DRY report (legacy): {' '.join(cmd)}")
        return 0
    try:
        return _run_subprocess(cmd, env=dict(os.environ), cwd=str(root))
    except Exception as exc:  # noqa: BLE001 - legacy report is best-effort
        _log(f"legacy report FAILED (ignored): {exc}")
        return 0


def _median(values: Sequence[Any]) -> float:
    """Median over non-None numeric values; 0.0 when empty."""
    nums = sorted(float(v) for v in values if v is not None)
    if not nums:
        return 0.0
    mid = len(nums) // 2
    return nums[mid] if len(nums) % 2 else (nums[mid - 1] + nums[mid]) / 2.0


def render_rubric_verdict(manifest: dict[str, Any], records: Sequence[dict[str, Any]]) -> str:
    """PURE: per-arm rubric-score table + the pinned promotion/rollback verdict.

    rubric_score is the x10 int encoding (0..100 == 0..10). Only judge-scored records
    (rubric_score > 0) count toward an arm's rubric stats — the 0 sentinel ('not
    evaluated') is excluded so it cannot deflate means/medians. No I/O; unit-testable."""
    from _telemetry.memory_gain_report import cohens_d, mann_whitney_u

    spec_to_arm = _spec_to_arm(manifest)
    arm_order: list[str] = list((manifest.get("arm_specs") or {}).keys())
    scored: dict[str, list[float]] = {a: [] for a in arm_order}
    prior: dict[str, list[float]] = {a: [] for a in arm_order}
    for rec in records:
        arm = spec_to_arm.get(str(rec.get("spec_id", "")))
        if arm is None:
            continue
        rs = _as_int(rec.get("rubric_score"))
        if rs > 0:
            scored.setdefault(arm, []).append(float(rs))
        pa = rec.get("prior_art_tokens")
        if pa is not None:
            prior.setdefault(arm, []).append(float(pa))

    off = scored.get("off", [])
    lines = ["# A/B rubric (plan-quality) report", ""]
    lines.append("rubric_score is x10 (0..100 == 0..10 rubric). Only judge-scored records "
                 "(rubric_score>0) count toward an arm.")
    lines.append("")
    lines.append("| arm | n_scored | mean | median | Cohen's d vs off | Mann-Whitney p vs off |")
    lines.append("|---|---|---|---|---|---|")
    for arm in arm_order:
        vals = scored.get(arm, [])
        n = len(vals)
        mean = (sum(vals) / n) if n else 0.0
        if arm == "off" or not off or not vals:
            d_s, p_s = "—", "—"
        else:
            d = cohens_d(off, vals)
            p = mann_whitney_u(off, vals)
            d_s = f"{d:.2f}" if d is not None else "null"
            p_s = f"{p:.3f}" if p is not None else "null"
        lines.append(f"| {arm} | {n} | {mean:.1f} | {_median(vals):.1f} | {d_s} | {p_s} |")

    # --- pinned decision rule (rubric x10 units; 0.5 rubric pts == 5) ---------
    def pmean(a: str) -> float:
        v = prior.get(a, [])
        return (sum(v) / len(v)) if v else 0.0

    def mw(a: str, b: str) -> Any:
        try:
            return mann_whitney_u(scored.get(a, []), scored.get(b, []))
        except Exception:  # noqa: BLE001
            return None

    n_off, n_pull, n_pd = len(off), len(scored.get("pull", [])), len(scored.get("push-distilled", []))
    med_off, med_pull, med_pd = _median(off), _median(scored.get("pull", [])), _median(scored.get("push-distilled", []))
    verdict: list[str] = []
    p_pull_off = mw("pull", "off")
    superior = med_pull > med_off and p_pull_off is not None and p_pull_off < 0.05 and min(n_pull, n_off) >= 26
    noninferior = (med_pull >= med_pd - 5) and (pmean("push-distilled") > 0 and pmean("pull") <= 0.7 * pmean("push-distilled"))
    if superior:
        verdict.append("PROMOTE pull — median(pull) > median(off), Mann-Whitney p<0.05, n>=26/arm.")
    elif noninferior:
        verdict.append("PROMOTE pull — non-inferior to push-distilled (within 0.5 rubric pts) AND "
                       "prior_art_tokens reduced >=30% vs push-distilled.")
    else:
        verdict.append("HOLD pull — neither the superiority branch (p<0.05 at n>=26) nor the "
                       "non-inferiority+>=30%-token-savings branch is satisfied.")
    p_pd_off = mw("push-distilled", "off")
    if med_pd < med_off and p_pd_off is not None and p_pd_off < 0.05:
        verdict.append("ROLL BACK push-distilled — median < off with p<0.05 (demonstrated harm).")
    else:
        verdict.append("KEEP push-distilled live — no demonstrated harm vs off (rollback requires p<0.05 below off).")
    lines.append("")
    lines.append("## Verdict (pinned decision rule)")
    lines.extend(f"- {v}" for v in verdict)
    if min(n_off, n_pull, n_pd) < 26:
        lines.append(f"- NOTE: under-powered for the superiority branch — judge-scored n/arm = "
                     f"off:{n_off} push-distilled:{n_pd} pull:{n_pull} (rule wants >=26).")
    return "\n".join(lines)


def do_score_rubric(root: Path, *, model: str | None, passes: int,
                    scorer: Any | None = None, dry_run: bool = False) -> int:
    """Score this run's produced plans via the blind rubric judge, then write+print the
    rubric report + verdict. Spawns real ``claude -p`` judge turns unless ``scorer`` is
    injected (tests). Best-effort: never crashes the orchestration."""
    if dry_run:
        _log("DRY --score-rubric: would load this run's memory_eval records, score each "
             "plan blind via the rubric judge (RUBRIC_JUDGE_MODEL), and print the verdict.")
        return 0
    try:
        from _telemetry.memory_eval import load_memory_evals

        manifest = load_manifest(root)
        records = load_memory_evals(Path(root))
        scored = attach_rubric_scores(
            records, runtime_dir(root) / "specs",
            model=model, passes=passes, scorer=scorer,
        )
        md = render_rubric_verdict(manifest, scored)
        out_path = runtime_dir(root) / "telemetry" / "reports" / "ab-rubric-report.md"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(md, encoding="utf-8")
        _log(f"wrote rubric report: {out_path}")
        print(md, flush=True)
        return 0
    except Exception as exc:  # noqa: BLE001 - rubric scoring is best-effort
        _log(f"rubric scoring FAILED (ignored): {exc}")
        return 0


def do_cleanup(
    config_path: str,
    root: Path,
    spec_ids: Sequence[str],
    dry_run: bool,
    *,
    temp_config: str | None = None,
    isolated_queue: str | None = None,
) -> int:
    """Cancel any queued work for the throwaway specs and archive their spec dirs,
    then remove the temp config + isolated queue. Idempotent: missing specs /
    already-archived dirs / absent temp artifacts are skipped quietly."""
    archive_dir = runtime_dir(root) / "specs" / "archive"
    specs_dir = runtime_dir(root) / "specs"
    cleaned = 0
    for spec_id in spec_ids:
        spec_dir = specs_dir / spec_id
        if dry_run:
            _log(f"DRY cleanup: would archive {spec_dir} -> {archive_dir / spec_id}")
            continue
        if not spec_dir.exists():
            _log(f"cleanup: {spec_id} not present (already removed?)")
            continue
        archive_dir.mkdir(parents=True, exist_ok=True)
        dest = archive_dir / spec_id
        if dest.exists():
            dest = archive_dir / f"{spec_id}-{datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%d%H%M%S')}"
        shutil.move(str(spec_dir), str(dest))
        _log(f"cleanup: archived {spec_id} -> {dest}")
        cleaned += 1

    # Tear down the isolated queue + temp config (never the live ones).
    if isolated_queue:
        q = Path(isolated_queue)
        if dry_run:
            _log(f"DRY cleanup: would remove isolated queue {q}")
        elif q.exists():
            shutil.rmtree(q, ignore_errors=True)
            _log(f"cleanup: removed isolated queue {q}")
    if temp_config:
        c = Path(temp_config)
        if dry_run:
            _log(f"DRY cleanup: would remove temp config {c}")
        elif c.exists():
            c.unlink()
            _log(f"cleanup: removed temp config {c}")

    if not dry_run:
        # Reset the manifest so a later --run does not target archived specs.
        save_manifest(root, [], None, arm_specs={}, temp_config=None, isolated_queue=None)
    return cleaned


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ab-memory-gain",
        description="Controlled off-vs-hivemind A/B runner for the memory-gain experiment.",
    )
    p.add_argument("--config", default=None,
                   help="SOURCE dispatch config path (its parent.parent is the project root). "
                        "A temp copy with plan_gate:true + an isolated queue is derived from it; "
                        "the live config is never modified.")
    p.add_argument("--root", default=None,
                   help="project root for telemetry/specs (default: derived from --config)")
    p.add_argument("--lane", default="claude", help="lane for plan phases (default: claude)")

    p.add_argument("--draft", type=int, metavar="N", default=None,
                   help="synthesize N plan-ready throwaway benchmark specs PER ARM (unpaired) and record a manifest")
    p.add_argument("--specs", default=None,
                   help="comma-separated existing spec ids to use instead of the manifest (applied to every arm)")
    p.add_argument("--seed", action="store_true",
                   help="write related decision/learned memories into hivemind before the hivemind arm")
    p.add_argument("--run", action="store_true",
                   help="run each arm's plan phases (unpaired) for the benchmark specs")
    p.add_argument("--arms", default=",".join(DEFAULT_ARMS),
                   help="comma-separated arms to run, in order (default: "
                        f"{','.join(DEFAULT_ARMS)}). Recognized arms: "
                        f"{', '.join(KNOWN_ARMS)}. off=strip HIVEMIND_*; "
                        "hivemind=set HIVEMIND_* (legacy, no other knobs); "
                        "push-raw=HIVEMIND_*+MEMORY_RECALL_MODE=push; "
                        "push-distilled=push-raw+MEMORY_DISTILL_MODEL+PRIOR_ART_CHAR_BUDGET+PRIOR_ART_REL_GATE; "
                        "pull=HIVEMIND_*+MEMORY_RECALL_MODE=pull")
    p.add_argument("--report", action="store_true",
                   help="after the arms, render the off-vs-hivemind gain report")
    p.add_argument("--score-rubric", action="store_true",
                   help="score this run's produced plans via the blind rubric judge "
                        "(RUBRIC_JUDGE_MODEL; spawns real claude -p judge turns) and print "
                        "the per-arm rubric report + the pinned promotion/rollback verdict")
    p.add_argument("--rubric-passes", type=int, default=2,
                   help="k rubric scoring passes per plan, averaged (default 2)")
    p.add_argument("--telegram", action="store_true",
                   help="with --report, POST the report to Telegram")
    p.add_argument("--cleanup", action="store_true",
                   help="archive the throwaway specs + remove the temp config and isolated queue")
    p.add_argument("--dry-run", action="store_true",
                   help="print exactly what WOULD run; execute nothing (SAFE smoke path)")
    return p


def _resolve_root(args: argparse.Namespace) -> Path:
    if args.root:
        return Path(args.root).resolve()
    config = Path(args.config) if args.config else runtime_dir(Path.cwd()) / "dispatch.yaml"
    return config.resolve().parent.parent


def _resolve_arms(raw: str) -> list[str]:
    arms = [a.strip() for a in raw.split(",") if a.strip()]
    for arm in arms:
        if arm not in KNOWN_ARMS:
            raise SystemExit(
                f"error: unknown arm {arm!r} (expected one of {', '.join(KNOWN_ARMS)})"
            )
    return arms or list(DEFAULT_ARMS)


def run(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = _resolve_root(args)
    source_config = Path(args.config) if args.config else runtime_dir(root) / "dispatch.yaml"
    arms = _resolve_arms(args.arms)
    base_env = dict(os.environ)
    live_hivemind = {v: base_env[v] for v in HIVEMIND_ENV_VARS if v in base_env}

    # No action flag => show the dry-run plan as a help-ish default.
    no_action = not (args.draft is not None or args.seed or args.run or args.report or args.score_rubric or args.cleanup)

    # A stable run id ties the temp config + isolated queue + drafted specs together.
    manifest = load_manifest(root)
    run_id = str(manifest.get("stamp") or new_run_id())

    # --- draft / resolve the per-arm benchmark spec sets (unpaired) ------------
    if args.draft is not None:
        run_id = new_run_id()
        arm_specs = do_draft(root, args.draft, arms, args.dry_run, stamp=run_id)
    else:
        arm_specs = resolve_arm_specs(root, args.specs, arms)
    all_spec_ids = sorted({s for arm in arms for s in arm_specs.get(arm, [])})

    # --- temp config (plan_gate:true + isolated queue) -------------------------
    # In dry-run we still resolve the paths so the plan can SHOW them, but write
    # nothing. In the live path we materialize the temp config + isolated queue.
    temp_cfg_str = str(temp_config_path(root, run_id))
    isolated_q_str = str(isolated_queue_path(root, run_id))
    config_for_commands = temp_cfg_str

    if args.dry_run or no_action:
        plan = build_dry_run_plan(
            config_path=config_for_commands,
            root=str(root),
            arm_specs=arm_specs,
            arms=arms,
            base_env=base_env,
            live_hivemind=live_hivemind,
            do_seed=args.seed,
            do_report=args.report or no_action,
            telegram=args.telegram,
            temp_config=temp_cfg_str,
            isolated_queue=isolated_q_str,
            plan_gate=True,
        )
        print(render_dry_run(plan))
        if no_action and not args.dry_run:
            _log("no action flag given; printed the dry-run plan. "
                 "Pass --draft/--seed/--run/--report/--cleanup to act, "
                 "or --dry-run to preview an action set.")
        if args.run:
            # Surface the live --run mechanics in dry-run: per-arm seed clear/re-seed
            # (when --seed) and the bounded run --once drain loop. Side-effect-free.
            _log("DRY --run detail (per-arm seed isolation + drain-to-empty loop):")
            do_run_arms(config_for_commands, root, arm_specs, arms, dry_run=True,
                        seed=args.seed, isolated_queue=isolated_q_str)
        if args.report:
            do_report(root, args.telegram, dry_run=True)
        if args.score_rubric:
            do_score_rubric(root, model=None, passes=args.rubric_passes, dry_run=True)
        if args.cleanup:
            do_cleanup(config_for_commands, root, all_spec_ids, dry_run=True,
                       temp_config=temp_cfg_str, isolated_queue=isolated_q_str)
        return 0

    # --- live path -------------------------------------------------------------
    # Materialize the temp config + isolated queue (plan_gate:true) BEFORE any
    # draft/enqueue/run touches the queue, and record everything in the manifest.
    if args.draft is not None or args.run:
        cfg_path, queue_dir = build_temp_config(source_config, root, run_id)
        config_for_commands = str(cfg_path)
        temp_cfg_str = str(cfg_path)
        isolated_q_str = str(queue_dir)
        _log(f"temp config: {cfg_path} (plan_gate:true, isolated queue: {queue_dir})")

    if args.draft is not None:
        save_manifest(root, all_spec_ids, run_id, arm_specs=arm_specs,
                      temp_config=temp_cfg_str, isolated_queue=isolated_q_str)
        _log(f"recorded manifest: {_manifest_path(root)} "
             f"({sum(len(v) for v in arm_specs.values())} specs across {len(arms)} arms)")

    # With --run, seeding is driven PER ARM inside do_run_arms (clear + re-seed under
    # each arm's env, so distillation-at-write is measured). A standalone --seed (no
    # --run) still writes the single raw corpus via do_seed for ad-hoc use.
    if args.seed and not args.run:
        do_seed(root, dry_run=False)

    if args.run:
        if not all_spec_ids:
            _log("error: no benchmark specs to run (use --draft N or --specs id1,id2,...)")
            return 1
        for arm in arms:
            if len(arm_specs.get(arm, [])) < 2:
                _log(f"WARNING: arm {arm} has fewer than 2 specs — the report's Cohen's d / "
                     "p-value will be null. Draft >= 2 comparable specs per arm for a valid A/B.")
        do_run_arms(config_for_commands, root, arm_specs, arms, dry_run=False,
                    seed=args.seed, isolated_queue=isolated_q_str)

    if args.score_rubric:
        do_score_rubric(root, model=os.environ.get("RUBRIC_JUDGE_MODEL"),
                        passes=args.rubric_passes)

    if args.report:
        rc = do_report(root, args.telegram, dry_run=False)
        if rc != 0:
            return rc

    if args.cleanup:
        # Prefer the manifest-recorded isolation paths so cleanup works even in a
        # fresh process that did not (re)build the temp config.
        m = load_manifest(root)
        do_cleanup(
            config_for_commands, root, all_spec_ids, dry_run=False,
            temp_config=m.get("temp_config") or temp_cfg_str,
            isolated_queue=m.get("isolated_queue") or isolated_q_str,
        )

    return 0


def main() -> int:
    return run(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
