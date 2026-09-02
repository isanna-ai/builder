"""No user-facing text may claim review falls back to the author's own lane.

This idea has now been written into the repo three times, by three different routes, and it has
never been true:

  * three installed skills said review phases "fall back to the author's lane -- they still run
    and still write review-log.yaml, but lose model independence";
  * `isanna init --no-reviews` help said "reviews run on the author's own model family only if
    you also configure one lane";
  * and each time, `personas.select_independent_review_lane` was raising ValueError, with
    `scheduler.py` calling it uncaught.

Some of those were introduced by the fix for the one before. Removing instances has not worked;
the claim keeps being re-derived because it sounds like reasonable degradation. It is not what the
system does, and it is the wrong belief to hold about a product whose value is that review is
independent -- a user who thinks review merely degrades will accept a configuration that in fact
removes it.

So the shape itself becomes a failing test.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# Files a user or an agent actually reads.
SURFACES = [
    *(ROOT / "skills").glob("*/SKILL.md"),
    *(ROOT / "prompts").glob("*.prompt.md"),
    *(ROOT / "standards").glob("*.md"),
    ROOT / "README.md",
    ROOT / "AGENTS.md",
    ROOT / "scripts" / "isanna.py",
    ROOT / "scripts" / "init.py",
]

# "review ... falls back to / runs on ... the author's lane|model|family"
_CLAIM = re.compile(
    r"(?is)review[^.]{0,120}?(?:falls?\s+back\s+to|still\s+runs?\s+on|runs?\s+on)"
    r"[^.]{0,60}?author(?:'s)?\s+(?:own\s+)?(?:lane|model|family)"
)


def test_no_surface_claims_review_falls_back_to_the_author_lane():
    offenders: list[str] = []
    for path in SURFACES:
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        for m in _CLAIM.finditer(text):
            line = text[: m.start()].count("\n") + 1
            offenders.append(f"{path.relative_to(ROOT)}:{line}: {m.group(0)[:110]!r}")
    assert offenders == [], (
        "review does NOT fall back to the author's lane -- select_independent_review_lane raises, "
        "and scheduler.py does not catch it. These surfaces say otherwise:\n  "
        + "\n  ".join(offenders)
    )


def test_the_detector_matches_the_shape_it_is_guarding():
    """Guard the guard: all three historical phrasings must be caught, or this passes vacuously."""
    assert _CLAIM.search("the review phases fall back to the author's lane -- they still run")
    assert _CLAIM.search("Without an independent review lane, review phases fall back to the author's lane")
    assert _CLAIM.search("reviews run on the author's own model family only if you also configure one lane")
    assert not _CLAIM.search("dispatch fails loudly rather than reviewing with the author's own model")
