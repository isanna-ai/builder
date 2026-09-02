from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from _yaml import yaml  # type: ignore

ID_REPR = "[a-z0-9][a-z0-9-]*"
CANONICAL_PROVIDERS = ("claude-code-cli", "codex-cli")
LIVE_RELEASE_STATUSES = ("draft", "active")
HISTORICAL_RELEASE_STATUSES = ("shipped", "cancelled", "abandoned", "archived")
RELEASE_STATUSES = LIVE_RELEASE_STATUSES + HISTORICAL_RELEASE_STATUSES


def release_membership_field(status: str) -> str | None:
    normalized = str(status or "").strip().lower()
    if normalized in LIVE_RELEASE_STATUSES:
        return "intents"
    if normalized in HISTORICAL_RELEASE_STATUSES:
        return "specs"
    return None


def release_uses_intents(status: str) -> bool:
    return release_membership_field(status) == "intents"


def release_uses_specs(status: str) -> bool:
    return release_membership_field(status) == "specs"


@dataclass(frozen=True)
class ValidationIssue:
    location: str
    message: str

    def render(self) -> str:
        return f"{self.location}: {self.message}"


class ValidationError(ValueError):
    def __init__(self, issues: list[ValidationIssue]):
        self.issues = issues
        super().__init__("; ".join(issue.render() for issue in issues))


def load_yaml_mapping(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValidationError([ValidationIssue(str(path), "expected a mapping")])
    return data


def reject_unknown_keys(data: dict[str, Any], allowed: set[str], *, location: str) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    for key in sorted(data):
        if key not in allowed:
            issues.append(ValidationIssue(location, f"unknown key {key!r}"))
    return issues


def require_schema_version(data: dict[str, Any], *, location: str) -> list[ValidationIssue]:
    version = data.get("schema_version")
    if version != 1:
        return [ValidationIssue(location, f"unsupported schema_version {version!r} (expected 1)")]
    return []


def as_clean_str(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def ensure_relative_contained(
    raw: Any,
    *,
    base_dir: Path,
    container_dir: Path,
    location: str,
    allow_absolute: bool = False,
) -> tuple[Path | None, list[ValidationIssue]]:
    issues: list[ValidationIssue] = []
    if not isinstance(raw, str) or not raw.strip():
        return None, [ValidationIssue(location, "path must be a non-empty string")]
    candidate = Path(raw.strip())
    if candidate.is_absolute():
        if not allow_absolute:
            return None, [ValidationIssue(location, "path must be relative")]
        resolved = candidate.resolve()
    else:
        resolved = (base_dir / candidate).resolve()
    try:
        resolved.relative_to(container_dir.resolve())
    except ValueError:
        issues.append(ValidationIssue(location, f"path escapes {container_dir}"))
        return None, issues
    return resolved, issues


def is_safe_id(value: str) -> bool:
    if not value:
        return False
    if value[0] < "a" or value[0] > "z":
        if value[0] < "0" or value[0] > "9":
            return False
    for char in value:
        if char == "-":
            continue
        if "a" <= char <= "z":
            continue
        if "0" <= char <= "9":
            continue
        return False
    return True
