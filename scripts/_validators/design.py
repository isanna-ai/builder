from __future__ import annotations

from typing import Any

from .canonical import validate_canonical_artifact
from .common import ValidationContext
from .renderers import render_design


def run(context: ValidationContext):
    return validate_canonical_artifact(
        context,
        artifact_name="design",
        source_file="design.yaml",
        schema_file="design.schema.yaml",
        render=render_design,
        rendered_file="design.md",
        extra_validation=validate_design,
    )


def validate_design(data: dict[str, Any], source_name: str) -> list[str]:
    errors: list[str] = []
    if not data.get("responsibility_allocation"):
        errors.append(f"{source_name}: responsibility_allocation must not be empty")
    if not data.get("core_changes"):
        errors.append(f"{source_name}: core_changes must not be empty")
    if not data.get("telemetry_strategy"):
        errors.append(f"{source_name}: telemetry_strategy must not be empty")
    if not data.get("verification_strategy"):
        errors.append(f"{source_name}: verification_strategy must not be empty")
    return errors