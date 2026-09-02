from __future__ import annotations

from typing import Any

from .canonical import validate_canonical_artifact
from .common import ValidationContext
from .renderers import render_handoff


def run(context: ValidationContext):
    return validate_canonical_artifact(
        context,
        artifact_name="handoff",
        source_file="handoff.yaml",
        schema_file="handoff.schema.yaml",
        render=None,
        rendered_file=None,
        extra_validation=validate_handoff,
    )


def validate_handoff(data: dict[str, Any], source_name: str) -> list[str]:
    errors: list[str] = []
    for field_name in ("phase", "next_phase", "next_command", "used_model", "model_advice"):
        if not str(data.get(field_name, "")).strip():
            errors.append(f"{source_name}: missing required handoff field `{field_name}`")
    return errors