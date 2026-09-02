from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from .runtime import runtime_dir


@dataclass
class ValidationContext:
    spec_dir: Path
    strict: bool = False
    contract_path: Optional[Path] = None


@dataclass
class CheckResult:
    display_name: str
    errors: list[str]
    total_checks: int = 1
    summary: Optional[str] = None
    skipped: bool = False
    skip_message: Optional[str] = None


VALID_TDD_EXEMPT_REASONS = {
    "refactor-only",
    "delete-only",
    "type-only",
    "config-only",
    "infrastructure-only",
}

VALID_ARTIFACT_MODES = {"dual", "ai_native"}


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def normalize_rendered_text(text: str) -> str:
    return text.rstrip("\n") + "\n"


def read_text(path: Path) -> tuple[Optional[str], list[str]]:
    try:
        return path.read_text(encoding="utf-8"), []
    except OSError as exc:
        return None, [f"{path.name}: cannot read ({exc})"]


def parse_yaml_like_file(path: Path) -> tuple[dict[str, Any], list[str]]:
    text, errors = read_text(path)
    if errors:
        return {}, errors

    assert text is not None
    try:
        from _yaml import yaml  # type: ignore

        data = yaml.safe_load(text) or {}
        if not isinstance(data, dict):
            return {}, [f"{path.name}: top-level document must be a mapping"]
        return data, []
    except ImportError:
        data = _parse_entry(text.splitlines())
        if not isinstance(data, dict):
            return {}, [f"{path.name}: top-level document must be a mapping"]
        return data, []


def resolve_artifact_mode(context: ValidationContext) -> str:
    spec_data, spec_errors = parse_yaml_like_file(context.spec_dir / "spec.yaml")
    if not spec_errors:
        mode = str(spec_data.get("artifact_mode", "")).strip()
        if mode in VALID_ARTIFACT_MODES:
            return mode

    setup_candidates = [context.spec_dir / "setup-decisions.yaml"]
    if context.spec_dir.parent.name == "specs":
        setup_candidates.append(context.spec_dir.parent.parent / "setup-decisions.yaml")

    for setup_path in setup_candidates:
        if not setup_path.exists():
            continue
        setup_data, setup_errors = parse_yaml_like_file(setup_path)
        if setup_errors:
            continue
        for key in ("default_artifact_mode", "artifact_mode"):
            mode = str(setup_data.get(key, "")).strip()
            if mode in VALID_ARTIFACT_MODES:
                return mode
        builder = setup_data.get("builder") if isinstance(setup_data.get("builder"), dict) else {}
        mode = str(builder.get("artifact_mode", "")).strip()
        if mode in VALID_ARTIFACT_MODES:
            return mode

    return "dual"


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _parse_scalar(raw: str) -> Any:
    stripped = raw.strip()
    if stripped in {"", "null", "~"}:
        return "" if stripped == "" else None
    if stripped in {"true", "True"}:
        return True
    if stripped in {"false", "False"}:
        return False
    if stripped.startswith("[") and stripped.endswith("]"):
        inner = stripped[1:-1].strip()
        if not inner:
            return []
        return [_parse_scalar(part) for part in _split_inline_list(inner)]
    if stripped.startswith(("'", '"')) and stripped.endswith(("'", '"')):
        return stripped[1:-1]
    if re.fullmatch(r"-?[0-9]+", stripped):
        return int(stripped)
    return stripped


def _split_inline_list(inner: str) -> list[str]:
    parts: list[str] = []
    current: list[str] = []
    quote: Optional[str] = None
    depth = 0
    for char in inner:
        if quote:
            current.append(char)
            if char == quote:
                quote = None
            continue
        if char in {"'", '"'}:
            quote = char
            current.append(char)
            continue
        if char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
        if char == "," and depth == 0:
            parts.append("".join(current).strip())
            current = []
            continue
        current.append(char)
    if current:
        parts.append("".join(current).strip())
    return [part for part in parts if part]


def _parse_entry(lines: list[str]) -> Any:
    result: dict[str, Any] = {}
    index = 0
    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            index += 1
            continue
        indent = _indent(line)
        if indent == 0 and ":" in line:
            key, _, value = line.partition(":")
            key = key.strip()
            value = value.strip()
            if value:
                result[key] = _parse_scalar(value)
                index += 1
                continue

            nested_raw: list[str] = []
            index += 1
            while index < len(lines):
                nested = lines[index]
                if not nested.strip():
                    index += 1
                    continue
                if _indent(nested) > 0:
                    nested_raw.append(nested)
                    index += 1
                    continue
                break

            if not nested_raw:
                result[key] = {}
                continue

            min_indent = min(_indent(item) for item in nested_raw if item.strip())
            nested_lines = [item[min_indent:] for item in nested_raw]

            if nested_lines[0].startswith("- "):
                items: list[Any] = []
                cursor = 0
                while cursor < len(nested_lines):
                    item_line = nested_lines[cursor]
                    if not item_line.startswith("- "):
                        cursor += 1
                        continue
                    head = item_line[2:]
                    cursor += 1
                    child_lines: list[str] = []
                    while cursor < len(nested_lines) and not nested_lines[cursor].startswith("- "):
                        raw = nested_lines[cursor]
                        child_lines.append(raw[2:] if len(raw) >= 2 else raw)
                        cursor += 1
                    if ":" in head:
                        items.append(_parse_entry([head, *child_lines]))
                    else:
                        items.append(_parse_scalar(head))
                result[key] = items
            else:
                result[key] = _parse_entry(nested_lines)
            continue
        index += 1
    return result


