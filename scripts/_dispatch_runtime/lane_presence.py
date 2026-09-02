"""Builder dispatcher lane-presence write side (cross-session awareness).

Optional. Mirrors the running dispatcher phase into an external ``session_presence``
table, so that an interactive coding session can see what the autonomous dispatcher is
working on right now ("dispatch-claude is running spec X phase verify"). Disabled unless
the environment points it at such a store; nothing in the workflow depends on it.

Two entry points, both env-gated and swallow-on-error (presence is awareness-only —
it MUST NEVER break the dispatch loop, mirroring run_ledger.py's contract):

  * ``register_lane(work, lane_name)`` — upsert an ``active`` presence row for this
    spec+lane (front_end ``dispatch-claude`` / ``dispatch-codex``) and record the
    ``start`` event, in ONE ``bia_session_upsert`` call. Called at the top of each
    lane's ``execute()`` after ``resolve_work``.
  * ``end_lane(work, lane_name)`` — flip the row to ``ended``. Called once in the
    shared ``finalize_turn``, covering BOTH lanes.

Reuses ``memory_hook._hive_client`` (no new client).

This module holds an INDEPENDENT COPY of the presence-writing logic the interactive bridge
uses, which is the thing to know before changing it: the two writers share one table, so a
change here that is not mirrored there (or vice versa) diverges silently and the first
dispatcher lane to run re-creates whatever the other side just dropped. Presence tables are
provisioned by the provider, not created on demand from this side.
"""

from __future__ import annotations

import os
from typing import Any

from _dispatch_runtime.memory_hook import _hive_client  # reuse; no new client

__all__ = ["register_lane", "end_lane"]


def _lane_kind(lane_name: str | None) -> str:
    return "codex" if "codex" in str(lane_name or "").lower() else "claude"


# A container layout that mounts every repo under one parent (`/<parent>/<project>/...`) names
# the project in the segment AFTER that parent, not the last one. This is a convenience for
# that shape only -- any other layout falls through to the final path segment.
_PROJECTS_PARENT = "workspaces"


def _project(work: Any) -> str:
    parts = [p for p in str(getattr(work, "project_dir", "")).split("/") if p]
    if _PROJECTS_PARENT in parts:
        i = parts.index(_PROJECTS_PARENT)
        if i + 1 < len(parts):
            return parts[i + 1]
    return parts[-1] if parts else "unknown"


def _session_id(work: Any, lane: str) -> str:
    # One stable row per spec+lane: re-runs upsert it back to active; phases update
    # current_task. Keeps the table from accumulating one row per phase.
    return f"dispatch-{lane}-{getattr(work, 'spec_id', 'unknown')}"


# This side creates nothing and mints no timestamps. It used to call a create-table tool for
# both presence tables at the top of every lane, as an existence check -- an independent copy
# of logic the presence store already owns. Where the store provides the tables itself, that
# copy is worse than redundant: it can re-create tables the store has deliberately dropped.
# So the schema is the store's to define, and the upsert is server-stamped.


def register_lane(work: Any, lane_name: str | None, *, client: Any | None = None) -> bool:
    """Upsert an ``active`` presence row for this dispatcher phase + log a start
    event, in ONE call. Env-gated (no HIVEMIND_* => no-op False); never raises."""
    if client is None:
        client = _hive_client()
    if client is None:
        return False
    lane = _lane_kind(lane_name)
    sid = _session_id(work, lane)
    front = f"dispatch-{lane}"
    project = _project(work)
    phase = str(getattr(work, "phase", "") or "")
    spec_id = str(getattr(work, "spec_id", "") or "")
    try:
        # Was: create_table x2, then a read-before-write (query -> update|insert), then a second
        # insert for the event — five calls. Now one. The read-before-write also raced: `sid` is a
        # LANE NAME (dispatch-<lane>-<spec_id>), stable across re-runs, so two lanes starting
        # together both read "absent" and both inserted. That is where the duplicate rows in the
        # old table came from; UNIQUE (org_id, session_id) now makes it impossible.
        client.call(
            "bia_session_upsert",
            {
                "session_id": sid,
                "front_end": front,
                "agent": "builder-dispatch",
                "project": project,
                "cwd": str(getattr(work, "project_dir", "")),
                "current_task": f"{phase} {spec_id}".strip(),
                "status": "active",
                "pid": os.getpid(),
                "event": "start",
                "summary": f"dispatcher {phase} {spec_id}".strip(),
            },
        )
        return True
    except Exception:  # noqa: BLE001 - presence must never break the lane
        return False


def end_lane(work: Any, lane_name: str | None, *, client: Any | None = None) -> bool:
    """Flip this dispatcher phase's presence row to ``ended``. Env-gated; never
    raises."""
    if client is None:
        client = _hive_client()
    if client is None:
        return False
    lane = _lane_kind(lane_name)
    sid = _session_id(work, lane)
    try:
        client.call(
            "bia_session_upsert",
            {
                "session_id": sid,
                "front_end": f"dispatch-{lane}",
                "agent": "builder-dispatch",
                "status": "ended",
                "event": "end",
            },
        )
        return True
    except Exception:  # noqa: BLE001 - presence must never break the lane
        return False
