from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import difflib

from _yaml import yaml  # type: ignore

from .common import ValidationError, ValidationIssue, is_safe_id, release_uses_intents, release_uses_specs
from .home import BuilderHome
from .init import PlannedWrite
from .parsers import parse_project_manifest, parse_release_manifest


@dataclass(frozen=True)
class MutationPreview:
    home_root: Path
    writes: list[PlannedWrite]


def _relative(home: BuilderHome, path: Path) -> str:
    try:
        return str(path.resolve().relative_to(home.root))
    except ValueError:
        return str(path)


def _render_diff(path: Path, before: str, after: str) -> list[str]:
    return list(
        difflib.unified_diff(
            before.splitlines(),
            after.splitlines(),
            fromfile=str(path),
            tofile=str(path),
            lineterm="",
        )
    )


def render_mutation_preview(home: BuilderHome, preview: MutationPreview) -> str:
    lines = [f"Selected home: {home.root}"]
    for item in preview.writes:
        before = item.path.read_text(encoding="utf-8") if item.path.exists() else ""
        after = item.content or ""
        lines.append(f"write {_relative(home, item.path)}")
        lines.extend(_render_diff(item.path, before, after) or [after.rstrip()])
    return "\n".join(lines) + "\n"


def apply_mutation_preview(preview: MutationPreview) -> None:
    for item in preview.writes:
        item.path.parent.mkdir(parents=True, exist_ok=True)
        item.path.write_text(item.content or "", encoding="utf-8")


def _load_yaml(path: Path) -> dict:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return data if isinstance(data, dict) else {}


def plan_repo_register(*, home: BuilderHome, repo_id: str, repo_path: str) -> MutationPreview:
    if not is_safe_id(repo_id):
        raise ValidationError([ValidationIssue(str(home.root / "repositories.yaml"), f"repo id must match [a-z0-9][a-z0-9-]*: {repo_id!r}")])
    manifest_path = home.root / "repositories.yaml"
    data = _load_yaml(manifest_path)
    repos = list(data.get("repos") or [])
    if any(isinstance(entry, dict) and entry.get("id") == repo_id for entry in repos):
        raise ValidationError([ValidationIssue(str(manifest_path), f"repo id already exists: {repo_id!r}")])
    repos.append({"id": repo_id, "path": repo_path})
    data["repos"] = repos
    return MutationPreview(home.root, [PlannedWrite(manifest_path, yaml.safe_dump(data, sort_keys=False))])


def plan_repo_unregister(*, home: BuilderHome, repo_id: str) -> MutationPreview:
    manifest_path = home.root / "repositories.yaml"
    data = _load_yaml(manifest_path)
    repos = list(data.get("repos") or [])
    filtered = [entry for entry in repos if not (isinstance(entry, dict) and entry.get("id") == repo_id)]
    if len(filtered) == len(repos):
        raise ValidationError([ValidationIssue(str(manifest_path), f"repo id not found: {repo_id!r}")])
    for project in home.projects:
        if repo_id in {repo.repo_id for repo in project.declaration.repos}:
            raise ValidationError([ValidationIssue(str(project.manifest_path), f"repo id {repo_id!r} is still referenced by project {project.id!r}")])
    data["repos"] = filtered
    return MutationPreview(home.root, [PlannedWrite(manifest_path, yaml.safe_dump(data, sort_keys=False))])


def plan_project_edit(
    *,
    home: BuilderHome,
    project_id: str,
    title: str | None = None,
    description: str | None = None,
    default_repo: str | None = None,
    repos: list[tuple[str, str]] | None = None,
) -> MutationPreview:
    project = home.project(project_id)
    if project is None:
        raise ValidationError([ValidationIssue(str(home.root), f"unknown project id {project_id!r}")])
    parse_project_manifest(project.manifest_path)
    data = _load_yaml(project.manifest_path)
    if title is not None:
        data["title"] = title
    if description is not None:
        data["description"] = description
    if default_repo is not None:
        data["default_repo"] = default_repo
    if repos is not None:
        data["repos"] = [{"alias": alias, "repo_id": repo_id} for alias, repo_id in repos]
    return MutationPreview(home.root, [PlannedWrite(project.manifest_path, yaml.safe_dump(data, sort_keys=False))])


def plan_backlog_edit(*, home: BuilderHome, project_id: str, backlog: list[str]) -> MutationPreview:
    project = home.project(project_id)
    if project is None:
        raise ValidationError([ValidationIssue(str(home.root), f"unknown project id {project_id!r}")])
    parse_project_manifest(project.manifest_path)
    data = _load_yaml(project.manifest_path)
    data["backlog"] = list(backlog)
    return MutationPreview(home.root, [PlannedWrite(project.manifest_path, yaml.safe_dump(data, sort_keys=False))])


def plan_release_edit(
    *,
    home: BuilderHome,
    project_id: str,
    release_name: str,
    description: str | None = None,
    specs: list[str] | None = None,
    intents: list[str] | None = None,
    status: str | None = None,
) -> MutationPreview:
    project = home.project(project_id)
    if project is None:
        raise ValidationError([ValidationIssue(str(home.root), f"unknown project id {project_id!r}")])
    release = next((item for item in project.releases if item.declaration.name == release_name), None)
    if release is None:
        raise ValidationError([ValidationIssue(str(project.manifest_path), f"unknown release {release_name!r}")])
    parse_release_manifest(release.manifest_path, project=project.declaration)
    data = _load_yaml(release.manifest_path)
    if description is not None:
        data["description"] = description
    target_status = status or release.declaration.status
    if release_uses_intents(target_status):
        if specs is not None:
            raise ValidationError([
                ValidationIssue(str(release.manifest_path), "live release edits use intents, not specs")
            ])
        if intents is not None:
            data["intents"] = list(intents)
        data.pop("specs", None)
    elif release_uses_specs(target_status):
        historical_specs = specs
        if historical_specs is None and release.declaration.intents:
            historical_specs = [member.spec for member in release.declaration.specs]
        if historical_specs is not None:
            data["specs"] = [{"spec": ref} for ref in historical_specs]
        data.pop("intents", None)
    if status is not None:
        data["status"] = status
    return MutationPreview(home.root, [PlannedWrite(release.manifest_path, yaml.safe_dump(data, sort_keys=False))])
