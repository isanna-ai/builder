from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import difflib

from .home import BuilderHome, load_optional_home
from .editors import MutationPreview


@dataclass(frozen=True)
class AuthoringContext:
    home_root: Path
    repo_root: Path
    project_id: str
    project_title: str
    project_description: str
    backlog: tuple[str, ...]
    aliases: tuple[str, ...]
    release_names: tuple[str, ...]


def load_authoring_context(*, repo_root: Path, home: BuilderHome | None = None) -> AuthoringContext | None:
    resolved_home = home or load_optional_home(start=repo_root)
    if resolved_home is None:
        return None
    project = resolved_home.default_project_for_repo(repo_root.resolve())
    if project is None:
        return None
    return AuthoringContext(
        home_root=resolved_home.root,
        repo_root=repo_root.resolve(),
        project_id=project.id,
        project_title=project.declaration.title,
        project_description=project.declaration.description,
        backlog=tuple(project.declaration.backlog),
        aliases=tuple(entry.alias for entry in project.declaration.repos),
        release_names=tuple(release.declaration.name for release in project.releases),
    )


def emit_declaration_patch_handoff(*, preview: MutationPreview) -> str:
    lines = ["Declaration patch handoff", f"Selected home: {preview.home_root}", ""]
    for item in preview.writes:
        before = item.path.read_text(encoding="utf-8") if item.path.exists() else ""
        after = item.content or ""
        diff = "\n".join(
            difflib.unified_diff(
                before.splitlines(),
                after.splitlines(),
                fromfile=str(item.path),
                tofile=str(item.path),
                lineterm="",
            )
        )
        lines.append(diff or f"(no textual diff for {item.path})")
    return "\n".join(line for line in lines if line) + "\n"
