from __future__ import annotations

from pathlib import Path
from typing import Any

from _validators.common import parse_yaml_like_file
from _dispatch_runtime.paths import runtime_dir


CONSTITUTION_CANDIDATES = ("constitution.yaml", "constitution.md")


def discover(project_root: Path) -> list[Path]:
    found: list[Path] = []
    for relative in CONSTITUTION_CANDIDATES:
        candidate = runtime_dir(project_root) / relative
        if candidate.is_file():
            found.append(candidate)
    candidate = project_root / "CONSTITUTION.md"
    if candidate.is_file():
        found.append(candidate)
    return found


def load_machine_constitution(path: Path) -> tuple[dict[str, Any], list[str]]:
    data, errors = parse_yaml_like_file(path)
    if errors:
        return {}, errors
    if data.get("artifact") != "constitution":
        return {}, [f"{path}: expected artifact `constitution`"]
    return data, []
