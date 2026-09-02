"""Builder run-ledger write side.

Turns each finalized Builder phase into a structured row in two hivemind
dynamic-data tables, so operator run-history questions ("which specs took >2h",
"which finance tests regressed this week") can be answered with `hive_query`
instead of grepping the dispatcher log.

Two entry points, both env-gated and swallow-on-error (the ledger is operator
telemetry — it MUST NEVER break the dispatch loop, mirroring the
`# noqa: BLE001` contract in `_emit_finalize_memory_eval`):

  * ``ensure_ledger_tables(client)`` — idempotent `hive_create_table` ×2 for the
    `spec_runs` and `verify_runs` tables. `hive_create_table` short-circuits on an
    existing table and returns ``{success, table, idempotent: true}``
    on the server, so this is safe to call every turn.
  * ``write_run_ledger(work, exec_result, lane_name, decision, ...)`` — ensure the
    tables, then `hive_insert` one `spec_runs` row for the finalized phase (and one
    `verify_runs` row per learned note on a verify turn). Returns ``True`` iff a
    `spec_runs` row was inserted; ``False`` on the env-off no-op or on any error.

Reuses ``memory_hook._HiveClient`` / ``_hive_client()`` (no new client) and
``phase_runtime.normalize_phase``. The `hive_create_table` ``{table, columns}`` and
`hive_insert` ``{table, data}`` arg shapes are constructed in code, so the write
side has NO dependency on S6 schema GENERATION (it depends only on the dispatch
shape `callHiveTool` unpacks).

Columns: NO `id`/`org_id` — the server injects both automatically; the chosen
names are all valid PG identifiers (not PostgreSQL reserved words). `org_id` isolation is enforced
server-side from the validated API key, never from tool input (R6).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from _dispatch_runtime.memory_hook import _HiveClient, _hive_client  # reuse; no new client
from _dispatch_runtime.phase_runtime import normalize_phase

__all__ = ["ensure_ledger_tables", "write_run_ledger"]

# No id/org_id column — the server injects both.
# plan_tokens and wall_ms are `integer`, NOT `bigint`: the live hivemind server
# cannot serialize a `bigint` column value back through `/mcp/message` —
# the pg driver returns a JS BigInt and the JSON response serializer
# throws `TypeError: Do not know how to serialize a BigInt`, returning HTTP 500 on
# any insert/query that round-trips the column. `integer` (int32, ~2.1B ceiling) is
# serialization-safe and far above any realistic per-phase token count (< ~1M) or
# per-turn wall-clock ms (2.1e9 ms ≈ 24 days), so the int32 width is not a practical
# constraint for a per-phase ledger row.
_SPEC_RUNS_COLUMNS: list[dict[str, str]] = [
    {"name": "spec_id", "type": "text"},
    {"name": "phase", "type": "text"},
    {"name": "lane", "type": "text"},
    {"name": "plan_tokens", "type": "integer"},
    {"name": "wall_ms", "type": "integer"},
    {"name": "outcome", "type": "text"},
    {"name": "ts", "type": "timestamp"},
    # Tier-1 forward-transfer clock (live RCT substrate): the spec's plan-time
    # recall exposure, recorded on EVERY phase row so memory_mode is a queryable
    # per-run attribute. It MUST NOT gate the write — the OFF arm has to be logged
    # or the natural experiment has no control. memory_mode in
    # {"on","off","unknown"}; recall_hits = count of prior-art memories that fired.
    {"name": "memory_mode", "type": "text"},
    {"name": "recall_hits", "type": "integer"},
]
_VERIFY_RUNS_COLUMNS: list[dict[str, str]] = [
    {"name": "spec_id", "type": "text"},
    {"name": "module", "type": "text"},
    {"name": "test", "type": "text"},
    {"name": "root_cause", "type": "text"},
    {"name": "ts", "type": "timestamp"},
]

# Map PostTurnDecision.outcome (phase_runtime.PostTurnDecision) -> ledger label.
_OUTCOME_LABELS: dict[str, str] = {
    "phase-complete": "complete",
    "blocked-human": "blocked",
    "stale-escalate": "stale",
    "rate-limit-cooldown": "rate-limited",
    "resume-same-session": "resumed",
    "retry-fresh-session": "retried",
}


def ensure_ledger_tables(client: Any) -> None:
    """Idempotently ensure the two ledger tables exist.

    Issues two `hive_create_table` calls with the documented columns. The server
    short-circuits on an existing table and returns ``idempotent: true``, so this
    is safe to call on every finalized turn. May raise (caller swallows)."""
    client.call("bia_create_table", {"table": "spec_runs", "columns": _SPEC_RUNS_COLUMNS})
    client.call("bia_create_table", {"table": "verify_runs", "columns": _VERIFY_RUNS_COLUMNS})


def _outcome(decision_outcome: str) -> str:
    """Map a PostTurnDecision.outcome to a short ledger label; passthrough for an
    unrecognized non-empty outcome; ``"unknown"`` for an empty one."""
    return _OUTCOME_LABELS.get(decision_outcome) or decision_outcome or "unknown"


def _test_token(note: Any) -> str:
    """Extract a short test/module token from a learned note: the leading
    ``"<token>: <reason>"`` segment when a ``": "`` is present, else the whole
    note — truncated to 100 chars."""
    s = str(note)
    return (s.split(": ", 1)[0] if ": " in s else s)[:100]


def _norm_memory_mode(value: Any) -> str:
    """Normalize a memory_mode to one of ``{"on","off","unknown"}`` for the
    Tier-1 ledger. Anything not recognizably on/off becomes ``"unknown"`` so the
    column is always a clean three-valued factor for the forward-transfer split."""
    s = str(value or "").strip().lower()
    # "hivemind" is the dispatcher's active-arm label (_memory_mode_for_dispatcher);
    # it means recall is routed to the live store == memory ON for the clock.
    if s in ("on", "hivemind", "true", "1", "relevant", "on-relevant"):
        return "on"
    if s in ("off", "false", "0", "baseline"):
        return "off"
    return "unknown"


def write_run_ledger(
    work: Any,
    exec_result: dict[str, Any],
    lane_name: str | None,
    decision: Any,
    *,
    client: Any | None = None,
    decisions_learned: list[Any] | None = None,
    memory_mode: Any = None,
    recall_hits: Any = None,
) -> bool:
    """Ensure the ledger tables and insert one `spec_runs` row for the finalized
    phase (plus one `verify_runs` row per learned note when ``work.phase`` is
    ``verify``).

    Env-gated: when ``client`` is ``None`` it is resolved from ``_hive_client()``;
    if that is also ``None`` (no `HIVEMIND_MCP_URL`/`HIVEMIND_API_KEY`) the call is
    a no-op returning ``False`` (the ``memory_mode="off"`` baseline arm stays
    zero-dependency). Never raises: any network/auth/schema error is swallowed and
    the call returns ``False`` so the dispatch loop outcome is unchanged.

    Returns ``True`` iff a `spec_runs` row was inserted."""
    if client is None:
        client = _hive_client()  # None => env gate off
    if client is None:
        return False

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    lane = "codex" if "codex" in str(lane_name or "").lower() else "claude"
    phase = normalize_phase(str(getattr(work, "phase", ""))) or str(getattr(work, "phase", ""))

    try:
        ensure_ledger_tables(client)
        client.call(
            "bia_insert",
            {
                "table": "spec_runs",
                "data": {
                    "spec_id": work.spec_id,
                    "phase": phase,
                    "lane": lane,
                    "plan_tokens": int(exec_result.get("input_tokens") or 0)
                    + int(exec_result.get("output_tokens") or 0),
                    "wall_ms": int(exec_result.get("cli_duration_ms") or 0),
                    "outcome": _outcome(str(getattr(decision, "outcome", "") or "")),
                    "ts": ts,
                    # Tier-1: recorded on every phase row; NEVER gates the write.
                    "memory_mode": _norm_memory_mode(memory_mode),
                    "recall_hits": int(recall_hits or 0),
                },
            },
        )
        if phase == "verify" and decisions_learned:
            for note in decisions_learned:  # learned notes == verify failures
                client.call(
                    "bia_insert",
                    {
                        "table": "verify_runs",
                        "data": {
                            "spec_id": work.spec_id,
                            "module": work.spec_id,
                            "test": _test_token(note),
                            "root_cause": str(note)[:2000],
                            "ts": ts,
                        },
                    },
                )
        return True
    except Exception:  # noqa: BLE001 - ledger must never break the lane
        return False
