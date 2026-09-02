from __future__ import annotations

import json
from enum import Enum, StrEnum
from typing import Any


def safe_load(text: str) -> Any:
    try:
        return json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError):
        pass
    lines = text.splitlines()
    return _parse_block(lines, 0)[0]


def safe_dump(data: Any, sort_keys: bool = False, allow_unicode: bool = True, **_: Any) -> str:
    return _dump(data, 0)


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _scalar(value: str) -> Any:
    value = value.strip()
    if value in {"", "null", "~"}:
        return None if value else ""
    if value in {"true", "True"}:
        return True
    if value in {"false", "False"}:
        return False
    if value == "[]":
        return []
    if value == "{}":
        return {}
    if value.startswith("{") and value.endswith("}"):
        inner = value[1:-1].strip()
        if not inner:
            return {}
        mapping: dict[str, Any] = {}
        for entry in _split_flow(inner):
            key, separator, item = entry.partition(":")
            if not separator:
                return value
            normalized_key = _unquote(key.strip())
            if normalized_key in mapping:
                raise ValueError(f"duplicate YAML key {normalized_key!r}")
            mapping[normalized_key] = _scalar(item)
        return mapping
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        return [] if not inner else [_scalar(part.strip()) for part in inner.split(",")]
    if value.startswith(("'", '"')) and value.endswith(("'", '"')):
        return _unquote(value)
    try:
        return int(value)
    except ValueError:
        return value


def _unquote(value: str) -> str:
    if value.startswith(("'", '"')) and value.endswith(("'", '"')):
        return value[1:-1]
    return value


def _split_flow(value: str) -> list[str]:
    """Split a small YAML flow collection without splitting quoted commas.

    Gate-evidence manifests can use a flow mapping for each ``path``/``sha256``
    reference.  Treating that line as a block mapping loses the ``path`` key and
    invents the old ``missing.yaml`` violation.
    """
    parts: list[str] = []
    start = 0
    quote = ""
    depth = 0
    for index, char in enumerate(value):
        if char in {"'", '"'}:
            quote = "" if quote == char else (char if not quote else quote)
        elif not quote and char in "[{":
            depth += 1
        elif not quote and char in "]}":
            depth -= 1
        elif char == "," and not quote and depth == 0:
            parts.append(value[start:index].strip())
            start = index + 1
    parts.append(value[start:].strip())
    return parts


def _parse_block(lines: list[str], start: int, indent: int = 0) -> tuple[Any, int]:
    mapping: dict[str, Any] = {}
    sequence: list[Any] | None = None
    index = start
    while index < len(lines):
        line = lines[index]
        if not line.strip() or line.lstrip().startswith("#"):
            index += 1
            continue
        current = _indent(line)
        if current < indent:
            break
        if current > indent:
            index += 1
            continue
        stripped = line.strip()
        if stripped.startswith("- "):
            if sequence is None:
                sequence = []
            item = stripped[2:]
            if not item:
                child, index = _parse_block(lines, index + 1, indent + 2)
                sequence.append(child)
                continue
            if item.startswith("{") and item.endswith("}"):
                sequence.append(_scalar(item))
                index += 1
                continue
            if ":" in item:
                key, _, value = item.partition(":")
                key = key.strip()
                if value.strip():
                    obj: dict[str, Any] = {key: _scalar(value)}
                    child, new_index = _parse_block(lines, index + 1, indent + 2)
                    if isinstance(child, dict):
                        duplicates = set(obj).intersection(child)
                        if duplicates:
                            raise ValueError(f"duplicate YAML key {sorted(duplicates)[0]!r}")
                        obj.update(child)
                        index = new_index
                    elif isinstance(child, list):
                        index = new_index
                    else:
                        index += 1
                else:
                    child, new_index = _parse_block(lines, index + 1, indent + 4)
                    obj = {key: child if isinstance(child, (dict, list)) else None}
                    if isinstance(child, (dict, list)):
                        index = new_index
                    else:
                        index += 1
                sequence.append(obj)
                continue
            sequence.append(_scalar(item))
            index += 1
            continue
        if ":" in stripped:
            key, _, value = stripped.partition(":")
            normalized_key = key.strip()
            if normalized_key in mapping:
                raise ValueError(f"duplicate YAML key {normalized_key!r}")
            if value.strip():
                mapping[normalized_key] = _scalar(value)
                index += 1
            else:
                child, index = _parse_block(lines, index + 1, indent + 2)
                mapping[normalized_key] = child
            continue
        index += 1
    return (sequence if sequence is not None else mapping), index


def _format(value: Any) -> str:
    # Match the real PyYAML integration: StrEnum is a string-valued protocol
    # field, but serializing a plain Enum (including IntEnum) invents a
    # misleading scalar such as ``State.READY`` or ``1``.
    if isinstance(value, Enum):
        if isinstance(value, StrEnum):
            return str(value)
        raise TypeError(f"cannot serialize non-StrEnum enum: {value!r}")
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, str):
        return value
    return str(value)


def _dump(data: Any, indent: int) -> str:
    prefix = " " * indent
    if isinstance(data, dict):
        lines: list[str] = []
        for key, value in data.items():
            if isinstance(value, (dict, list)):
                if value == []:
                    lines.append(f"{prefix}{key}: []")
                else:
                    lines.append(f"{prefix}{key}:")
                    lines.append(_dump(value, indent + 2).rstrip())
            else:
                lines.append(f"{prefix}{key}: {_format(value)}")
        return "\n".join(lines) + "\n"
    if isinstance(data, list):
        if not data:
            return f"{prefix}[]\n"
        lines = []
        for item in data:
            if isinstance(item, dict):
                entries = list(item.items())
                if not entries:
                    lines.append(f"{prefix}- {{}}")
                    continue
                first_key, first_value = entries[0]
                if isinstance(first_value, (dict, list)):
                    lines.append(f"{prefix}- {first_key}:")
                    lines.append(_dump(first_value, indent + 4).rstrip())
                else:
                    lines.append(f"{prefix}- {first_key}: {_format(first_value)}")
                for key, value in entries[1:]:
                    if isinstance(value, (dict, list)):
                        if value == []:
                            lines.append(f"{' ' * (indent + 2)}{key}: []")
                        else:
                            lines.append(f"{' ' * (indent + 2)}{key}:")
                            lines.append(_dump(value, indent + 4).rstrip())
                    else:
                        lines.append(f"{' ' * (indent + 2)}{key}: {_format(value)}")
            else:
                lines.append(f"{prefix}- {_format(item)}")
        return "\n".join(lines) + "\n"
    return f"{prefix}{_format(data)}\n"
