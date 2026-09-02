from __future__ import annotations

from typing import Any

from .canonical import validate_canonical_artifact
from .common import ValidationContext
from .renderers import render_constitution_review


VALID_VERDICTS = {"pass", "warn", "block", "requires-human-decision", "skipped"}


def run(context: ValidationContext):
    return validate_canonical_artifact(
        context,
        artifact_name="constitution-review",
        source_file="constitution-review.yaml",
        schema_file="constitution-review.schema.yaml",
        render=render_constitution_review,
        rendered_file="constitution-review.md",
        extra_validation=validate_constitution_review,
    )


def validate_constitution_review(data: dict[str, Any], source_name: str) -> list[str]:
    errors: list[str] = []
    verdict = str(data.get("verdict", "")).strip()
    if verdict not in VALID_VERDICTS:
        errors.append(f"{source_name}.verdict: expected one of {sorted(VALID_VERDICTS)}")

    results = data.get("principle_results") if isinstance(data.get("principle_results"), list) else []
    blocking = [
        item
        for item in results
        if isinstance(item, dict) and str(item.get("status", "")).strip() == "block"
    ]
    decisions = [
        item
        for item in results
        if isinstance(item, dict) and str(item.get("status", "")).strip() == "requires-human-decision"
    ]
    if blocking and verdict != "block":
        errors.append(f"{source_name}.verdict: blocking principle results require verdict `block`")
    if decisions and verdict not in {"block", "requires-human-decision"}:
        errors.append(f"{source_name}.verdict: decision principle results require a decision verdict")
    return errors

