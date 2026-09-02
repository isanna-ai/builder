from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from _yaml import yaml  # type: ignore

from .common import ValidationError, ValidationIssue, is_safe_id


@dataclass(frozen=True)
class PlannedWrite:
    path: Path
    content: str | None


def _default_home_id(projects_root: Path) -> str:
    token = projects_root.name.strip().lower().replace("_", "-").replace(" ", "-")
    token = "".join(ch for ch in token if ch.isascii() and (ch.isalnum() or ch == "-")).strip("-")
    return token or "builder-home"


def scaffold_home(
    *,
    projects_root: Path,
    home_id: str | None = None,
) -> list[PlannedWrite]:
    root = projects_root.resolve()
    chosen_home_id = (home_id or _default_home_id(root)).strip().lower()
    if not is_safe_id(chosen_home_id):
        raise ValidationError([ValidationIssue(str(root), f"home_id must match [a-z0-9][a-z0-9-]*: {chosen_home_id!r}")])
    home_dir = root / ".builder-home"
    builder = {
        "schema_version": 1,
        "home_id": chosen_home_id,
        "repositories": "repositories.yaml",
        "policy": "policy.yaml",
        "projects": [],
    }
    repositories = {"schema_version": 1, "repos": []}
    policy = {
        "schema_version": 1,
        "governor": {"enabled": False},
        "providers": {
            "claude-code-cli": {"max_sessions": 2, "quota_cooldown": {"initial_seconds": 300, "max_seconds": 3600}},
            "codex-cli": {"max_sessions": 3, "quota_cooldown": {"initial_seconds": 300, "max_seconds": 3600}},
        },
        "allocation": {"policy": "equal-weight-fair-share", "project_weight": 1},
        "scheduler": {"poll_seconds": 2, "heartbeat_seconds": 5, "stale_daemon_seconds": 30},
    }
    return [
        PlannedWrite(home_dir / "builder.yaml", yaml.safe_dump(builder, sort_keys=False)),
        PlannedWrite(home_dir / "repositories.yaml", yaml.safe_dump(repositories, sort_keys=False)),
        PlannedWrite(home_dir / "policy.yaml", yaml.safe_dump(policy, sort_keys=False)),
        PlannedWrite(home_dir / "projects", None),
    ]


def render_plan(projects_root: Path, writes: list[PlannedWrite]) -> str:
    lines = [f"Selected projects root: {projects_root.resolve()}"]
    for item in writes:
        rel = item.path
        if item.content is None:
            lines.append(f"mkdir {rel}")
        else:
            lines.append(f"write {rel}")
            lines.append(item.content.rstrip())
    return "\n".join(lines) + "\n"


def apply_plan(writes: list[PlannedWrite]) -> None:
    for item in writes:
        if item.content is None:
            item.path.mkdir(parents=True, exist_ok=True)
            continue
        item.path.parent.mkdir(parents=True, exist_ok=True)
        if item.path.exists():
            raise ValidationError([ValidationIssue(str(item.path), "refusing to overwrite existing file")])
        item.path.write_text(item.content, encoding="utf-8")
