"""Lane execution seam and typed dispatch outcomes."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from _dispatch_runtime.paths import RUNTIME_DIR_NAMES

from _yaml import yaml  # type: ignore

from _dispatch_runtime.model_registry import resolve_effort as resolve_effort  # re-exported
from _dispatch_runtime.model_registry import resolve_model as resolve_model  # re-exported


class DispatchResultType(StrEnum):
    SUCCESS = "success"
    RETRYABLE_ERROR = "retryable_error"
    TERMINAL_ERROR = "terminal_error"
    RATE_LIMITED = "rate_limited"
    HUMAN_BLOCK = "human_block"


@dataclass(frozen=True)
class DispatchResult:
    result_type: DispatchResultType
    retry_after: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class LaneExecutor(Protocol):
    def execute(
        self,
        task_ref: dict[str, Any],
        lane_name: str,
        attempt_context: dict[str, Any],
    ) -> DispatchResult: ...


def _workspace_root(attempt_context: dict[str, Any]) -> Path:
    raw = attempt_context.get("workspace_root")
    return Path(raw) if raw else Path.cwd()


def _resolve_builder_runner_task_ref(task_ref: dict[str, Any], attempt_context: dict[str, Any]) -> str:
    raw_ref = task_ref.get("runner_task_ref")
    if not isinstance(raw_ref, str) or not raw_ref.strip():
        raise ValueError("builder-runner-task requires task_ref.runner_task_ref")

    candidate = Path(raw_ref.strip())
    if not candidate.is_absolute():
        candidate = _workspace_root(attempt_context) / candidate
    candidate = candidate.resolve()
    if not candidate.exists():
        raise ValueError(f"runner_task_ref does not exist: {candidate}")
    if candidate.parent.name != "runs" or not candidate.name.startswith("task-") or candidate.suffix not in {".yaml", ".yml"}:
        raise ValueError(f"runner_task_ref must point at a runs/task-<id>.yaml artifact: {candidate}")
    if not any(name in candidate.parts for name in RUNTIME_DIR_NAMES) or "specs" not in candidate.parts:
        raise ValueError(f"runner_task_ref must stay within .builder/specs/*/runs: {candidate}")
    return str(candidate)


def _resolve_builder_phase_batch_ref(task_ref: dict[str, Any], attempt_context: dict[str, Any]) -> str:
    raw_ref = task_ref.get("runner_task_ref")
    if not isinstance(raw_ref, str) or not raw_ref.strip():
        raise ValueError("builder-phase-batch requires task_ref.runner_task_ref")

    candidate = Path(raw_ref.strip())
    if not candidate.is_absolute():
        candidate = _workspace_root(attempt_context) / candidate
    candidate = candidate.resolve()
    if not candidate.exists():
        raise ValueError(f"runner_task_ref does not exist: {candidate}")
    if candidate.parent.name != "runs" or not candidate.name.startswith("phase-") or candidate.suffix not in {".yaml", ".yml"}:
        raise ValueError(f"runner_task_ref must point at a runs/phase-<id>.yaml artifact: {candidate}")
    if not any(name in candidate.parts for name in RUNTIME_DIR_NAMES) or "specs" not in candidate.parts:
        raise ValueError(f"runner_task_ref must stay within .builder/specs/*/runs: {candidate}")
    return str(candidate)


def _resolve_capability_class(task_ref: dict[str, Any], attempt_context: dict[str, Any]) -> str | None:
    """Read capability_class from a phase-batch artifact; return None for legacy task refs."""
    kind = str(task_ref.get("kind") or "")
    if kind != "builder-phase-batch":
        return None
    raw_ref = task_ref.get("runner_task_ref")
    if not isinstance(raw_ref, str) or not raw_ref.strip():
        return None
    candidate = Path(raw_ref.strip())
    if not candidate.is_absolute():
        candidate = _workspace_root(attempt_context) / candidate
    candidate = candidate.resolve()
    if not candidate.exists():
        return None
    try:
        data = yaml.safe_load(candidate.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            value = data.get("capability_class")
            return str(value) if value else None
    except Exception:
        return None
    return None