def string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped or stripped == "[]":
            return []
        if stripped.startswith("[") and stripped.endswith("]"):
            return [str(item).strip() for item in _parse_scalar(stripped) if str(item).strip()]
        return [stripped]
    return [str(value).strip()]


def mapping_list(path_name: str, block_name: str, value: Any, errs: list[str]) -> list[dict[str, Any]]:
    if isinstance(value, list):
        items = value
    elif value in (None, "[]"):
        items = []
    else:
        errs.append(f"{path_name}: `{block_name}` must be a list")
        return []

    result: list[dict[str, Any]] = []
    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            errs.append(f"{path_name}: `{block_name}[{index}]` must be a mapping")
            continue
        result.append(item)
    return result


def load_schema(schema_file: str) -> tuple[dict[str, Any], list[str]]:
    for root in (repo_root() / "schemas", runtime_dir(repo_root()) / "schemas"):
        candidate = root / schema_file
        if candidate.is_file():
            return parse_yaml_like_file(candidate)
    return {}, [f"{schema_file}: schema file not found"]


def validate_schema(data: Any, schema: dict[str, Any], location: str) -> list[str]:
    errors: list[str] = []
    _validate_schema_node(data, schema, location, errors)
    return errors


def _validate_schema_node(data: Any, schema: dict[str, Any], location: str, errors: list[str]) -> None:
    schema_type = schema.get("type")
    if schema_type:
        allowed_types = [str(item) for item in schema_type] if isinstance(schema_type, list) else [str(schema_type)]
        if not any(_matches_type(data, item) for item in allowed_types):
            errors.append(f"{location}: expected {schema_type}")
            return

    # For string-typed fields, compare const/enum as strings. YAML 1.1 implicit
    # typing (real PyYAML 6.x) reparses unquoted schema literals like `off`/`on`
    # into bools and ISO timestamps into datetimes; coercing both sides to str
    # keeps a legitimate string value (e.g. memory_mode "off") matching.
    is_string_field = schema_type == "string"

    if "const" in schema:
        const_value = schema["const"]
        if is_string_field:
            if str(data) != str(const_value):
                errors.append(f"{location}: expected constant {const_value!r}")
        elif data != const_value:
            errors.append(f"{location}: expected constant {const_value!r}")

    enum_values = schema.get("enum")
    if isinstance(enum_values, list):
        if is_string_field:
            allowed = {str(item) for item in enum_values}
            if str(data) not in allowed:
                errors.append(f"{location}: expected one of {enum_values}")
        elif data not in enum_values:
            errors.append(f"{location}: expected one of {enum_values}")

    if schema_type == "string":
        pattern = schema.get("pattern")
        if isinstance(pattern, str) and not re.fullmatch(pattern, str(data)):
            errors.append(f"{location}: does not match pattern {pattern!r}")
        min_length = schema.get("minLength")
        if isinstance(min_length, int) and len(str(data)) < min_length:
            errors.append(f"{location}: must have length >= {min_length}")

    if schema_type in ("integer", "number"):
        # Enforce the declared numeric bounds. Coerce a string-encoded integer (YAML
        # 1.1 may quote it) to its numeric value first; on a non-numeric value the
        # `type` check above has already recorded the error, so skip the bound test.
        numeric: float | None
        if isinstance(data, bool):
            numeric = None
        elif isinstance(data, (int, float)):
            numeric = data
        elif isinstance(data, str) and re.fullmatch(r"-?[0-9]+", data):
            numeric = int(data)
        else:
            numeric = None
        if numeric is not None:
            minimum = schema.get("minimum")
            if isinstance(minimum, (int, float)) and numeric < minimum:
                errors.append(f"{location}: must be >= {minimum}")
            maximum = schema.get("maximum")
            if isinstance(maximum, (int, float)) and numeric > maximum:
                errors.append(f"{location}: must be <= {maximum}")

    if schema_type == "object" and isinstance(data, dict):
        required = string_list(schema.get("required"))
        properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
        additional_properties = schema.get("additionalProperties", True)
        for field_name in required:
            if field_name not in data:
                errors.append(f"{location}: missing required field `{field_name}`")
        for field_name, field_value in data.items():
            if field_name in properties:
                field_schema = properties[field_name]
                if isinstance(field_schema, dict):
                    _validate_schema_node(field_value, field_schema, f"{location}.{field_name}", errors)
            elif additional_properties is False:
                errors.append(f"{location}: unknown field `{field_name}`")

    if schema_type == "array" and isinstance(data, list):
        min_items = schema.get("minItems")
        if isinstance(min_items, int) and len(data) < min_items:
            errors.append(f"{location}: must have at least {min_items} item(s)")
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(data, start=1):
                _validate_schema_node(item, item_schema, f"{location}[{index}]", errors)


def _matches_type(value: Any, schema_type: str) -> bool:
    if schema_type == "object":
        return isinstance(value, dict)
    if schema_type == "array":
        return isinstance(value, list)
    if schema_type == "string":
        return isinstance(value, str)
    if schema_type == "integer":
        return isinstance(value, int) or (isinstance(value, str) and re.fullmatch(r"-?[0-9]+", value) is not None)
    if schema_type == "number":
        return isinstance(value, (int, float))
    if schema_type == "boolean":
        return isinstance(value, bool)
    if schema_type == "null":
        return value is None
    return True


def compare_rendered_view(source_name: str, rendered_name: str, rendered_text: str, rendered_path: Path) -> list[str]:
    text, errors = read_text(rendered_path)
    if errors:
        return [f"{source_name}: rendered view `{rendered_name}` is missing"]
    assert text is not None
    if normalize_rendered_text(text) != normalize_rendered_text(rendered_text):
        return [f"{source_name}: rendered view drift detected for {rendered_name}"]
    return []
