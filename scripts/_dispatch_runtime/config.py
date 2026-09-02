"""Dispatch control-plane config loading and validation."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from _yaml import yaml  # type: ignore


ENV_REF_PATTERN = re.compile(r"^\$\{([A-Z_][A-Z0-9_]*)\}$")
SECRET_NAME_PATTERN = re.compile(r"(secret|token|key|password|credential)", re.IGNORECASE)


class ConfigError(ValueError):
    """Raised when dispatch config is missing or invalid."""


@dataclass(frozen=True)
class SecretRef:
    env_var: str
    value: str


@dataclass(frozen=True)
class LaneConfig:
    name: str
    provider: str
    max_concurrency: int = 1
    secrets: dict[str, SecretRef] = field(default_factory=dict)


@dataclass(frozen=True)
class DispatchConfig:
    queue_store_path: Path
    lanes: dict[str, LaneConfig]
    routing_policy: dict[str, Any]
    cooldown_policy: dict[str, Any]
    retry_policy: dict[str, Any]
    pipeline: dict[str, Any] = field(default_factory=dict)


def _read_mapping(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ConfigError(f"missing dispatch config: {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ConfigError("dispatch config must be a mapping")
    return data


def _resolve_secret(name: str, value: Any) -> SecretRef:
    if not isinstance(value, str):
        raise ConfigError(f"secret {name} must be an env-var reference")
    match = ENV_REF_PATTERN.fullmatch(value)
    if match is None:
        raise ConfigError(f"inline secret value is not allowed for {name}")
    env_var = match.group(1)
    if env_var not in os.environ:
        raise ConfigError(f"missing env var referenced by dispatch config: {env_var}")
    return SecretRef(env_var=env_var, value=os.environ[env_var])


def _reject_inline_secrets(value: Any, path: str = "") -> None:
    if isinstance(value, dict):
        for key, inner in value.items():
            child_path = f"{path}.{key}" if path else str(key)
            if SECRET_NAME_PATTERN.search(str(key)) and not isinstance(inner, dict):
                if not (isinstance(inner, str) and ENV_REF_PATTERN.fullmatch(inner)):
                    raise ConfigError(f"inline secret value is not allowed for {child_path}")
            _reject_inline_secrets(inner, child_path)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_inline_secrets(item, f"{path}[{index}]")


def _reject_unresolved_env_refs(value: Any, path: str = "") -> None:
    if isinstance(value, dict):
        for key, inner in value.items():
            child_path = f"{path}.{key}" if path else str(key)
            _reject_unresolved_env_refs(inner, child_path)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_unresolved_env_refs(item, f"{path}[{index}]")
    elif isinstance(value, str):
        match = ENV_REF_PATTERN.fullmatch(value)
        if match is not None and match.group(1) not in os.environ:
            raise ConfigError(f"missing env var referenced by dispatch config: {match.group(1)}")


def _load_lanes(raw_lanes: Any) -> dict[str, LaneConfig]:
    if not isinstance(raw_lanes, list) or not raw_lanes:
        raise ConfigError("dispatch config requires at least one lane")

    lanes: dict[str, LaneConfig] = {}
    for index, raw_lane in enumerate(raw_lanes):
        if not isinstance(raw_lane, dict):
            raise ConfigError(f"lanes[{index}] must be a mapping")
        name = str(raw_lane.get("name") or "").strip()
        provider = str(raw_lane.get("provider") or "").strip()
        if not name:
            raise ConfigError(f"lanes[{index}].name is required")
        if not provider:
            raise ConfigError(f"lanes[{index}].provider is required")
        if name in lanes:
            raise ConfigError(f"duplicate lane: {name}")
        max_concurrency = int(raw_lane.get("max_concurrency", 1))
        if max_concurrency < 1:
            raise ConfigError(f"{name}.max_concurrency must be >= 1")
        raw_secrets = raw_lane.get("secrets") or {}
        if not isinstance(raw_secrets, dict):
            raise ConfigError(f"{name}.secrets must be a mapping")
        secrets = {str(key): _resolve_secret(str(key), value) for key, value in raw_secrets.items()}
        lanes[name] = LaneConfig(
            name=name,
            provider=provider,
            max_concurrency=max_concurrency,
            secrets=secrets,
        )
    return lanes


def load_dispatch_config(path: str | Path | None = None) -> DispatchConfig:
    if path is None:
        from _dispatch_runtime.paths import runtime_dir
        path = runtime_dir(Path.cwd()) / "dispatch.yaml"
    config_path = Path(path)
    data = _read_mapping(config_path)
    _reject_inline_secrets(data)
    _reject_unresolved_env_refs(data)

    queue_store = data.get("queue_store")
    if not isinstance(queue_store, dict) or not queue_store.get("path"):
        raise ConfigError("queue_store.path is required")

    cooldown_policy = {
        "default_seconds": 300,
        **dict(data.get("cooldown_policy") or {}),
    }
    retry_policy = {
        "max_attempts": 3,
        "initial_seconds": 30,
        "max_seconds": 900,
        "jitter_seconds": 0,
        # R6: max verify<->implement rework loops (VERIFIED_WITH_TASKS) before a spec
        # is escalated to BLOCKED_HUMAN, so a verifier/implementer ping-pong cannot run
        # all night at Opus-xhigh prices under the notifier's radar. 0 = disabled
        # (default; opt in per repo). Flag-off preserves prior unbounded behavior.
        "rework_max": 0,
        **dict(data.get("retry_policy") or {}),
    }

    # Full automation by DEFAULT: an enqueued spec runs spec->plan->implement->verify
    # straight through to verified / ready-to-archive, no human stop. The plan-approval
    # gate is OPT-IN per spec (spec.yaml `plan_gate: true`, e.g. via `draft --plan-gate`);
    # this pipeline value is only the project-wide fallback when a spec omits its own.
    pipeline = {
        "plan_gate": False,
        "notify": {},
        "deliver": {"enabled": False},  # off until a project opts in (branch protection + prod env)
        "auto_env_up": True,            # run builder-env up before implement/verify so real tests can run
        "default_lane": "claude",       # lock to claude; codex only when explicitly requested (--lane codex)
        # Independent reviews (opt-in): an INDEPENDENT spec review before plan + an
        # INDEPENDENT adversarial review of the implementation, both on the review
        # lane when available, then a claude fix pass before verify. If that lane is
        # unavailable, the goal must not claim model-level independence. This default applies
        # to NEWLY generated dispatch config; an existing deployment that pinned
        # `reviews: {enabled: false}` in its own dispatch.yaml keeps that setting.
        # Requires a `codex` lane in the dispatcher; falls back to the author lane if
        # absent. The 4-phase order is byte-identical when disabled.
        "reviews": {"enabled": True, "lane": "codex"},
        # R6 circuit breaker: after this many CONSECUTIVE specs land terminal-FAILED,
        # pause the repo queue (write .drain) + notify, so one bad pattern can't cascade
        # across a dozen specs overnight. 0 = disabled (default; opt in per repo).
        "max_consecutive_failed_specs": 0,
        # R6 roadmap budget kill-switch: pause the queue once this daemon run has spent
        # this many tokens (input+output) or wall-seconds of ATTEMPT time (not idle
        # uptime) across all attempts. 0 = disabled.
        "roadmap_budget": {"max_tokens": 0, "max_wall_seconds": 0},
        # R4 dependency-aware scheduling (opt-in, default OFF): a QUEUED item whose
        # spec declares a `required` dependency.yaml sibling not yet verified/archived
        # is held as BLOCKED_DEP instead of dispatched, auto-recovering to QUEUED once
        # every dependency verifies (or cascading to BLOCKED_HUMAN if a dependency's
        # own pipeline permanently stalls). Flag-off preserves prior unbounded/no-gate
        # behavior byte-for-byte.
        "dependency_gating": False,
        # R5 per-spec worktree isolation + scoped delivery (opt-in, default OFF): each
        # spec's attempt runs in its own `git worktree` (never `git worktree prune`)
        # instead of the shared project_dir, and delivery `git add`s only the files
        # traceability.yaml/handoff.yaml say the spec touched instead of `git add -A`.
        # Flag-off preserves the prior shared-workspace / add-A behavior byte-for-byte.
        "worktree_isolation": False,
        **dict(data.get("pipeline") or {}),
    }
    reviews = dict(pipeline.get("reviews") or {})
    # `default` is the per-spec review count when spec.yaml omits `reviews`.
    # Preserve old dispatch.yaml files exactly by deriving it from `enabled`.
    if "default" not in reviews:
        reviews["default"] = 1 if bool(reviews.get("enabled", False)) else 0
    if (not isinstance(reviews["default"], int)
            or isinstance(reviews["default"], bool)
            or reviews["default"] not in (0, 1, 2)):
        raise ConfigError("pipeline.reviews.default must be 0, 1, or 2")
    pipeline["reviews"] = reviews

    configured_queue_store_path = Path(str(queue_store["path"]))
    queue_store_path = (
        configured_queue_store_path
        if configured_queue_store_path.is_absolute()
        else config_path.resolve().parent / configured_queue_store_path
    )

    return DispatchConfig(
        queue_store_path=queue_store_path,
        lanes=_load_lanes(data.get("lanes")),
        routing_policy=dict(data.get("routing_policy") or {"default": "ordered"}),
        cooldown_policy=cooldown_policy,
        retry_policy=retry_policy,
        pipeline=pipeline,
    )
