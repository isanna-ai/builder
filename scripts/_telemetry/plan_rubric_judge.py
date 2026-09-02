"""Arm-blind LLM-judge rubric for the agent-lift A/B (Phase 2, R1).

Scores a produced Builder plan 0-10 on how well it adheres to the conventions
seeded into the A/B (``ab-memory-gain.py`` ``SEED_MEMORIES``): a per-convention
checklist, summed 0-N and rescaled to ``[0, 10]``.

Design (pinned by the spec):
  * BLIND — ``strip_prior_art`` removes the injected "PRIOR ART / KNOWN PITFALLS"
    block (push AND pull variants) before scoring, so the judge cannot tell the
    ``off`` / ``push-distilled`` / ``pull`` arm apart from the plan text.
  * k=2 — ``score_plan`` runs ``passes`` (default 2) independent scoring turns and
    returns the MEAN of the VALID passes; a pass whose output cannot be parsed into
    a 0/1-per-item checklist is discarded.
  * PINNED model — ``RUBRIC_JUDGE_MODEL`` (env-overridable, default ``"sonnet"``),
    recorded in the returned dict (and, by the run, in the committed report).
  * NEVER raises — on a CLI error, timeout, or unparseable output the offending
    pass is discarded; if NO pass is valid the result is ``rubric_score=None``
    ("unscored"), which the run records rather than aborting.

Spawn pattern mirrors ``memory_hook._distill_batch``: a lazily-imported
``run_cli_turn`` + ``_scrubbed_env`` driving ``claude -p --model <m> --output-format
json``, parsed with the same robust ``_coerce_json``-adjacent strategy (direct ->
fence-strip -> first-brace..last-brace). The model call is injected as a function
(``scorer``) so the scoring LOGIC is unit-testable WITHOUT a real model — tests pass
a fake ``scorer``; production resolves to the real ``claude -p`` spawn.

Anthropic note: the spawn is Claude Code's ``claude -p`` headless CLI (Max
subscription with ANTHROPIC keys scrubbed by the lane), NOT a direct SDK call — the
model id is a Claude Code alias ("sonnet"/"haiku"), not a dated API id.
"""

from __future__ import annotations

import json
import os
from typing import Any, Callable

# Pinned judge model. A Claude Code alias (resolves on the Max subscription where
# dated API ids 404), recorded in the run's report. Distinct from the lane's
# planning model where feasible. Env-overridable for the run.
RUBRIC_JUDGE_MODEL = os.environ.get("RUBRIC_JUDGE_MODEL", "sonnet")

# How long a single scoring turn may run (seconds). One short claude -p turn; kept
# well under any phase timeout so a hung judge degrades to "unscored" not a stall.
_JUDGE_TIMEOUT_S = 120

# The injected "Prior art / known pitfalls" section headings the goal builder emits
# (push and pull variants, see phase_runtime.py). Stripping the block makes the
# judge arm-blind. Matched case-insensitively against the leading "=== ... ===" line.
_PRIOR_ART_HEADINGS = (
    "=== prior art / known pitfalls ===",
    "=== prior art / known pitfalls (pull) ===",
)

# A type alias for the injected model-call. Given the assembled prompt + model id it
# returns the raw CLI/model stdout (a string), or "" / raises on failure (handled).
Scorer = Callable[[str, str], str]

# The per-convention checklist, one anchored item per seeded convention
# (SEED_MEMORIES). Each is scored 0/1; the sum is rescaled to [0, 10].
RUBRIC_ITEMS: tuple[tuple[str, str], ...] = (
    (
        "integer_minor_units",
        "Monetary amounts are represented as integer minor units (cents), never "
        "binary floats; conversions happen only at the formatting boundary.",
    ),
    (
        "typed_domain_error",
        "Inputs are validated at the constructor boundary and a typed DomainError "
        "(not a silent coercion or a null/None return) is raised on invalid input.",
    ),
    (
        "colocated_test",
        "Each helper has a co-located unit test covering the zero/identity case, a "
        "representative case, and an invalid-input raise.",
    ),
    (
        "immutable_construct_and_return",
        "Value-object helpers are immutable: construct-and-return a new value, never "
        "mutate in place, and expose no setters.",
    ),
    (
        "structural_equals",
        "Value objects define a structural equals() compared by value, not by "
        "reference/identity.",
    ),
)

# Max raw checklist sum and the rescale factor to [0, 10].
_MAX_RAW = len(RUBRIC_ITEMS)
_RESCALE = 10.0 / _MAX_RAW if _MAX_RAW else 0.0


def _is_section_heading(line: str) -> bool:
    """A goal section starts with a ``=== ... ===`` heading line. Used to find where
    the prior-art block ends (the block itself contains internal blank lines, so a
    blank-line stop is not enough)."""
    s = line.strip()
    return s.startswith("===") and s.endswith("===") and len(s) >= 6


