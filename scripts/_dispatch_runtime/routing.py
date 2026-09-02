"""Deterministic lane routing helpers."""

from __future__ import annotations

from dataclasses import dataclass

from _dispatch_runtime.config import DispatchConfig
from _dispatch_runtime.queue_store import WorkItem


class UnknownLaneHintError(ValueError):
    """Raised when a work item names a lane that is not configured."""


@dataclass(frozen=True)
class RoutingDecision:
    lane_name: str | None


def resolve_lane(
    item: WorkItem,
    config: DispatchConfig,
    eligible_lanes: list[str],
) -> RoutingDecision:
    if item.lane:
        if item.lane not in config.lanes:
            raise UnknownLaneHintError(f"unknown lane hint: {item.lane}")
        if item.lane in eligible_lanes:
            return RoutingDecision(lane_name=item.lane)
        return RoutingDecision(lane_name=None)

    ordered_lanes = list(config.lanes.keys())
    for lane_name in ordered_lanes:
        if lane_name in eligible_lanes:
            return RoutingDecision(lane_name=lane_name)
    return RoutingDecision(lane_name=None)
