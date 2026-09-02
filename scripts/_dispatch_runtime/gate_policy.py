"""Deterministic, versioned gate-lane policy engine.

Replaces the blanket human plan-gate approve with a three-lane decision:

  - Lane A (flow-through): low-risk surfaces pass automatically once required
    artifacts exist and validators are green.
  - Lane B (veto window, the DEFAULT): opens after a quiet period with no
    recorded hold; a notification fires when the window opens.
  - Lane C (human approve): policy-listed high-risk surfaces (migrations,
    critical ssot-delta, auth/payment, deploy config, public contracts) never
    open on silence — an explicit recorded human approval is required.

`decide()` is pure and side-effect free: given a policy version, the
architect's proposed lane, and declared risk signals, it always returns the
same lane. The architect's proposed lane is advisory input ONLY — the engine
may raise it (never lower it) and is the sole decider of the final lane. No
agent-authored value may become the final gate decision (see
`reject_agent_authored_decision`).
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from _yaml import yaml  # type: ignore

POLICY_FILENAME = "gate-lane-policy.yaml"
SHIPPED_POLICY_PATH = Path(__file__).resolve().parents[2] / "templates" / POLICY_FILENAME

LANE_RANK = {"A": 0, "B": 1, "C": 2}
VALID_LANES = frozenset(LANE_RANK)


def _read_policy_document(path: Path) -> dict[str, Any]:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"gate-lane policy must be a mapping: {path}")
    return raw


class _ShippedPolicy(Mapping[str, Any]):
    """Lazy view of the shipped policy.

    Importing unrelated dispatcher modules must not perform filesystem I/O:
    some tools deliberately copy only ``scripts/``. Read the policy document
    only when a gate decision actually needs its defaults.
    """

    @staticmethod
    def _data() -> dict[str, Any]:
        return _read_policy_document(SHIPPED_POLICY_PATH)

    def __getitem__(self, key: str) -> Any:
        return self._data()[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._data())

    def __len__(self) -> int:
        return len(self._data())


# Compatibility export for callers/tests that inspect defaults. The
# authoritative values live entirely in the shipped YAML document.
DEFAULT_POLICY: Mapping[str, Any] = _ShippedPolicy()

# Keys an agent's plan-turn output may legitimately declare — advisory input
# only. Any OTHER gate-shaped key found on agent-authored data (a `handoff.yaml`,
# a plan packet, ...) is a self-classification attempt and must be rejected
# rather than silently honored.
ADVISORY_INPUT_KEYS = frozenset({"proposed_gate_lane", "gate_risk_signals"})
FORBIDDEN_AGENT_GATE_KEYS = frozenset({
    "gate_lane", "final_lane", "gate_decision", "lane_decision", "resolved_lane",
})


class SelfClassificationError(RuntimeError):
    """Raised when agent-authored data tries to write a final gate decision."""


def default_policy_path(project_dir: Path) -> Path:
    from _dispatch_runtime.paths import runtime_dir

    return runtime_dir(Path(project_dir)) / POLICY_FILENAME


def load_policy(path: Path | None = None) -> dict[str, Any]:
    """Load the versioned gate-lane policy document.

    Falls back to `DEFAULT_POLICY` (still versioned DATA, never a code
    constant consulted by `decide()` directly) when `path` is None or absent.
    A present file only needs to override the keys it cares about — unset
    keys inherit the default, so a project can edit just `lane_c_surfaces`
    without also having to restate `veto_window`.
    """
    if path is None or not Path(path).is_file():
        return {k: (dict(v) if isinstance(v, dict) else list(v) if isinstance(v, list) else v)
                 for k, v in DEFAULT_POLICY.items()}
    raw = _read_policy_document(Path(path))
    merged = dict(DEFAULT_POLICY)
    merged.update(raw)
    return merged


@dataclass(frozen=True)
class GateLaneDecision:
    """The sole gate authority's output. `lane` is the ONLY value a caller may
    act on; `proposed_lane`/`risk_signals` are the advisory inputs recorded
    for audit, `data_lane` is what the policy data alone implied, and
    `policy_version` is the resolved version this decision was made against."""

    lane: str
    policy_version: str
    proposed_lane: str | None
    risk_signals: tuple[str, ...]
    data_lane: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "lane": self.lane,
            "policy_version": self.policy_version,
            "proposed_lane": self.proposed_lane,
            "risk_signals": list(self.risk_signals),
            "data_lane": self.data_lane,
        }


def decide(
    proposed_lane: str | None,
    risk_signals: Any,
    policy: dict[str, Any],
) -> GateLaneDecision:
    """Deterministically map (proposed_lane, risk_signals) against a versioned
    `policy` to exactly one lane (A, B, or C).

    Data-driven classification: a declared risk signal that hits the policy's
    `lane_c_surfaces` always yields lane C, regardless of what was proposed
    (an "under-classified" proposal is overridden upward). Lane A requires
    every declared signal to be a recognized low-risk surface; an empty or
    unrecognized signal set never earns lane A — it falls to the lane-B
    default (R3: "lane B whenever the policy does not list a surface as lane
    A or lane C"). The proposed lane acts only as a FLOOR the engine may
    raise above, never a value the engine lowers to: `final = max(data_lane,
    proposed_lane)`. Identical inputs against the same policy always produce
    the same decision — no randomness, no clock, no I/O.
    """
    version = str(policy.get("version", DEFAULT_POLICY["version"]))
    lane_a = {str(s) for s in (policy.get("lane_a_surfaces") or [])}
    lane_c = {str(s) for s in (policy.get("lane_c_surfaces") or [])}
    signals = frozenset(str(s) for s in (risk_signals or []))

    if signals & lane_c:
        data_lane = "C"
    elif signals and signals <= lane_a:
        data_lane = "A"
    else:
        data_lane = "B"

    normalized_proposed = proposed_lane if proposed_lane in VALID_LANES else "B"
    final_lane = (
        data_lane if LANE_RANK[data_lane] >= LANE_RANK[normalized_proposed] else normalized_proposed
    )

    return GateLaneDecision(
        lane=final_lane,
        policy_version=version,
        proposed_lane=proposed_lane,
        risk_signals=tuple(sorted(signals)),
        data_lane=data_lane,
    )


def lane_a_flow_through_ready(
    spec_dir: Path,
    required_artifact_groups: list[list[str]],
    *,
    validators_green: bool,
) -> bool:
    """AC-R2-1/AC-R2-2: a lane-A gate auto-passes ONLY when every required
    artifact group has at least one existing file in `spec_dir` AND
    `validators_green` is True. Missing artifacts or a red validator run
    means the gate does not auto-pass, regardless of the decided lane."""
    if not validators_green:
        return False
    for group in required_artifact_groups or []:
        if not any((Path(spec_dir) / name).exists() for name in group):
            return False
    return True


def extract_proposed_gate_lane(handoff: dict[str, Any] | None) -> tuple[str | None, list[str]]:
    """Pull ONLY the advisory `proposed_gate_lane` / `gate_risk_signals` keys
    out of agent-authored data (a plan turn's `handoff.yaml`). Never reads a
    final/resolved lane field — an agent writing one is ignored here, not
    honored; see `reject_agent_authored_decision` for the hard guard used
    where agent output is otherwise trusted verbatim."""
    data = handoff if isinstance(handoff, dict) else {}
    proposed = data.get("proposed_gate_lane")
    if proposed not in VALID_LANES:
        proposed = None
    raw_signals = data.get("gate_risk_signals")
    signals = [str(s) for s in raw_signals] if isinstance(raw_signals, list) else []
    return proposed, signals


def reject_agent_authored_decision(payload: dict[str, Any] | None) -> None:
    """Guard: raise `SelfClassificationError` if agent-authored `payload`
    (e.g. a plan turn's handoff.yaml, a review packet) carries any of
    `FORBIDDEN_AGENT_GATE_KEYS` — a direct attempt to write a gate decision
    or select a final lane instead of merely proposing one. `decide()` is the
    ONLY function permitted to produce a `GateLaneDecision`; nothing else may
    set `lane`/`gate_decision`/etc. and have it honored."""
    data = payload if isinstance(payload, dict) else {}
    found = FORBIDDEN_AGENT_GATE_KEYS & set(data.keys())
    if found:
        raise SelfClassificationError(
            f"agent-authored data may not write a gate decision (forbidden keys: {sorted(found)}); "
            f"only proposed_gate_lane/gate_risk_signals are advisory input, and only "
            f"gate_policy.decide() may resolve the final lane"
        )
