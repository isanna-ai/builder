from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .attribution import receipt_for_work
from .common import ValidationError, ValidationIssue
from .home import BuilderHome


@dataclass(frozen=True)
class DispatchPlanAction:
    roadmap_index: int
    repo_id: str
    repo_root: Path
    alias: str | None
    spec_id: str
    ref: str
    admitted: bool


def build_dispatch_plan(*, home: BuilderHome, project_id: str, release_name: str) -> list[DispatchPlanAction]:
    project = home.project(project_id)
    if project is None:
        raise ValidationError([ValidationIssue(str(home.root), f"unknown project id {project_id!r}")])
    release = next((item for item in project.releases if item.declaration.name == release_name), None)
    if release is None:
        raise ValidationError([ValidationIssue(str(project.manifest_path), f"unknown release {release_name!r}")])
    actions: list[DispatchPlanAction] = []
    for index, member in enumerate(release.declaration.specs):
        ref = member.spec
        if "/" in ref:
            alias, spec_id = ref.split("/", 1)
            repo_id = next((entry.repo_id for entry in project.declaration.repos if entry.alias == alias), None)
        else:
            alias = None
            spec_id = ref
            repo_id = project.declaration.default_repo
        if repo_id is None:
            raise ValidationError([ValidationIssue(str(release.manifest_path), f"cannot resolve repo for ref {ref!r}")])
        repo_root = home.repo_roots_by_id.get(repo_id)
        if repo_root is None:
            raise ValidationError([ValidationIssue(str(release.manifest_path), f"unknown repo id {repo_id!r} for ref {ref!r}")])
        actions.append(
            DispatchPlanAction(
                roadmap_index=index,
                repo_id=repo_id,
                repo_root=repo_root,
                alias=alias,
                spec_id=spec_id,
                ref=ref,
                admitted=receipt_for_work(home.root, repo_id=repo_id, spec_id=spec_id) is not None,
            )
        )
    return actions


def render_dispatch_plan(*, home: BuilderHome, project_id: str, release_name: str, actions: list[DispatchPlanAction]) -> str:
    lines = [f"Selected home: {home.root}", f"Dispatch plan: {project_id}/{release_name}"]
    for action in actions:
        state = "admitted" if action.admitted else "pending"
        lines.append(
            f"[{action.roadmap_index:02d}] repo={action.repo_id} alias={action.alias or '-'} "
            f"spec={action.spec_id} ref={action.ref} state={state}"
        )
    return "\n".join(lines) + "\n"