def _is_prior_art_heading(line: str) -> bool:
    """True iff ``line`` is the injected prior-art block heading (the block we want
    to STRIP). Uses a PREFIX match so future arm-suffixed variants (e.g.
    ``=== PRIOR ART / KNOWN PITFALLS (PULL) ===``) are also caught."""
    return line.strip().lower().startswith("=== prior art / known pitfalls")


def strip_prior_art(plan_text: str) -> str:
    """Remove the injected "PRIOR ART / KNOWN PITFALLS" block from ``plan_text`` so
    the judge is arm-blind. The goal builder appends the block as a ``\\n\\n``-joined
    section starting with a ``=== PRIOR ART / KNOWN PITFALLS ... ===`` heading line;
    the block body itself contains an internal blank line (an explanatory sentence,
    then the bulleted memories), so the block runs from its heading until the NEXT
    ``=== ... ===`` section heading (or end-of-text). NEVER raises.

    The BODY terminator is ``_is_section_heading`` applied to every line AFTER the
    prior-art heading — the heading itself is already consumed before the inner scan
    begins, so it cannot accidentally re-trigger the end-of-block condition."""
    text = plan_text or ""
    lines = text.split("\n")
    out: list[str] = []
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        if _is_prior_art_heading(line):
            # Skip the heading, then skip the whole block body: advance past every
            # line until the NEXT ``=== ... ===`` section heading (or end-of-text).
            # The heading itself is already consumed (i+1), so an embedded
            # ``=== x ===`` line inside the body correctly ends the block, while the
            # prior-art heading itself does NOT re-trigger the condition.
            i += 1
            while i < n and not _is_section_heading(lines[i]):
                i += 1
            continue
        out.append(line)
        i += 1
    # Collapse any run of 3+ blank lines the removal may have produced back to the
    # canonical section separator (a single blank line). Keeps the result tidy without
    # changing meaning; the judge only reads it.
    result = "\n".join(out)
    while "\n\n\n" in result:
        result = result.replace("\n\n\n", "\n\n")
    return result.strip("\n")


def _build_prompt(plan_text: str) -> str:
    """Assemble the per-convention checklist scoring prompt. Instructs the model to
    return ONLY a JSON object mapping each convention key -> 0 or 1, which makes the
    parse deterministic and ``_coerce_json``-adjacent."""
    keys = [key for key, _ in RUBRIC_ITEMS]
    checklist = "\n".join(f"- {key}: {desc}" for key, desc in RUBRIC_ITEMS)
    example = json.dumps({key: 0 for key in keys})
    return (
        "You are a strict, blind reviewer scoring a software implementation PLAN for "
        "adherence to a fixed set of engineering conventions. You are given ONLY the "
        "plan text. For EACH convention below, output 1 if the plan clearly satisfies "
        "or commits to it, else 0.\n\n"
        "CONVENTIONS:\n"
        f"{checklist}\n\n"
        "Return ONLY a single JSON object mapping each convention key to 0 or 1 — no "
        "prose, no markdown, no commentary. Use exactly these keys:\n"
        f"{example}\n\n"
        "PLAN:\n"
        f"{plan_text}\n"
    )


def _coerce_json_object(text: str) -> dict[str, Any] | None:
    """Parse a JSON object out of a model answer string, mirroring
    ``memory_hook._coerce_json_array``'s robustness for objects: direct parse ->
    code-fence strip -> first-'{'..last-'}' extraction. Returns a dict or None."""
    t = (text or "").strip()
    if not t:
        return None
    try:
        parsed = json.loads(t)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        pass
    # Strip a leading ``` or ```json fence and the trailing ``` fence.
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else ""
        if t.rstrip().endswith("```"):
            t = t.rstrip()[:-3]
        try:
            parsed = json.loads(t.strip())
            return parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            pass
    # Last resort: extract the outermost { ... } span.
    start, end = t.find("{"), t.rfind("}")
    if 0 <= start < end:
        try:
            parsed = json.loads(t[start : end + 1])
            return parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            return None
    return None


def _unwrap_cli_envelope(text: str) -> str:
    """A ``claude -p --output-format json`` turn wraps the model's answer in an
    envelope ``{"result": "...", "is_error": ...}``. Unwrap to the answer string;
    pass the text through untouched when it is not a recognizable envelope (lets a
    fake scorer return the bare checklist object directly)."""
    t = (text or "").strip()
    if not t:
        return t
    try:
        payload = json.loads(t)
    except json.JSONDecodeError:
        return t
    if isinstance(payload, dict) and ("result" in payload or "is_error" in payload):
        if payload.get("is_error"):
            return ""
        result = payload.get("result")
        if isinstance(result, str):
            return result
    return t


