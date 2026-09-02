from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Iterable

from _dispatch_runtime.paths import resolve_spec_dir, runtime_dir

from .common import ValidationError, ValidationIssue
from .parsers import (
    BuilderManifest,
    PolicyManifest,
    ProjectManifest,
    ReleaseManifest,
    ReleaseSpec,
    RepoCatalog,
    parse_builder_manifest,
    parse_policy_manifest,
    parse_project_manifest,
    parse_release_manifest,
    parse_repositories_manifest,
)


@dataclass(frozen=True)
class HomeRelease:
    manifest_path: Path
    declaration: ReleaseManifest


@dataclass(frozen=True)
class HomeProject:
    manifest_path: Path
    declaration: ProjectManifest
    releases: list[HomeRelease]

    @property
    def id(self) -> str:
        return self.declaration.product


@dataclass(frozen=True)
class BuilderHome:
    root: Path
    builder_path: Path
    manifest: BuilderManifest
    catalog: RepoCatalog
    policy: PolicyManifest
    projects: list[HomeProject]
    repo_roots_by_id: dict[str, Path]

    def project(self, product_id: str) -> HomeProject | None:
        for project in self.projects:
            if project.id == product_id:
                return project
        return None

    def projects_for_repo(self, repo_root: Path) -> list[HomeProject]:
        repo_real = repo_root.resolve()
        matches: list[HomeProject] = []
        for project in self.projects:
            for repo in project.declaration.repos:
                if self.repo_roots_by_id.get(repo.repo_id) == repo_real:
                    matches.append(project)
                    break
        return matches

    def default_project_for_repo(self, repo_root: Path) -> HomeProject | None:
        matches = self.projects_for_repo(repo_root)
        if len(matches) == 1:
            return matches[0]
        repo_real = repo_root.resolve()
        default_matches = [
            project
            for project in matches
            if project.declaration.default_repo
            and self.repo_roots_by_id.get(project.declaration.default_repo) == repo_real
        ]
        if len(default_matches) == 1:
            return default_matches[0]
        return None

    def resolve_project_alias(self, project_id: str, alias: str) -> Path | None:
        project = self.project(project_id)
        if project is None:
            return None
        repo_id = next((entry.repo_id for entry in project.declaration.repos if entry.alias == alias), None)
        if repo_id is None:
            return None
        return self.repo_roots_by_id.get(repo_id)

    def resolve_project_repo_id(self, project_id: str, repo_id: str) -> Path | None:
        project = self.project(project_id)
        if project is None:
            return None
        if repo_id not in {entry.repo_id for entry in project.declaration.repos}:
            return None
        return self.repo_roots_by_id.get(repo_id)

    def drains_repo(self, repo_id: str) -> bool:
        return self.policy.governor_enabled and repo_id in self.policy.drain_repos

    def release_refs(self, project_id: str) -> Iterable[HomeRelease]:
        project = self.project(project_id)
        return [] if project is None else list(project.releases)


def resolve_home_dir(
    *,
    start: Path | None = None,
    home: Path | None = None,
    projects_root: Path | None = None,
) -> Path | None:
    if home is not None:
        candidate = home.resolve()
        if candidate.is_file():
            if candidate.name != "builder.yaml":
                raise ValidationError([ValidationIssue(str(candidate), "home file must be builder.yaml")])
            return candidate.parent
        return candidate
    if projects_root is not None:
        candidate = projects_root.resolve() / ".builder-home"
        return candidate if (candidate / "builder.yaml").is_file() else None
    cursor = (start or Path.cwd()).resolve()
    if cursor.is_file():
        cursor = cursor.parent
    for parent in (cursor, *cursor.parents):
        if parent.name == ".builder-home" and (parent / "builder.yaml").is_file():
            return parent
        candidate = parent / ".builder-home"
        if (candidate / "builder.yaml").is_file():
            return candidate
    return None


