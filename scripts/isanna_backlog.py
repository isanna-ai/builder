#!/usr/bin/env python3
"""Backlog tending -- read-only views and owner-curated rank over the intent backlog.

No external board: the intent backlog (`.builder/intents/<id>/intent.yaml`) is the only home, and
this module never edits that closed schema. It reuses `planning.intent_inventory` for visible
state, `planning.active_backlog_capability_index` for the collision lint, and
`_intent_model.atomic_write_bytes` for the one file it does own: the owner-curated rank sidecar at
`.builder/intents/backlog-rank.yaml`. Promote/retire add no mutation path of their own -- they
delegate to `isanna.cmd_intent`, the existing human-only, controlling-TTY-confirmed transition.
"""

from __future__ import annotations

import importlib.util
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from _yaml import yaml
from _intent_model import atomic_write_bytes, load_intent_object
from _dispatch_runtime.paths import runtime_dir

SCRIPTS = Path(__file__).resolve().parent

DAY_SECONDS = 86400


class BacklogError(Exception):
    """The intent inventory itself could not be read; do not present a partial view as authoritative."""


class BacklogRefusal(Exception):
    """A tending request was refused before any write occurred."""


def _load(script: str, name: str) -> Any:
    """Import a sibling script the same way scripts/isanna.py does, so both share one contract."""
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / script)
    if spec is None or spec.loader is None:
        raise BacklogError(f"cannot load {script}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _planning() -> Any:
    return _load("planning.py", "isanna_backlog_planning")


def _intent_path(root: Path, intent_id: str) -> Path:
    return runtime_dir(root) / "intents" / intent_id / "intent.yaml"


def _rank_path(root: Path) -> Path:
    return runtime_dir(root) / "intents" / "backlog-rank.yaml"


def _diagnostic_detail(diagnostic: Any) -> str:
    return diagnostic.findings[0] if diagnostic.findings else diagnostic.path


@dataclass(frozen=True)
class BacklogRow:
    intent_id: str
    title: str
    visible_state: str
    rank: int


def load_rank(root: Path) -> list[str]:
    path = _rank_path(Path(root))
    if not path.is_file():
        return []
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise BacklogError(f"{path}: unreadable backlog-rank sidecar ({exc})") from exc
    if not isinstance(data, dict) or data.get("artifact") != "backlog-rank":
        raise BacklogError(f"{path}: artifact must be 'backlog-rank'")
    order = data.get("order")
    if (
        not isinstance(order, list)
        or any(not isinstance(item, str) or not item for item in order)
        or len(set(order)) != len(order)
    ):
        raise BacklogError(f"{path}: backlog-rank order must be a list of unique non-empty intent ids")
    return list(order)


def save_rank(root: Path, order: list[str]) -> None:
    path = _rank_path(Path(root))
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = yaml.safe_dump(
        {"artifact": "backlog-rank", "order": list(order)}, sort_keys=False
    ).encode("utf-8")
    try:
        atomic_write_bytes(path, payload)
    except OSError as exc:
        raise BacklogError(f"{path}: cannot write backlog-rank sidecar ({exc})") from exc


def _rank_positions(intent_ids: list[str], order: list[str]) -> dict[str, int]:
    position_in_order = {intent_id: index for index, intent_id in enumerate(order)}
    ranked = sorted(
        intent_ids,
        key=lambda intent_id: (position_in_order.get(intent_id, len(order)), intent_id),
    )
    return {intent_id: rank for rank, intent_id in enumerate(ranked, start=1)}


def visible_backlog(root: Path) -> list[BacklogRow]:
    root = Path(root)
    planning = _planning()
    visible, diagnostics = planning.intent_inventory(root)
    if diagnostics:
        raise BacklogError(_diagnostic_detail(diagnostics[0]))
    items = [
        (item.intent.intent, item.intent.title, item.visible_state)
        for item in visible
        if item.intent is not None
    ]
    order = load_rank(root)
    ranks = _rank_positions([item[0] for item in items], order)
    rows = [
        BacklogRow(intent_id=intent_id, title=title, visible_state=visible_state, rank=ranks[intent_id])
        for intent_id, title, visible_state in items
    ]
    rows.sort(key=lambda row: row.rank)
    return rows


def rank_intent(root: Path, intent_id: str, position: int) -> list[str]:
    root = Path(root)
    rows = visible_backlog(root)
    known_ids = {row.intent_id for row in rows}
    if intent_id not in known_ids:
        raise BacklogRefusal(f"unknown intent id: {intent_id}")
    full_order = [row.intent_id for row in sorted(rows, key=lambda row: row.rank)]
    if not (1 <= position <= len(full_order)):
        raise BacklogRefusal(f"position out of range: {position} (backlog has {len(full_order)} intents)")
    full_order.remove(intent_id)
    full_order.insert(position - 1, intent_id)
    save_rank(root, full_order)
    return full_order


def promotion_collisions(root: Path, intent_id: str) -> list[str]:
    root = Path(root)
    planning = _planning()
    path = _intent_path(root, intent_id)
    intent_obj = load_intent_object(path, root, planning.parse_spec_ref)
    targets = {delta.target for delta in intent_obj.ssot_delta["capabilities"]}
    if not targets:
        return []
    index, diagnostics = planning.active_backlog_capability_index(root)
    if diagnostics:
        raise BacklogError(diagnostics[0])
    colliding: set[str] = set()
    for target in targets:
        owners = index.get(target)
        if owners is None:
            continue
        colliding.update(other_id for other_id in owners.collision_intent_ids if other_id != intent_id)
    return sorted(colliding)


def _tending_commands(root: Path, intent_id: str) -> tuple[str, ...]:
    root_arg = shlex.quote(str(root))
    intent_arg = shlex.quote(intent_id)
    return (
        f"isanna backlog promote --root {root_arg} --id {intent_arg}",
        f"isanna backlog rank --root {root_arg} --id {intent_arg} --position 1",
        f"isanna backlog retire --root {root_arg} --id {intent_arg} --reason {shlex.quote('<reason>')}",
    )


@dataclass(frozen=True)
class GardenReviewRow:
    intent_id: str
    title: str
    rank: int
    age_days: int
    collisions: tuple[str, ...]
    commands: tuple[str, ...]


def stale_proposed(root: Path, stale_days: int, now_ts: float) -> list[str]:
    root = Path(root)
    planning = _planning()
    visible, diagnostics = planning.intent_inventory(root)
    if diagnostics:
        raise BacklogError(_diagnostic_detail(diagnostics[0]))
    threshold = stale_days * DAY_SECONDS
    stale: list[str] = []
    for item in visible:
        if item.intent is None or item.visible_state != "proposed":
            continue
        path = root / item.path
        try:
            mtime = path.stat().st_mtime
        except OSError as exc:
            raise BacklogError(f"{path}: cannot inspect intent staleness ({exc})") from exc
        if now_ts - mtime >= threshold:
            stale.append(item.intent.intent)
    return sorted(stale)


def garden_review(root: Path, stale_days: int, now_ts: float) -> list[GardenReviewRow]:
    root = Path(root)
    stale_ids = stale_proposed(root, stale_days, now_ts)
    if not stale_ids:
        return []
    rows_by_id = {row.intent_id: row for row in visible_backlog(root)}
    report: list[GardenReviewRow] = []
    for intent_id in stale_ids:
        row = rows_by_id.get(intent_id)
        path = _intent_path(root, intent_id)
        age_days = int((now_ts - path.stat().st_mtime) // DAY_SECONDS)
        collisions = promotion_collisions(root, intent_id)
        report.append(
            GardenReviewRow(
                intent_id=intent_id,
                title=row.title if row is not None else intent_id,
                rank=row.rank if row is not None else 0,
                age_days=age_days,
                collisions=tuple(collisions),
                commands=_tending_commands(root, intent_id),
            )
        )
    return report
