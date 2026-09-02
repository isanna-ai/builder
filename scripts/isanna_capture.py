"""Capture core -- turn a crystallized discussion into a `proposed` intent, non-interactively.

`isanna capture` (and the /idea skill that wraps it) exist so a distilled why + success criteria
never depend on the capturing conversation staying open. Capture PROPOSES ONLY: it reuses the
existing intent-object contract (`_intent_model.validate_intent_payload`, `atomic_write_bytes`) so
every field it writes is exactly as strict as the human-only accept/reject/supersede path, and it
never touches status beyond `proposed`.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from _intent_model import atomic_write_bytes, validate_intent_payload
from _dispatch_runtime.paths import runtime_dir
from _yaml import yaml


def _non_empty(value: Any, context: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{context} must be a non-empty string")
    return value.strip()


def _safe_intent_id(intent_id: Any) -> str:
    if (
        not isinstance(intent_id, str)
        or not intent_id.strip()
        or intent_id in {".", ".."}
        or "/" in intent_id
        or "\\" in intent_id
    ):
        raise ValueError(f"unsafe intent id {intent_id!r}")
    return intent_id.strip()


def build_captured_intent(
    intent_id: str,
    title: str,
    problem: str,
    why: str,
    success_criteria: list[str] | tuple[str, ...],
    non_goals: list[str] | tuple[str, ...] = (),
    ssot_delta: dict[str, list[dict[str, str]]] | None = None,
    specs: list[str] | tuple[str, ...] = (),
) -> bytes:
    """Assemble a canonical `intent-object` payload with status `proposed`. Raises ValueError
    before any bytes are produced if a required field is missing, empty, or the id is unsafe."""
    intent_id = _safe_intent_id(intent_id)
    title = _non_empty(title, "title")
    problem = _non_empty(problem, "problem")
    why = _non_empty(why, "why")

    criteria = list(success_criteria)
    if not criteria:
        raise ValueError("success_criteria must be a non-empty list")
    criteria_payload = []
    for index, statement in enumerate(criteria, start=1):
        criteria_payload.append({"id": f"SC-{index}", "statement": _non_empty(statement, f"success_criteria[{index - 1}]")})

    non_goals_payload = [
        _non_empty(item, f"non_goals[{index}]") for index, item in enumerate(non_goals)
    ] or ["not yet triaged for scope"]

    delta_payload = ssot_delta if ssot_delta is not None else {"capabilities": [], "behaviors": [], "journeys": []}

    data = {
        "artifact": "intent-object",
        "intent": intent_id,
        "title": title,
        "status": "proposed",
        "problem": problem,
        "why": why,
        "success_criteria": criteria_payload,
        "non_goals": non_goals_payload,
        "ssot_delta": delta_payload,
        "specs": list(specs),
    }
    return yaml.safe_dump(data, sort_keys=False).encode("utf-8")


def capture_intent(
    root: Path | str,
    intent_id: str,
    title: str,
    problem: str,
    why: str,
    success_criteria: list[str] | tuple[str, ...],
    non_goals: list[str] | tuple[str, ...] = (),
    ssot_delta: dict[str, list[dict[str, str]]] | None = None,
    specs: list[str] | tuple[str, ...] = (),
) -> Path:
    """Write a validated, `proposed` intent-object at `.builder/intents/<id>/intent.yaml`.

    Refuses -- raising, writing nothing -- when that path already exists, so capture can never
    clobber an intent a human is already tending."""
    root = Path(root).resolve()
    safe_id = _safe_intent_id(intent_id)
    path = runtime_dir(root) / "intents" / safe_id / "intent.yaml"
    if path.exists():
        raise FileExistsError(f"intent already exists: {path}")

    payload = build_captured_intent(
        safe_id,
        title,
        problem,
        why,
        success_criteria,
        non_goals=non_goals,
        ssot_delta=ssot_delta,
        specs=specs,
    )

    import planning

    validate_intent_payload(payload, path, root, planning.parse_spec_ref)

    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.touch()
    atomic_write_bytes(path, payload)
    return path