def load_builder_home(home_dir: Path) -> BuilderHome:
    root = home_dir.resolve()
    builder_path = root / "builder.yaml"
    builder = parse_builder_manifest(builder_path)
    catalog = parse_repositories_manifest(builder.repositories)
    policy = parse_policy_manifest(builder.policy)
    repo_roots_by_id = {entry.id: entry.path.resolve() for entry in catalog.repos}
    findings: list[ValidationIssue] = []
    projects: list[HomeProject] = []
    for project_ref in builder.projects:
        project = parse_project_manifest(project_ref.manifest)
        if project.product != project_ref.id:
            findings.append(
                ValidationIssue(
                    str(project_ref.manifest),
                    f"product {project.product!r} does not match builder index id {project_ref.id!r}",
                )
            )
        for repo in project.repos:
            if repo.repo_id not in repo_roots_by_id:
                findings.append(
                    ValidationIssue(str(project_ref.manifest), f"repo_id {repo.repo_id!r} is not declared in repositories.yaml")
                )
        releases: list[HomeRelease] = []
        for release_ref in project.releases:
            release = parse_release_manifest(release_ref.manifest, project=project)
            if release.name != release_ref.id:
                findings.append(
                    ValidationIssue(
                        str(release_ref.manifest),
                        f"name {release.name!r} does not match project index name {release_ref.id!r}",
                    )
                )
            if release.intents:
                default_repo_id = project.default_repo
                intent_repo = repo_roots_by_id.get(default_repo_id or "")
                if intent_repo is None:
                    findings.append(
                        ValidationIssue(
                            str(release_ref.manifest),
                            "intents require a resolvable default_repo for repo-local intent objects",
                        )
                    )
                else:
                    # Import lazily to avoid a module-level planning <-> Builder Home cycle.
                    from _intent_model import load_repo_intents
                    from planning import parse_spec_ref

                    inventory, diagnostics = load_repo_intents(intent_repo, parse_spec_ref)
                    intents_by_id = {intent.intent: intent for intent in inventory}
                    diagnostics_by_path = {diagnostic.path: diagnostic for diagnostic in diagnostics}
                    flattened: list[ReleaseSpec] = []
                    for index, intent_id in enumerate(release.intents):
                        intent = intents_by_id.get(intent_id)
                        if intent is None:
                            relpath = f".builder/intents/{intent_id}/intent.yaml"
                            diagnostic = diagnostics_by_path.get(relpath)
                            detail = (
                                "; ".join(diagnostic.findings)
                                if diagnostic is not None
                                else f"missing intent object {intent_repo / relpath}"
                            )
                            findings.append(
                                ValidationIssue(
                                    f"{release_ref.manifest}:intents[{index}]",
                                    detail,
                                )
                            )
                            continue
                        if intent.status in {"rejected", "superseded"}:
                            findings.append(
                                ValidationIssue(
                                    f"{release_ref.manifest}:intents[{index}]",
                                    f"release references {intent.status} intent {intent_id!r}; remove or replace it",
                                )
                            )
                        flattened.extend(ReleaseSpec(spec=member, weight=1) for member in intent.specs)
                    release = replace(release, specs=flattened)
            releases.append(HomeRelease(release_ref.manifest, release))
        projects.append(HomeProject(project_ref.manifest, project, releases))
    if findings:
        raise ValidationError(findings)
    return BuilderHome(root, builder_path, builder, catalog, policy, projects, repo_roots_by_id)


def load_optional_home(
    *,
    start: Path | None = None,
    home: Path | None = None,
    projects_root: Path | None = None,
) -> BuilderHome | None:
    home_dir = resolve_home_dir(start=start, home=home, projects_root=projects_root)
    return None if home_dir is None else load_builder_home(home_dir)


