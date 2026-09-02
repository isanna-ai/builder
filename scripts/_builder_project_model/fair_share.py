from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .session_store import SessionStore


@dataclass(frozen=True)
class FairShareCandidate:
    provider: str
    project_id: str
    repo_id: str
    work_id: str
    lane_name: str
    priority: int
    roadmap_index: int | None
    enqueued_at: str


def _provider_state(store: SessionStore, provider: str) -> dict:
    data = store.read_allocation()
    providers = data.setdefault("providers", {})
    state = providers.setdefault(
        provider,
        {"cursor_project_id": None, "launch_count": 0},
    )
    if "schema_version" not in data:
        data["schema_version"] = 1
    return state


def _persist(store: SessionStore, provider: str, state: dict) -> None:
    data = store.read_allocation()
    if "schema_version" not in data:
        data["schema_version"] = 1
    providers = data.setdefault("providers", {})
    providers[provider] = dict(state)
    store.write_allocation(data)


def sort_group(candidates: Iterable[FairShareCandidate]) -> list[FairShareCandidate]:
    return sorted(
        candidates,
        key=lambda row: (
            -row.priority,
            row.roadmap_index is None,
            row.roadmap_index if row.roadmap_index is not None else 10**9,
            row.enqueued_at,
            row.work_id,
        ),
    )


def choose_candidate(*, home_root: Path, provider: str, candidates: list[FairShareCandidate]) -> FairShareCandidate | None:
    if not candidates:
        return None
    store = SessionStore(home_root)
    state = _provider_state(store, provider)
    groups: dict[str, list[FairShareCandidate]] = {}
    for row in candidates:
        groups.setdefault(row.project_id, []).append(row)
    ordered_projects = sorted(groups)
    if not ordered_projects:
        return None
    cursor_project_id = state.get("cursor_project_id")
    start_index = 0
    if cursor_project_id in ordered_projects:
        start_index = (ordered_projects.index(cursor_project_id) + 1) % len(ordered_projects)
    chosen_project = ordered_projects[start_index]
    chosen = sort_group(groups[chosen_project])[0]
    state["cursor_project_id"] = chosen_project
    state["launch_count"] = int(state.get("launch_count", 0)) + 1
    _persist(store, provider, state)
    return chosen