def _score_one_pass(plan_text: str, model: str, scorer: Scorer) -> float | None:
    """Run ONE scoring pass: build the prompt, invoke ``scorer``, parse a 0/1-per-item
    checklist, sum, and rescale to ``[0, 10]``. Returns the pass score (a float in
    ``[0, 10]``) or None when the pass cannot be parsed into a valid checklist. NEVER
    raises — a scorer exception is caught and yields None."""
    prompt = _build_prompt(plan_text)
    try:
        raw = scorer(prompt, model)
    except Exception:  # noqa: BLE001 - a scorer failure discards this pass, never raises
        return None
    answer = _unwrap_cli_envelope(raw)
    obj = _coerce_json_object(answer)
    if obj is None:
        return None
    raw_sum = 0
    for key, _ in RUBRIC_ITEMS:
        value = obj.get(key)
        # Accept booleans (subclass of int — must be checked FIRST), strict 0/1
        # integers, and clean "0"/"1" strings. Non-{0,1} floats (e.g. 0.5) are
        # rejected to avoid silent truncation: int(0.5) -> 0 misrepresents the
        # scorer's answer and MUST invalidate the pass instead.
        if isinstance(value, bool):
            point = 1 if value else 0
        elif isinstance(value, float):
            # Reject non-integer floats (e.g. 0.5) — invalidate the whole pass.
            if value not in (0.0, 1.0):
                return None
            point = int(value)
        elif isinstance(value, int):
            point = value
        elif isinstance(value, str) and value.strip() in ("0", "1"):
            point = int(value.strip())
        else:
            return None
        if point not in (0, 1):
            return None
        raw_sum += point
    return round(raw_sum * _RESCALE, 4)


def _default_scorer(prompt: str, model: str) -> str:
    """Production scorer: drive ONE headless ``claude -p`` turn (Max subscription;
    ANTHROPIC keys scrubbed by the lane) and return raw stdout. Lazy imports mirror
    ``memory_hook._distill_batch`` to avoid an import cycle. Returns "" on a timeout
    so the pass is discarded."""
    from _dispatch_runtime.lane_claude_code_cli import _scrubbed_env
    from _dispatch_runtime.lane_common import run_cli_turn

    command = [
        "claude", "-p", prompt,
        "--model", model,
        "--output-format", "json",
    ]
    returncode, stdout, stderr, timed_out = run_cli_turn(
        command, cwd=os.getcwd(), env=_scrubbed_env(), timeout=_JUDGE_TIMEOUT_S,
    )
    if timed_out:
        return ""
    return stdout or ""


def score_plan(
    plan_text: str,
    *,
    model: str | None = None,
    passes: int = 2,
    scorer: Scorer | None = None,
) -> dict[str, Any]:
    """Score a produced plan 0-10 against the seeded conventions, arm-blind, k=2.

    Strips the injected prior-art block (arm-blind), then runs ``passes`` independent
    scoring turns via ``scorer`` (default: the real ``claude -p`` spawn; inject a fake
    in tests). Returns::

        {"rubric_score": float|None, "model": resolved, "passes": [..], "blind": True}

    ``rubric_score`` is the MEAN of the VALID passes (a float in ``[0, 10]``), or
    ``None`` ("unscored") when NO pass is valid. ``passes`` lists each pass's score
    (a float or None) for transparency. NEVER raises — any spawn/parse failure
    discards that pass; total failure yields ``rubric_score=None``.

    The model is resolved at CALL TIME (not at import time) so a post-import env
    override of ``RUBRIC_JUDGE_MODEL`` is respected: ``model`` wins when given;
    else ``os.environ.get('RUBRIC_JUDGE_MODEL', 'sonnet')`` is read fresh."""
    resolved = model or os.environ.get("RUBRIC_JUDGE_MODEL", "sonnet")
    if scorer is None:
        scorer = _default_scorer
    blind_text = strip_prior_art(plan_text)
    n = max(int(passes), 1)
    pass_scores: list[float | None] = []
    for _ in range(n):
        pass_scores.append(_score_one_pass(blind_text, resolved, scorer))
    valid = [s for s in pass_scores if s is not None]
    mean = round(sum(valid) / len(valid), 4) if valid else None
    return {
        "rubric_score": mean,
        "model": resolved,
        "passes": pass_scores,
        "blind": True,
    }


def rubric_score_to_minor_units(rubric_score: float | None) -> int:
    """Convert a 0.0-10.0 rubric mean to the ``memory_eval.rubric_score`` integer
    encoding (x10, clamped to [0, 100]). ``None`` ("unscored") maps to ``0`` — the
    same default an un-stamped record carries, so an unscored plan is indistinct from
    a non-A/B record (the run records the raw ``None`` in its report, not the row)."""
    if rubric_score is None:
        return 0
    value = int(round(rubric_score * 10))
    return max(0, min(100, value))
