from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from _dispatch_runtime.paths import runtime_dir
from _yaml import yaml  # type: ignore

from .common import ValidationError, ValidationIssue, is_safe_id
from .home import BuilderHome
from .init import PlannedWrite


@dataclass(frozen=True)
class ImportPreview:
    subject: str
    source_root: Path
    writes: list[PlannedWrite]


def _relative_from(home: BuilderHome, path: Path) -> str:
    try:
        return str(path.resolve().relative_to(home.root))
    except ValueError:
        return str(path)


def _intent_doc(intent_id: str, title: str, specs: list[str]) -> dict[str, object]:
    return {
        "artifact": "intent-object",
        "intent": intent_id,
        "title": title,
        "status": "accepted",
        "problem": f"Import legacy release {title} into canonical intent membership.",
        "why": "Builder Home draft and active releases require non-empty intents.",
        "success_criteria": [
            {"id": "SC-1", "statement": "The imported release preserves its exact authored membership once."}
        ],
        "non_goals": ["Change the imported release scope."],
        "ssot_delta": {"capabilities": [], "behaviors": [], "journeys": []},
        "specs": specs,
    }


def _legacy_spec_refs(release_path: Path, planning_module) -> list[str]:
    data = yaml.safe_load(release_path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        return []
    refs: list[str] = []
    for raw in data.get("specs", []) if isinstance(data.get("specs"), list) else []:
        ref, err = planning_module.parse_spec_ref(raw)
        if err or ref is None:
            continue
        refs.append(ref.canonical)
    return refs


def preview_bia_import(*, home: BuilderHome, source_root: Path) -> ImportPreview:
    subject = "bia"
    root = source_root.resolve()
    product_path = runtime_dir(root) / "product.yaml"
    if not product_path.is_file():
        raise ValidationError([ValidationIssue(str(product_path), "legacy product.yaml not found")])
    import planning  # local import avoids a top-level cycle

    product = planning.parse_product(product_path)
    if product.product != subject:
        raise ValidationError([ValidationIssue(str(product_path), f"only {subject!r} is supported; found {product.product!r}")])
    repo_id = root.name.strip().lower()
    if not is_safe_id(repo_id):
        raise ValidationError([ValidationIssue(str(root), f"source repo basename is not a safe repo id: {repo_id!r}")])

    writes: list[PlannedWrite] = []
    builder_path = home.root / "builder.yaml"
    builder_data = yaml.safe_load(builder_path.read_text(encoding="utf-8")) or {}
    projects = list(builder_data.get("projects", []))
    if not any(isinstance(entry, dict) and entry.get("id") == subject for entry in projects):
        projects.append({"id": subject, "manifest": f"projects/{subject}/product.yaml"})
        builder_data["projects"] = projects
        writes.append(PlannedWrite(builder_path, yaml.safe_dump(builder_data, sort_keys=False)))

    mirror_root = home.root / "imported-repos" / repo_id

    repos_path = home.root / "repositories.yaml"
    repos_data = yaml.safe_load(repos_path.read_text(encoding="utf-8")) or {}
    repos = list(repos_data.get("repos", []))
    if not any(isinstance(entry, dict) and entry.get("id") == repo_id for entry in repos):
        repos.append({"id": repo_id, "path": str(Path("imported-repos") / repo_id)})
    for alias in product.repo_aliases:
        alias_root = home.root.parent / alias
        if alias_root == root or not alias_root.is_dir():
            continue
        if any(isinstance(entry, dict) and entry.get("id") == alias for entry in repos):
            continue
        repos.append({"id": alias, "path": str(Path("..") / alias)})
    repos_data["repos"] = repos
    writes.append(PlannedWrite(repos_path, yaml.safe_dump(repos_data, sort_keys=False)))
    writes.append(PlannedWrite(mirror_root / ".git" / "keep", ""))

    releases = planning.load_releases(root)
    product_doc = {
        "schema_version": 1,
        "product": product.product,
        "title": product.title or product.product,
        "description": "",
        "default_repo": repo_id,
        "repos": [{"alias": alias, "repo_id": repo_id if alias == repo_id else alias} for alias in product.repo_aliases],
        "backlog": [],
        "releases": [
            {"name": release.release_id, "manifest": f"releases/{release.release_id}.yaml"}
            for release in releases
        ],
    }
    writes.append(PlannedWrite(home.root / "projects" / subject / "product.yaml", yaml.safe_dump(product_doc, sort_keys=False)))
    for release in releases:
        legacy_specs = _legacy_spec_refs(runtime_dir(root) / "releases" / f"{release.release_id}.yaml", planning)
        if release.status in {"draft", "active"}:
            intent_id = release.release_id
            release_doc = {
                "schema_version": 1,
                "name": release.release_id,
                "description": release.goal,
                "status": "cancelled" if release.status == "abandoned" else release.status,
                "intents": [intent_id],
            }
            writes.append(
                PlannedWrite(
                    home.root / "projects" / subject / "releases" / f"{release.release_id}.yaml",
                    yaml.safe_dump(release_doc, sort_keys=False),
                )
            )
            writes.append(
                PlannedWrite(
                    mirror_root / ".builder" / "intents" / intent_id / "intent.yaml",
                    yaml.safe_dump(
                        _intent_doc(intent_id, release.title or release.release_id, legacy_specs),
                        sort_keys=False,
                    ),
                )
            )
        else:
            release_doc = {
                "schema_version": 1,
                "name": release.release_id,
                "description": release.goal,
                "status": "cancelled" if release.status == "abandoned" else release.status,
                "specs": [{"spec": ref.canonical, "weight": ref.weight} for ref in release.specs],
            }
            writes.append(
                PlannedWrite(
                    home.root / "projects" / subject / "releases" / f"{release.release_id}.yaml",
                    yaml.safe_dump(release_doc, sort_keys=False),
                )
            )
        for canonical_ref in legacy_specs or [ref.canonical for ref in release.specs]:
            ref, err = planning.parse_spec_ref(canonical_ref)
            if err or ref is None or ref.alias is not None:
                continue
            spec_dir = runtime_dir(root) / "specs" / ref.spec_id
            if not spec_dir.is_dir():
                continue
            for source_path in sorted(path for path in spec_dir.rglob("*") if path.is_file()):
                relative = source_path.relative_to(spec_dir)
                writes.append(
                    PlannedWrite(
                        mirror_root / ".builder" / "specs" / ref.spec_id / relative,
                        source_path.read_text(encoding="utf-8"),
                    )
                )
    return ImportPreview(subject, root, writes)


def render_import_preview(home: BuilderHome, preview: ImportPreview) -> str:
    lines = [
        f"Selected home: {home.root}",
        f"Import subject: {preview.subject}",
        f"Source root: {preview.source_root}",
    ]
    for item in preview.writes:
        lines.append(f"write {_relative_from(home, item.path)}")
        if item.content:
            lines.append(item.content.rstrip())
    return "\n".join(lines) + "\n"


def apply_import_preview(preview: ImportPreview) -> None:
    for item in preview.writes:
        item.path.parent.mkdir(parents=True, exist_ok=True)
        item.path.write_text(item.content or "", encoding="utf-8")
