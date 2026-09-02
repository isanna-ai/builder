from __future__ import annotations

from typing import Any, Callable, Optional

from .common import CheckResult, ValidationContext, compare_rendered_view, load_schema, parse_yaml_like_file, resolve_artifact_mode, validate_schema


def validate_canonical_artifact(
    context: ValidationContext,
    artifact_name: str,
    source_file: str,
    schema_file: str,
    render: Optional[Callable[[dict[str, Any]], str]] = None,
    rendered_file: Optional[str] = None,
    extra_validation: Optional[Callable[[dict[str, Any], str], list[str]]] = None,
) -> CheckResult:
    source_path = context.spec_dir / source_file
    if not source_path.exists():
        return CheckResult(
            display_name=source_file,
            errors=[],
            skipped=True,
            skip_message=f"{source_file} not found at {source_path}",
        )

    data, errors = parse_yaml_like_file(source_path)
    if errors:
        return CheckResult(display_name=source_file, errors=errors)

    schema, schema_errors = load_schema(schema_file)
    errors.extend(schema_errors)
    if schema:
        errors.extend(validate_schema(data, schema, source_file))
    if extra_validation:
        errors.extend(extra_validation(data, source_file))
    if render and rendered_file:
        rendered_text = render(data)
        rendered_path = context.spec_dir / rendered_file
        artifact_mode = resolve_artifact_mode(context)
        if artifact_mode != "ai_native" or rendered_path.exists():
            errors.extend(compare_rendered_view(source_file, rendered_file, rendered_text, rendered_path))

    summary = None if errors else f"{source_file}: valid"
    return CheckResult(display_name=source_file, errors=errors, summary=summary)