def lint_loaded_home(home: BuilderHome) -> list[str]:
    findings: list[str] = []
    for repo_id in home.policy.drain_repos:
        if repo_id not in home.repo_roots_by_id:
            findings.append(f"{home.root / 'policy.yaml'}: governor.drain_repos contains unknown repo id {repo_id!r}")
    for entry in home.catalog.repos:
        if not entry.portable:
            findings.append(f"{home.root / 'repositories.yaml'}: repo {entry.id!r} uses a non-portable absolute path")
    for project in home.projects:
        declared_repo_ids = {repo.repo_id for repo in project.declaration.repos}
        for repo_id in declared_repo_ids:
            if repo_id not in home.repo_roots_by_id:
                findings.append(f"{project.manifest_path}: repo_id {repo_id!r} is not declared in repositories.yaml")
        for release in project.releases:
            seen_physical: set[tuple[str, str]] = set()
            for raw_ref in release.declaration.specs:
                ref = raw_ref.spec
                if "/" in ref:
                    alias, spec_id = ref.split("/", 1)
                    repo_id = next((repo.repo_id for repo in project.declaration.repos if repo.alias == alias), None)
                else:
                    spec_id = ref
                    repo_id = project.declaration.default_repo
                if not repo_id:
                    continue
                physical = (repo_id, spec_id)
                if physical in seen_physical:
                    findings.append(f"{release.manifest_path}: duplicate physical spec {repo_id}/{spec_id}")
                else:
                    seen_physical.add(physical)
                repo_root = home.repo_roots_by_id.get(repo_id)
                if repo_root is None:
                    continue
                specs_root = runtime_dir(repo_root) / "specs"
                # Live spec, else its archived form -- archiving a release-referenced spec must
                # not dangle the release that names it. The diagnostic still points at the LIVE
                # path, which is where a genuinely missing spec should be.
                spec_dir = resolve_spec_dir(specs_root, spec_id) or (specs_root / spec_id)
                if not spec_dir.is_dir():
                    findings.append(f"{release.manifest_path}: dangling ref {ref!r} (no spec dir at {spec_dir})")
            if release.declaration.status == "active" and len(seen_physical) < 2:
                findings.append(f"{release.manifest_path}: active release requires at least two unique physical members")
        for repo in project.declaration.repos:
            repo_root = home.repo_roots_by_id.get(repo.repo_id)
            if repo_root is None:
                continue
            legacy_path = runtime_dir(repo_root) / "product.yaml"
            if not legacy_path.is_file():
                continue
            try:
                from planning import parse_product as parse_legacy_product  # local import avoids a top-level cycle
            except Exception:
                continue
            legacy = parse_legacy_product(legacy_path)
            if not legacy.product:
                continue
            if legacy.product == project.id:
                legacy_aliases = set(legacy.repo_aliases)
                canonical_aliases = {item.alias for item in project.declaration.repos}
                if legacy_aliases != canonical_aliases:
                    findings.append(
                        f"{legacy_path}: conflicts with canonical project {project.id!r} aliases {sorted(canonical_aliases)}"
                    )
            elif legacy.product in {p.id for p in home.projects}:
                # Many-to-many membership is supported (design §4: "aliases are project-scoped;
                # many-to-many membership is supported"). A repo may legitimately be a member of
                # several projects — e.g. a design system owned by one and shared into another.
                # So this is only a conflict when the legacy-named owner project does NOT itself
                # declare this repo: the repo claims an owner that disowns it (a real orphan).
                owner = home.project(legacy.product)
                owner_repo_ids = (
                    {item.repo_id for item in owner.declaration.repos} if owner is not None else set()
                )
                if repo.repo_id not in owner_repo_ids:
                    findings.append(
                        f"{legacy_path}: legacy product {legacy.product!r} conflicts with canonical declaration of the same id"
                    )
    return findings


def render_home_status(home: BuilderHome) -> str:
    findings = lint_loaded_home(home)
    release_count = sum(len(project.releases) for project in home.projects)
    lines = [
        f"Selected home: {home.root}",
        f"home_id: {home.manifest.home_id}",
        f"repos: {len(home.catalog.repos)}",
        f"projects: {len(home.projects)}",
        f"releases: {release_count}",
        f"lint: {'clean' if not findings else f'{len(findings)} finding(s)'}",
    ]
    lines.extend(findings)
    return "\n".join(lines) + "\n"
