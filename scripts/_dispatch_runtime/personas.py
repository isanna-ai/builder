"""Persona runner-profile registry: phase/turn -> {harness, capability class, skills, charter}.

Layers on the existing phase->capability routing (phase_routing.PHASE_CLASS_MAP,
model_registry.CAPABILITY_MODEL_MAP) rather than introducing a new runtime
subsystem. A Persona is pure data; persona_for_phase() is a pure resolver. Two
independence invariants are enforced BY CONFIGURATION, not discipline:

  1. No persona judges its own output (spec-review != spec, adversarial-review
     != implement, verify != implement).
  2. adversarial-review resolves to a different model family (vendor) than the
     implementing author, under the active lane routing.

assert_persona_independence() checks both and is called once at import time so
a broken persona map fails loudly at load, before it can ever staff a turn.
"""

from __future__ import annotations

from dataclasses import dataclass

from _dispatch_runtime.model_registry import resolve_model
from _dispatch_runtime.phase_routing import REVIEW_LANE_PHASES, route_lane
from _dispatch_runtime.phase_runtime import normalize_phase


@dataclass(frozen=True)
class Persona:
    key: str
    name: str
    harness: str  # "author" | "review" — which lane role staffs this turn
    capability_class: str  # reuses phase_routing.PHASE_CLASS_MAP classes
    skills: tuple[str, ...]
    charter: str


PM = Persona(
    key="pm",
    name="PM",
    harness="author",
    capability_class="deep_reasoner",
    skills=("requirements-elicitation", "acceptance-criteria", "stakeholder-clarification"),
    charter=(
        "Define the problem precisely: turn intent into EARS acceptance criteria and a "
        "locked design. Ask, never guess — when the ask is ambiguous, surface the "
        "ambiguity instead of inventing a resolution."
    ),
)

SPEC_REVIEWER = Persona(
    key="cross_model_reviewer_spec",
    name="Spec Reviewer",
    harness="review",
    capability_class="independent_reviewer",
    skills=("adversarial-review", "contract-verification", "acceptance-criteria-audit"),
    charter=(
        "Independently and adversarially review the formalized spec before planning "
        "begins. Judge only the artifact, never the author's intent. Record every "
        "finding and follow the session's review auto-application policy."
    ),
)

ARCHITECT = Persona(
    key="architect",
    name="Architect",
    harness="author",
    capability_class="structured_planner",
    skills=("task-decomposition", "dependency-sequencing", "gate-lane-design"),
    charter=(
        "Turn the locked spec into a TDD-anchored task breakdown with an explicit "
        "proposed gate lane. Propose the plan; do not implement it."
    ),
)

DEVELOPER = Persona(
    key="developer",
    name="Developer",
    harness="author",
    capability_class="fast_editor",
    skills=("test-driven-implementation", "refactoring", "code-review-response"),
    charter=(
        "Implement the approved plan by writing code and tests. Apply confirmed review "
        "findings without re-litigating them."
    ),
)

DATA_ENGINEER = Persona(
    key="data_engineer",
    name="Data Engineer",
    harness="author",
    capability_class="fast_editor",
    skills=("schema-migration", "data-pipeline-integrity", "backward-compatible-rollout"),
    charter=(
        "Implement schema, migration, and data-pipeline changes with backward-compatible "
        "rollout and data-integrity checks the generic developer profile does not carry."
    ),
)

ADVERSARIAL_REVIEWER = Persona(
    key="cross_model_reviewer_security",
    name="Adversarial Reviewer",
    harness="review",
    capability_class="independent_reviewer",
    skills=("adversarial-review", "security-review", "defect-hunting"),
    charter=(
        "Independently and adversarially review the IMPLEMENTED code against "
        "requirements/design, on a different model family than the author. Security: "
        "for sensitive surfaces (auth, secrets, injection, data exposure), explicitly "
        "assess trust and authorization boundaries. Record every finding and follow "
        "the session's review auto-application policy."
    ),
)

QA = Persona(
    key="qa",
    name="QA",
    harness="review",
    capability_class="independent_reviewer",
    skills=("host-verification", "regression-testing", "gate-evidence-audit"),
    charter=(
        "Independently verify the implementation against requirements/design and run "
        "all verification commands. The host-verified gate evidence, not your claim, "
        "stays the proof."
    ),
)

