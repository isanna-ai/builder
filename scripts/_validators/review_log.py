from __future__ import annotations

from typing import Any

from .canonical import validate_canonical_artifact
from .common import ValidationContext
from .renderers import render_review_log


def run(context: ValidationContext):
    return validate_canonical_artifact(
        context,
        artifact_name="review-log",
        source_file="review-log.yaml",
        schema_file="review-log.schema.yaml",
        render=render_review_log,
        rendered_file="review-log.md",
        extra_validation=validate_review_log,
    )


def validate_review_log(data: dict[str, Any], source_name: str) -> list[str]:
    errors: list[str] = []
    finding_ids: set[str] = set()
    for index, finding in enumerate(data.get("findings") or [], start=1):
        if not isinstance(finding, dict):
            continue
        finding_id = str(finding.get("id", "")).strip()
        if finding_id in finding_ids:
            errors.append(f"{source_name}.findings[{index}]: duplicate id `{finding_id}`")
        finding_ids.add(finding_id)
    return errors