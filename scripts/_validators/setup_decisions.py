from __future__ import annotations

from typing import Any

from .canonical import validate_canonical_artifact
from .common import ValidationContext, string_list


def run(context: ValidationContext):
    return validate_canonical_artifact(
        context,
        artifact_name="setup-decisions",
        source_file="setup-decisions.yaml",
        schema_file="setup-decisions.schema.yaml",
        render=None,
        rendered_file=None,
        extra_validation=validate_setup_decisions,
    )


def validate_setup_decisions(data: dict[str, Any], source_name: str) -> list[str]:
    errors: list[str] = []
    workspace = data.get("workspace") if isinstance(data.get("workspace"), dict) else {}
    commands = data.get("commands") if isinstance(data.get("commands"), dict) else {}
    default_commands = commands.get("default") if isinstance(commands.get("default"), dict) else {}

    if not string_list(workspace.get("roots")):
        errors.append(f"{source_name}: workspace.roots must not be empty")
    if not default_commands:
        errors.append(f"{source_name}: commands.default must be present")
    if not isinstance(default_commands.get("test"), str) or not default_commands.get("test", "").strip():
        errors.append(f"{source_name}: commands.default.test must be present")
    if not isinstance(default_commands.get("check"), str) or not default_commands.get("check", "").strip():
        errors.append(f"{source_name}: commands.default.check must be present")
    return errors