LIBRARIAN = Persona(
    key="librarian",
    name="Librarian",
    harness="author",
    capability_class="deep_reasoner",
    skills=("ssot-reconciliation", "documentation-sync", "provenance-tracking"),
    charter=(
        "Reconcile the shipped change into the SSOT: apply the ssot-delta and keep "
        "provenance and documentation in sync with what was actually built."
    ),
)

# Every phase in phase_runtime.REVIEW2_SPEC_PHASE_ORDER resolves to exactly one
# Persona. `implement` / `review-fix` map to the developer here; persona_for_phase()
# swaps in DATA_ENGINEER when the spec's ssot-delta declares a schema/migration/
# pipeline touch (see declares_schema_touch below).
PHASE_PERSONA_MAP: dict[str, Persona] = {
    "spec": PM,
    "spec-review": SPEC_REVIEWER,
    "spec-review-2": SPEC_REVIEWER,
    "plan": ARCHITECT,
    "implement": DEVELOPER,
    "adversarial-review": ADVERSARIAL_REVIEWER,
    "adversarial-review-2": ADVERSARIAL_REVIEWER,
    "review-fix": DEVELOPER,
    "verify": QA,
    "sync": LIBRARIAN,
}

# Phases where the implement profile can swap developer -> data-engineer based on
# the spec's declared delta.
_SCHEMA_SWAP_PHASES = frozenset({"implement", "review-fix"})

# Signals in an ssot-delta target/change that mark a schema/migration/pipeline touch.
_SCHEMA_SIGNALS = ("schema", "migration", "pipeline")


def declares_schema_touch(ssot_delta) -> bool:
    """True if an ssot-delta declares a schema/migration/pipeline touch.

    Shape-safe: None, a non-dict, or a delta missing the usual capabilities/
    behaviors/journeys lists returns False rather than raising.
    """
    if not isinstance(ssot_delta, dict):
        return False
    for section in ("capabilities", "behaviors", "journeys"):
        items = ssot_delta.get(section)
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            target = str(item.get("target") or "").lower()
            change = str(item.get("change") or "").lower()
            text = f"{target} {change}"
            if any(signal in text for signal in _SCHEMA_SIGNALS):
                return True
    return False


def persona_for_phase(phase: str, ssot_delta=None) -> Persona:
    """Resolve the staffing Persona for a phase/turn.

    Raises KeyError for a phase with no mapped persona — an unmapped phase must
    fail loudly rather than fall back to an unstaffed default that could let an
    author grade its own work.
    """
    norm = normalize_phase(phase)
    if norm is None or norm not in PHASE_PERSONA_MAP:
        raise KeyError(f"No persona mapped for phase {phase!r}")
    if norm in _SCHEMA_SWAP_PHASES and declares_schema_touch(ssot_delta):
        return DATA_ENGINEER
    return PHASE_PERSONA_MAP[norm]


def _vendor_family(model_id: str) -> str:
    m = model_id.lower()
    if m.startswith("claude"):
        return "anthropic"
    if m.startswith("gpt"):
        return "openai"
    return "unknown"


def _provider_for_lane(
    lane: str,
    lane_providers: dict[str, str] | None = None,
) -> str:
    """Resolve a configured lane name to its provider id.

    Dispatch lane names are operator-defined, so provider detection must use the
    loaded config when available rather than assuming every lane is literally
    named ``claude`` or ``codex``. The name fallback preserves the small pure
    default used by import-time validation and unit tests.
    """
    if lane_providers and lane in lane_providers:
        return lane_providers[lane]
    lowered = lane.lower()
    if "codex" in lowered:
        return "codex-cli"
    if "claude" in lowered:
        return "claude-code-cli"
    raise ValueError(f"No provider known for lane {lane!r}")


