"""Work-item state model for dispatch queue records."""

from __future__ import annotations

from enum import StrEnum


class WorkItemState(StrEnum):
    QUEUED = "queued"
    DISPATCHED = "dispatched"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    BLOCKED_HUMAN = "blocked_human"
    # R4: a QUEUED item whose spec declares an unmet `required` dependency (see
    # dependency_gating). NON-terminal — it auto-recovers to QUEUED once every
    # dependency verifies, or cascades to BLOCKED_HUMAN if a dependency's own
    # pipeline permanently stalls (never waits forever on a dep that will not verify).
    BLOCKED_DEP = "blocked_dep"
    # M5: an operator-paused QUEUED item. NON-terminal and NOT dispatchable
    # (_dispatchable_items only reserves QUEUED), so the scheduler skips it until an
    # operator `continue`s it back to QUEUED. Distinct from lane-level pause (halts a
    # whole provider lane) and `drain` (halts all admission) — this halts ONE work item.
    PAUSED = "paused"
    CANCELLED = "cancelled"


TERMINAL_STATES = {
    WorkItemState.SUCCEEDED,
    WorkItemState.FAILED,
    WorkItemState.BLOCKED_HUMAN,
    WorkItemState.CANCELLED,
}


LEGAL_TRANSITIONS: dict[WorkItemState, set[WorkItemState]] = {
    WorkItemState.QUEUED: {
        WorkItemState.DISPATCHED,
        WorkItemState.FAILED,
        WorkItemState.CANCELLED,
        WorkItemState.BLOCKED_DEP,
        WorkItemState.PAUSED,
    },
    WorkItemState.DISPATCHED: {
        WorkItemState.RUNNING,
        WorkItemState.QUEUED,
        WorkItemState.FAILED,
        WorkItemState.CANCELLED,
    },
    WorkItemState.RUNNING: {
        WorkItemState.SUCCEEDED,
        WorkItemState.FAILED,
        WorkItemState.BLOCKED_HUMAN,
        WorkItemState.QUEUED,
        WorkItemState.CANCELLED,
    },
    WorkItemState.SUCCEEDED: set(),
    WorkItemState.FAILED: set(),
    WorkItemState.BLOCKED_HUMAN: set(),
    # R4: auto-recover once deps verify, or cascade to BLOCKED_HUMAN once a
    # dependency's pipeline permanently stalls. M3: also CANCELLED, so the
    # `cancel` CLI can cancel a dependency-held item instead of raising
    # InvalidTransitionError as an operator traceback.
    WorkItemState.BLOCKED_DEP: {
        WorkItemState.QUEUED,
        WorkItemState.BLOCKED_HUMAN,
        WorkItemState.CANCELLED,
    },
    # M5: a paused item `continue`s back to QUEUED, or can be CANCELLED while paused.
    WorkItemState.PAUSED: {
        WorkItemState.QUEUED,
        WorkItemState.CANCELLED,
    },
    WorkItemState.CANCELLED: set(),
}


class InvalidTransitionError(ValueError):
    """Raised when a work-item transition is outside the legal model."""


def normalize_state(value: WorkItemState | str) -> WorkItemState:
    if isinstance(value, WorkItemState):
        return value
    try:
        return WorkItemState(str(value))
    except ValueError as exc:
        raise InvalidTransitionError(f"unknown work item state: {value}") from exc


def transition(current: WorkItemState | str, requested: WorkItemState | str) -> WorkItemState:
    current_state = normalize_state(current)
    requested_state = normalize_state(requested)
    if requested_state not in LEGAL_TRANSITIONS[current_state]:
        raise InvalidTransitionError(
            f"invalid work item transition: {current_state.value} -> {requested_state.value}"
        )
    return requested_state
