"""The optional presence wire is observable through a stub, never a real presence store.

These tests were once left behind when `lane_presence.py` was reduced to a single upsert: they
still described the previous contract -- create-table twice, a read-before-write query, then two
inserts -- so all three failed against the module they were meant to guard. They now assert the
DOCUMENTED intent of that design (one call, no table creation, upsert-shaped), rather than
whatever the module happens to do today.
"""

from __future__ import annotations

import os
from types import SimpleNamespace

from _dispatch_runtime import lane_presence


class FakeClient:
    def __init__(self, *, raise_on: str | None = None):
        self.raise_on = raise_on
        self.calls: list[tuple[str, dict]] = []

    def call(self, tool: str, args: dict):
        self.calls.append((tool, args))
        if tool == self.raise_on:
            raise RuntimeError(f"{tool} failed")
        return {"success": True}


def _work():
    return SimpleNamespace(spec_id="demo", phase="verify", project_dir="/path/to/builder")


# --- register_lane ------------------------------------------------------------------------


def test_register_lane_upserts_presence_in_exactly_one_call():
    client = FakeClient()

    assert lane_presence.register_lane(_work(), "codex-cli", client=client) is True

    assert [tool for tool, _ in client.calls] == ["bia_session_upsert"]
    tool, args = client.calls[0]
    assert args == {
        "session_id": "dispatch-codex-demo",
        "front_end": "dispatch-codex",
        "agent": "builder-dispatch",
        "project": "builder",
        "cwd": "/path/to/builder",
        "current_task": "verify demo",
        "status": "active",
        "pid": os.getpid(),
        "event": "start",
        "summary": "dispatcher verify demo",
    }


def test_register_lane_carries_the_start_event_in_the_same_call_as_the_row():
    """The row and its activity event used to be two separate bia_inserts, so a failure
    between them left a presence row with no start event. One call makes that impossible."""
    client = FakeClient()

    assert lane_presence.register_lane(_work(), "claude-code-cli", client=client) is True

    assert len(client.calls) == 1
    args = client.calls[0][1]
    assert args["status"] == "active" and args["event"] == "start"


def test_register_lane_never_creates_a_table():
    """Registering a lane must never create a table.

    This module used to call a create-table tool for both presence tables at the top of every
    lane, as an existence check -- an independent copy of logic the presence store owns. Where
    the store provides the tables itself, that copy can re-create tables the store has
    deliberately dropped, within minutes of the first lane starting.
    """
    client = FakeClient()

    lane_presence.register_lane(_work(), "codex-cli", client=client)
    lane_presence.end_lane(_work(), "codex-cli", client=client)

    assert "bia_create_table" not in [tool for tool, _ in client.calls]


def test_register_lane_does_not_read_before_writing():
    """`session_id` is a LANE NAME, stable across re-runs, so the old query->insert pair
    raced: two lanes starting together both read "absent" and both inserted. An upsert
    against UNIQUE (org_id, session_id) is what makes the duplicate impossible -- a
    reintroduced read would quietly restore the race."""
    client = FakeClient()

    lane_presence.register_lane(_work(), "codex-cli", client=client)

    assert "bia_query" not in [tool for tool, _ in client.calls]


def test_register_lane_returns_false_when_the_client_errors():
    client = FakeClient(raise_on="bia_session_upsert")

    # Presence is awareness-only: it must swallow the failure rather than break the lane.
    assert lane_presence.register_lane(_work(), "claude-code-cli", client=client) is False
    assert [tool for tool, _ in client.calls] == ["bia_session_upsert"]


# --- end_lane -----------------------------------------------------------------------------


def test_end_lane_upserts_the_ended_status_in_exactly_one_call():
    client = FakeClient()

    assert lane_presence.end_lane(_work(), "claude-code-cli", client=client) is True

    assert client.calls == [
        ("bia_session_upsert", {
            "session_id": "dispatch-claude-demo",
            "front_end": "dispatch-claude",
            "agent": "builder-dispatch",
            "status": "ended",
            "event": "end",
        }),
    ]


def test_end_lane_returns_false_when_the_client_errors():
    client = FakeClient(raise_on="bia_session_upsert")

    assert lane_presence.end_lane(_work(), "codex-cli", client=client) is False


# --- the contract both entry points share -------------------------------------------------


def test_presence_is_a_no_op_when_no_client_is_configured():
    """Both entry points are env-gated through `_hive_client()`, which returns None without
    HIVEMIND_MCP_URL + HIVEMIND_API_KEY. Every call site wraps these in try/except and
    ignores the result, so returning False -- rather than raising or reaching the network --
    is the whole safety property. (The pytest shim scrubs HIVEMIND_* from the environment.)"""
    assert lane_presence.register_lane(_work(), "codex-cli") is False
    assert lane_presence.end_lane(_work(), "codex-cli") is False


def test_session_id_is_one_stable_row_per_spec_and_lane():
    """Re-runs upsert the same row back to active instead of accumulating one row per phase,
    and the two lanes never collide on a shared spec."""
    codex, claude = FakeClient(), FakeClient()

    lane_presence.register_lane(_work(), "codex-cli", client=codex)
    lane_presence.register_lane(SimpleNamespace(spec_id="demo", phase="plan", project_dir="/path/to/builder"),
                                "codex-cli", client=codex)
    lane_presence.register_lane(_work(), "claude-code-cli", client=claude)

    codex_ids = {args["session_id"] for _, args in codex.calls}
    assert codex_ids == {"dispatch-codex-demo"}          # stable across phases/re-runs
    assert codex.calls[1][1]["current_task"] == "plan demo"  # the phase still updates
    assert claude.calls[0][1]["session_id"] == "dispatch-claude-demo"
