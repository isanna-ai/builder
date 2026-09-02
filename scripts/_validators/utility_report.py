from __future__ import annotations

from .common import CheckResult, ValidationContext, load_schema, parse_yaml_like_file, validate_schema


def run(context: ValidationContext):
    report_paths = sorted(context.spec_dir.glob("*-report.yaml"))
    if not report_paths:
        return CheckResult(
            display_name="utility-report",
            errors=[],
            skipped=True,
            skip_message=f"no utility report files found at {context.spec_dir}",
        )

    schema, schema_errors = load_schema("utility-report.schema.yaml")
    errors = list(schema_errors)
    validated: list[str] = []
    total_checks = 0

    for report_path in report_paths:
        total_checks += 1
        data, parse_errors = parse_yaml_like_file(report_path)
        errors.extend(parse_errors)
        if parse_errors:
            continue
        if schema:
            errors.extend(validate_schema(data, schema, report_path.name))
        validated.append(report_path.name)

    summary = None if errors else f"utility reports valid: {', '.join(validated)}"
    return CheckResult(
        display_name="utility-report",
        errors=errors,
        total_checks=total_checks,
        summary=summary,
    )