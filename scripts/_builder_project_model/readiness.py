from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import readiness as readiness_ladder
from _yaml import yaml  # type: ignore

from .home import BuilderHome


@dataclass(frozen=True)
class ReadinessBlock:
    repo_id: str
    spec_id: str
    ref: str
    required: str
    observation: str
    external: bool


def _safe_load(path: Path):
    if not path.exists() or path.is_symlink():
        return None
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return data if isinstance(data, dict) else None


def _repo_id_for_alias(home: BuilderHome, repo_root: Path, alias: str) -> str | None:
    project = home.default_project_for_repo(repo_root)
    if project is None:
        return None
    for repo in project.declaration.repos:
        if repo.alias == alias:
            return repo.repo_id
    return None


def evaluate_cross_repo_dependencies(
    *,
    home: BuilderHome,
    repo_id: str,
    repo_root: Path,
    spec_id: str,
    git_runner=None,
    registry_query: Callable | None = None,
) -> list[ReadinessBlock]:
    deps_path = repo_root / ".builder" / "specs" / spec_id / "dependencies.yaml"
    if not deps_path.exists():
        deps_path = repo_root / ".builder" / "specs" / spec_id / "dependencies.yaml"
    data = _safe_load(deps_path)
    if not isinstance(data, dict):
        return []
    deps = data.get("dependencies")
    if not isinstance(deps, list):
        return []
    blocks: list[ReadinessBlock] = []
    for dep in deps:
        if not isinstance(dep, dict):
            continue
        target = str(dep.get("spec", "")).strip()
        if "/" not in target or "\\" in target:
            continue
        alias, upstream_spec_id = target.split("/", 1)
        upstream_repo_id = _repo_id_for_alias(home, repo_root, alias)
        if upstream_repo_id is None or upstream_repo_id == repo_id:
            continue
        upstream_repo_root = home.repo_roots_by_id.get(upstream_repo_id)
        if upstream_repo_root is None:
            blocks.append(
                ReadinessBlock(
                    repo_id=upstream_repo_id or alias,
                    spec_id=upstream_spec_id,
                    ref=target,
                    required=str(dep.get("ready_at", "merged")).strip().lower() or "merged",
                    observation="unresolved: unknown upstream repo",
                    external=True,
                )
            )
            continue
        required = str(dep.get("ready_at", "merged")).strip().lower() or "merged"
        package = dep.get("package") if isinstance(dep.get("package"), dict) else None
        spec_dir = upstream_repo_root / ".builder" / "specs" / upstream_spec_id
        if not spec_dir.is_dir():
            spec_dir = upstream_repo_root / ".builder" / "specs" / upstream_spec_id
        result = readiness_ladder.evaluate(
            target,
            spec_dir,
            upstream_repo_root,
            required=required,
            package=package,
            safe_load=_safe_load,
            git_runner=git_runner,
            registry_query=registry_query,
        )
        if not result.satisfies(required):
            blocks.append(
                ReadinessBlock(
                    repo_id=upstream_repo_id,
                    spec_id=upstream_spec_id,
                    ref=target,
                    required=required,
                    observation=result.observation,
                    external=True,
                )
            )
    return blocks