def model_family_for_phase(
    phase: str,
    *,
    default_lane: str | None = None,
    review_lane: str = "codex",
    lane_providers: dict[str, str] | None = None,
    persona_map: dict[str, Persona] | None = None,
) -> str:
    """The vendor family (e.g. "anthropic" / "openai") the phase's persona resolves
    to under the supplied lane routing.

    ``lane_providers`` makes this reflect live dispatch configuration, including
    operator-defined lane names. Without it, the canonical claude/codex pair is
    used for import-time validation.
    """
    norm = normalize_phase(phase)
    pm = persona_map if persona_map is not None else PHASE_PERSONA_MAP
    if norm is None or norm not in pm:
        raise KeyError(f"No persona mapped for phase {phase!r}")
    persona = pm[norm]
    base_lane = default_lane or "claude"
    available_lanes = (
        list(lane_providers)
        if lane_providers
        else sorted({base_lane, review_lane})
    )
    routed = route_lane(norm, available_lanes, default_lane=base_lane)
    if norm in REVIEW_LANE_PHASES:
        picked = next((ln for ln in available_lanes if review_lane.lower() in ln.lower()), None)
        if picked:
            routed = picked
    lane_provider = _provider_for_lane(routed, lane_providers)
    model_id = resolve_model(persona.capability_class, lane_provider)
    if not model_id:
        raise ValueError(
            f"No model resolved for phase {phase!r} (capability={persona.capability_class!r}, "
            f"lane_provider={lane_provider!r})"
        )
    return _vendor_family(model_id)


def select_independent_review_lane(
    author_phase: str,
    review_phase: str,
    author_lane: str,
    preferred_review_lane: str,
    lane_providers: dict[str, str],
    *,
    persona_map: dict[str, Persona] | None = None,
) -> str:
    """Choose a configured review lane whose model family differs from the author.

    The preferred review lane wins when it is independent. If it aliases the
    author's provider family, another configured lane is selected deterministically.
    A configuration with no independent lane fails loudly instead of silently
    presenting same-family review as cross-model review.
    """
    if author_lane not in lane_providers:
        raise ValueError(f"Author lane {author_lane!r} is not configured")
    candidates = [preferred_review_lane]
    candidates.extend(sorted(lane for lane in lane_providers if lane != preferred_review_lane))
    author_family = model_family_for_phase(
        author_phase,
        default_lane=author_lane,
        review_lane=preferred_review_lane,
        lane_providers=lane_providers,
        persona_map=persona_map,
    )
    for candidate in candidates:
        if candidate not in lane_providers:
            continue
        review_family = model_family_for_phase(
            review_phase,
            default_lane=author_lane,
            review_lane=candidate,
            lane_providers=lane_providers,
            persona_map=persona_map,
        )
        if review_family != author_family:
            return candidate
    raise ValueError(
        f"No independent review lane for {review_phase!r}: every configured lane "
        f"resolves to author model family {author_family!r}"
    )


# Judge/judged pairs: the persona staffing the judging turn must differ from the
# persona staffing the turn it judges.
_INDEPENDENCE_PAIRS = (
    ("spec-review", "spec"),
    ("adversarial-review", "implement"),
    ("verify", "implement"),
)


def assert_persona_independence(
    persona_map: dict[str, Persona] | None = None,
    *,
    default_lane: str | None = None,
    review_lane: str = "codex",
    lane_providers: dict[str, str] | None = None,
) -> None:
    """Raise unless the persona map satisfies both independence invariants.

    (a) Each review/verify turn's persona differs from the persona of the turn
        it judges (see _INDEPENDENCE_PAIRS).
    (b) adversarial-review resolves to a different model family than implement
        under the default lane routing.

    Called once at module import against the shipped PHASE_PERSONA_MAP so a
    broken map fails loudly at load time, not the first time it staffs a turn.
    """
    pm = persona_map if persona_map is not None else PHASE_PERSONA_MAP
    for judge_phase, judged_phase in _INDEPENDENCE_PAIRS:
        judge = pm.get(judge_phase)
        judged = pm.get(judged_phase)
        if judge is None or judged is None:
            raise KeyError(f"persona map missing {judge_phase!r} or {judged_phase!r}")
        if judge.key == judged.key:
            raise ValueError(
                f"persona independence violated: {judge_phase!r} is staffed by the same "
                f"persona ({judge.key!r}) as the {judged_phase!r} turn it judges"
            )
    author_family = model_family_for_phase(
        "implement",
        default_lane=default_lane,
        review_lane=review_lane,
        lane_providers=lane_providers,
        persona_map=pm,
    )
    review_family = model_family_for_phase(
        "adversarial-review",
        default_lane=default_lane,
        review_lane=review_lane,
        lane_providers=lane_providers,
        persona_map=pm,
    )
    if author_family == review_family:
        raise ValueError(
            f"persona independence violated: adversarial-review and implement both "
            f"resolve to model family {author_family!r}"
        )


assert_persona_independence()
