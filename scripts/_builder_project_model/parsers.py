from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .common import (
    CANONICAL_PROVIDERS,
    RELEASE_STATUSES,
    release_membership_field,
    release_uses_intents,
    ValidationError,
    ValidationIssue,
    as_clean_str,
    ensure_relative_contained,
    is_safe_id,
    load_yaml_mapping,
    reject_unknown_keys,
    require_schema_version,
)


@dataclass(frozen=True)
class BuilderProjectRef:
    id: str
    manifest: Path


@dataclass(frozen=True)
class BuilderManifest:
    home_id: str
    repositories: Path
    policy: Path
    projects: list[BuilderProjectRef]


@dataclass(frozen=True)
class RepoCatalogEntry:
    id: str
    path: Path
    portable: bool


@dataclass(frozen=True)
class RepoCatalog:
    repos: list[RepoCatalogEntry]


@dataclass(frozen=True)
class ProjectRepo:
    alias: str
    repo_id: str


@dataclass(frozen=True)
class ProjectManifest:
    product: str
    title: str
    description: str
    default_repo: str | None
    repos: list[ProjectRepo]
    backlog: list[str]
    releases: list[BuilderProjectRef]


@dataclass(frozen=True)
class ReleaseSpec:
    spec: str
    weight: int


@dataclass(frozen=True)
class ReleaseManifest:
    name: str
    description: str
    status: str
    specs: list[ReleaseSpec]
    intents: tuple[str, ...]


@dataclass(frozen=True)
class ProviderPolicy:
    max_sessions: int
    quota_initial_seconds: int
    quota_max_seconds: int


@dataclass(frozen=True)
class PolicyManifest:
    providers: dict[str, ProviderPolicy]
    scheduler: dict[str, int]
    governor_enabled: bool
    drain_repos: tuple[str, ...]


def _parse_spec_ref(raw: Any, *, location: str) -> tuple[str | None, list[ValidationIssue]]:
    if isinstance(raw, dict):
        raw = raw.get("spec")
    if not isinstance(raw, str) or not raw.strip():
        return None, [ValidationIssue(location, "spec ref must be a non-empty string")]
    ref = raw.strip()
    if "\\" in ref:
        return None, [ValidationIssue(location, "spec ref contains a backslash")]
    parts = ref.split("/")
    if len(parts) == 1:
        spec_id = parts[0]
        alias = None
    elif len(parts) == 2:
        alias, spec_id = parts
        if not is_safe_id(alias):
            return None, [ValidationIssue(location, "repo alias must match [a-z0-9][a-z0-9-]*")]
    else:
        return None, [ValidationIssue(location, "spec ref has too many segments")]
    if spec_id in ("", ".", "..") or "/" in spec_id or "\\" in spec_id:
        return None, [ValidationIssue(location, f"unsafe spec id {spec_id!r}")]
    return ref, []


def parse_builder_manifest(path: Path) -> BuilderManifest:
    data = load_yaml_mapping(path)
    issues = require_schema_version(data, location=str(path))
    issues.extend(reject_unknown_keys(data, {"schema_version", "home_id", "repositories", "policy", "projects"}, location=str(path)))
    home_id = as_clean_str(data.get("home_id"))
    if not is_safe_id(home_id):
        issues.append(ValidationIssue(str(path), "home_id must match [a-z0-9][a-z0-9-]*"))
    container = path.parent
    repositories, repo_issues = ensure_relative_contained(
        data.get("repositories"), base_dir=container, container_dir=container, location=f"{path}:repositories"
    )
    policy, policy_issues = ensure_relative_contained(
        data.get("policy"), base_dir=container, container_dir=container, location=f"{path}:policy"
    )
    issues.extend(repo_issues)
    issues.extend(policy_issues)
    projects: list[BuilderProjectRef] = []
    seen_ids: set[str] = set()
    seen_manifests: set[Path] = set()
    raw_projects = data.get("projects")
    if not isinstance(raw_projects, list):
        issues.append(ValidationIssue(str(path), "projects must be a list"))
    else:
        for index, entry in enumerate(raw_projects):
            location = f"{path}:projects[{index}]"
            if not isinstance(entry, dict):
                issues.append(ValidationIssue(location, "entry must be a mapping"))
                continue
            issues.extend(reject_unknown_keys(entry, {"id", "manifest"}, location=location))
            project_id = as_clean_str(entry.get("id"))
            if not is_safe_id(project_id):
                issues.append(ValidationIssue(location, "id must match [a-z0-9][a-z0-9-]*"))
            manifest, manifest_issues = ensure_relative_contained(
                entry.get("manifest"),
                base_dir=container,
                container_dir=container / "projects",
                location=f"{location}.manifest",
            )
            issues.extend(manifest_issues)
            if project_id in seen_ids:
                issues.append(ValidationIssue(location, f"duplicate project id {project_id!r}"))
            elif project_id:
                seen_ids.add(project_id)
            if manifest is not None:
                if manifest in seen_manifests:
                    issues.append(ValidationIssue(location, f"duplicate project manifest {manifest}"))
                else:
                    seen_manifests.add(manifest)
            if project_id and manifest is not None:
                projects.append(BuilderProjectRef(project_id, manifest))
    if issues:
        raise ValidationError(issues)
    return BuilderManifest(home_id=home_id, repositories=repositories, policy=policy, projects=projects)  # type: ignore[arg-type]


def parse_repositories_manifest(path: Path) -> RepoCatalog:
    data = load_yaml_mapping(path)
    issues = require_schema_version(data, location=str(path))
    issues.extend(reject_unknown_keys(data, {"schema_version", "repos"}, location=str(path)))
    raw_repos = data.get("repos")
    repos: list[RepoCatalogEntry] = []
    seen_ids: set[str] = set()
    seen_realpaths: set[Path] = set()
    if not isinstance(raw_repos, list):
        issues.append(ValidationIssue(str(path), "repos must be a list"))
    else:
        for index, entry in enumerate(raw_repos):
            location = f"{path}:repos[{index}]"
            if not isinstance(entry, dict):
                issues.append(ValidationIssue(location, "entry must be a mapping"))
                continue
            issues.extend(reject_unknown_keys(entry, {"id", "path"}, location=location))
            repo_id = as_clean_str(entry.get("id"))
            if not is_safe_id(repo_id):
                issues.append(ValidationIssue(location, "id must match [a-z0-9][a-z0-9-]*"))
            raw_path = entry.get("path")
            portable = isinstance(raw_path, str) and not Path(raw_path).is_absolute()
            resolved, path_issues = ensure_relative_contained(
                raw_path,
                base_dir=path.parent,
                container_dir=path.parent.parent,
                location=f"{location}.path",
                allow_absolute=True,
            )
            issues.extend(path_issues)
            if resolved is not None:
                if not resolved.exists():
                    issues.append(ValidationIssue(location, f"repo path does not exist: {resolved}"))
                elif not (resolved / ".git").exists():
                    issues.append(ValidationIssue(location, f"repo path is not a repository root: {resolved}"))
                if resolved in seen_realpaths:
                    issues.append(ValidationIssue(location, f"duplicate repo real path {resolved}"))
                else:
                    seen_realpaths.add(resolved)
            if repo_id in seen_ids:
                issues.append(ValidationIssue(location, f"duplicate repo id {repo_id!r}"))
            elif repo_id:
                seen_ids.add(repo_id)
            if repo_id and resolved is not None:
                repos.append(RepoCatalogEntry(repo_id, resolved, portable))
    if issues:
        raise ValidationError(issues)
    return RepoCatalog(repos)


def parse_project_manifest(path: Path) -> ProjectManifest:
    data = load_yaml_mapping(path)
    issues = require_schema_version(data, location=str(path))
    issues.extend(reject_unknown_keys(
        data,
        {"schema_version", "product", "title", "description", "default_repo", "repos", "backlog", "releases"},
        location=str(path),
    ))
    product = as_clean_str(data.get("product"))
    if not is_safe_id(product):
        issues.append(ValidationIssue(str(path), "product must match [a-z0-9][a-z0-9-]*"))
    repos: list[ProjectRepo] = []
    seen_aliases: set[str] = set()
    seen_repo_ids: set[str] = set()
    raw_repos = data.get("repos")
    if not isinstance(raw_repos, list) or not raw_repos:
        issues.append(ValidationIssue(str(path), "repos must be a non-empty list"))
    else:
        for index, entry in enumerate(raw_repos):
            location = f"{path}:repos[{index}]"
            if not isinstance(entry, dict):
                issues.append(ValidationIssue(location, "entry must be a mapping"))
                continue
            issues.extend(reject_unknown_keys(entry, {"alias", "repo_id"}, location=location))
            alias = as_clean_str(entry.get("alias"))
            repo_id = as_clean_str(entry.get("repo_id"))
            if not is_safe_id(alias):
                issues.append(ValidationIssue(location, "alias must match [a-z0-9][a-z0-9-]*"))
            if not is_safe_id(repo_id):
                issues.append(ValidationIssue(location, "repo_id must match [a-z0-9][a-z0-9-]*"))
            if alias in seen_aliases:
                issues.append(ValidationIssue(location, f"duplicate alias {alias!r}"))
            elif alias:
                seen_aliases.add(alias)
            if repo_id:
                seen_repo_ids.add(repo_id)
            if alias and repo_id:
                repos.append(ProjectRepo(alias, repo_id))
    default_repo = as_clean_str(data.get("default_repo")) or None
    if default_repo is not None and default_repo not in seen_repo_ids:
        issues.append(ValidationIssue(str(path), f"default_repo {default_repo!r} is not a declared repo_id"))
    backlog: list[str] = []
    backlog_seen: set[str] = set()
    raw_backlog = data.get("backlog", [])
    if raw_backlog is not None and not isinstance(raw_backlog, list):
        issues.append(ValidationIssue(str(path), "backlog must be a list"))
    else:
        for index, entry in enumerate(raw_backlog or []):
            ref, ref_issues = _parse_spec_ref(entry, location=f"{path}:backlog[{index}]")
            issues.extend(ref_issues)
            if ref is None:
                continue
            if "/" not in ref and default_repo is None:
                issues.append(ValidationIssue(f"{path}:backlog[{index}]", "bare spec ref requires default_repo"))
            if ref in backlog_seen:
                issues.append(ValidationIssue(f"{path}:backlog[{index}]", f"duplicate backlog ref {ref!r}"))
            else:
                backlog_seen.add(ref)
                backlog.append(ref)
    releases: list[BuilderProjectRef] = []
    seen_names: set[str] = set()
    seen_manifests: set[Path] = set()
    raw_releases = data.get("releases")
    if not isinstance(raw_releases, list):
        issues.append(ValidationIssue(str(path), "releases must be a list"))
    else:
        for index, entry in enumerate(raw_releases):
            location = f"{path}:releases[{index}]"
            if not isinstance(entry, dict):
                issues.append(ValidationIssue(location, "entry must be a mapping"))
                continue
            issues.extend(reject_unknown_keys(entry, {"name", "manifest"}, location=location))
            name = as_clean_str(entry.get("name"))
            if not is_safe_id(name):
                issues.append(ValidationIssue(location, "name must match [a-z0-9][a-z0-9-]*"))
            manifest, manifest_issues = ensure_relative_contained(
                entry.get("manifest"),
                base_dir=path.parent,
                container_dir=path.parent,
                location=f"{location}.manifest",
            )
            issues.extend(manifest_issues)
            if name in seen_names:
                issues.append(ValidationIssue(location, f"duplicate release name {name!r}"))
            elif name:
                seen_names.add(name)
            if manifest is not None:
                if manifest in seen_manifests:
                    issues.append(ValidationIssue(location, f"duplicate release manifest {manifest}"))
                else:
                    seen_manifests.add(manifest)
            if name and manifest is not None:
                releases.append(BuilderProjectRef(name, manifest))
    if issues:
        raise ValidationError(issues)
    return ProjectManifest(
        product=product,
        title=as_clean_str(data.get("title")) or product,
        description=as_clean_str(data.get("description")),
        default_repo=default_repo,
        repos=repos,
        backlog=backlog,
        releases=releases,
    )


def parse_release_manifest(path: Path, *, project: ProjectManifest | None = None) -> ReleaseManifest:
    data = load_yaml_mapping(path)
    issues = require_schema_version(data, location=str(path))
    issues.extend(reject_unknown_keys(data, {"schema_version", "name", "description", "status", "specs", "intents", "adopted_intents"}, location=str(path)))
    name = as_clean_str(data.get("name"))
    if not is_safe_id(name):
        issues.append(ValidationIssue(str(path), "name must match [a-z0-9][a-z0-9-]*"))
    if name and name != path.stem:
        issues.append(ValidationIssue(str(path), f"name {name!r} does not match filename {path.stem!r}"))
    status = as_clean_str(data.get("status")).lower()
    if status not in RELEASE_STATUSES:
        issues.append(ValidationIssue(str(path), f"status must be one of {', '.join(RELEASE_STATUSES)}"))
    membership_field = release_membership_field(status)
    specs: list[ReleaseSpec] = []
    intents: list[str] = []
    seen_specs: set[str] = set()
    seen_intents: set[str] = set()
    physical_members: set[tuple[str, str]] = set()
    raw_specs = data.get("specs")
    raw_intents = data.get("intents")
    if raw_specs is not None and raw_intents is not None:
        issues.append(ValidationIssue(str(path), "release may declare only one membership field: specs or intents"))

    if membership_field == "specs":
        if raw_intents is not None:
            issues.append(ValidationIssue(str(path), "historical releases must not declare intents"))
        if not isinstance(raw_specs, list):
            issues.append(ValidationIssue(str(path), "specs must be a list"))
        elif not raw_specs:
            issues.append(ValidationIssue(str(path), "specs must be a non-empty list"))
        else:
            for index, entry in enumerate(raw_specs):
                location = f"{path}:specs[{index}]"
                weight = 1
                if isinstance(entry, dict):
                    issues.extend(reject_unknown_keys(entry, {"spec", "weight"}, location=location))
                    raw_weight = entry.get("weight", 1)
                    if not isinstance(raw_weight, int) or isinstance(raw_weight, bool) or raw_weight < 1:
                        issues.append(ValidationIssue(location, "weight must be an integer >= 1"))
                    else:
                        weight = raw_weight
                ref, ref_issues = _parse_spec_ref(entry, location=location)
                issues.extend(ref_issues)
                if ref is None:
                    continue
                if ref in seen_specs:
                    issues.append(ValidationIssue(location, f"duplicate release ref {ref!r}"))
                    continue
                seen_specs.add(ref)
                alias, _, spec_id = ref.rpartition("/")
                if "/" not in ref:
                    if project is None or project.default_repo is None:
                        issues.append(ValidationIssue(location, "bare spec ref requires default_repo"))
                        continue
                    physical = (project.default_repo, ref)
                else:
                    repo_id = next((repo.repo_id for repo in project.repos), None) if project is None else None
                    if project is not None:
                        mapped = next((repo.repo_id for repo in project.repos if repo.alias == alias), "")
                        if not mapped:
                            issues.append(ValidationIssue(location, f"unknown project alias {alias!r}"))
                            continue
                        repo_id = mapped
                    if not repo_id:
                        repo_id = alias
                    physical = (repo_id, spec_id)
                if physical in physical_members:
                    issues.append(ValidationIssue(location, f"duplicate physical spec {physical[0]}/{physical[1]}"))
                else:
                    physical_members.add(physical)
                if project is not None and ref in project.backlog:
                    issues.append(ValidationIssue(location, f"spec {ref!r} appears in backlog and release"))
                specs.append(ReleaseSpec(ref, weight))
    elif membership_field == "intents":
        if raw_specs is not None:
            issues.append(ValidationIssue(str(path), "live releases must not declare specs"))
        if not isinstance(raw_intents, list):
            issues.append(ValidationIssue(str(path), "intents must be a list"))
        elif not raw_intents:
            issues.append(ValidationIssue(str(path), "intents must be a non-empty list"))
        else:
            for index, entry in enumerate(raw_intents):
                location = f"{path}:intents[{index}]"
                intent_id = as_clean_str(entry)
                if not is_safe_id(intent_id):
                    issues.append(ValidationIssue(location, "intent id must match [a-z0-9][a-z0-9-]*"))
                    continue
                if intent_id in seen_intents:
                    issues.append(ValidationIssue(location, f"duplicate intent id {intent_id!r}"))
                    continue
                seen_intents.add(intent_id)
                intents.append(intent_id)
    elif status:
        issues.append(ValidationIssue(str(path), f"status {status!r} has no declared membership mode"))
    if issues:
        raise ValidationError(issues)
    return ReleaseManifest(
        name=name,
        description=as_clean_str(data.get("description")),
        status=status,
        specs=specs,
        intents=tuple(intents),
    )


def parse_policy_manifest(path: Path) -> PolicyManifest:
    data = load_yaml_mapping(path)
    issues = require_schema_version(data, location=str(path))
    issues.extend(reject_unknown_keys(data, {"schema_version", "providers", "allocation", "scheduler", "governor"}, location=str(path)))
    raw_providers = data.get("providers")
    providers: dict[str, ProviderPolicy] = {}
    if not isinstance(raw_providers, dict):
        issues.append(ValidationIssue(str(path), "providers must be a mapping"))
    else:
        for name in raw_providers:
            if name not in CANONICAL_PROVIDERS:
                issues.append(ValidationIssue(str(path), f"unknown provider key {name!r}"))
        for provider_name in CANONICAL_PROVIDERS:
            entry = raw_providers.get(provider_name)
            if not isinstance(entry, dict):
                issues.append(ValidationIssue(str(path), f"provider {provider_name!r} must be a mapping"))
                continue
            issues.extend(reject_unknown_keys(entry, {"max_sessions", "quota_cooldown"}, location=f"{path}:providers.{provider_name}"))
            max_sessions = entry.get("max_sessions")
            if not isinstance(max_sessions, int) or isinstance(max_sessions, bool) or max_sessions < 1:
                issues.append(ValidationIssue(str(path), f"{provider_name}.max_sessions must be an integer >= 1"))
                continue
            cooldown = entry.get("quota_cooldown")
            if not isinstance(cooldown, dict):
                issues.append(ValidationIssue(str(path), f"{provider_name}.quota_cooldown must be a mapping"))
                continue
            issues.extend(reject_unknown_keys(cooldown, {"initial_seconds", "max_seconds"}, location=f"{path}:providers.{provider_name}.quota_cooldown"))
            initial = cooldown.get("initial_seconds")
            maximum = cooldown.get("max_seconds")
            if not isinstance(initial, int) or isinstance(initial, bool) or initial < 1:
                issues.append(ValidationIssue(str(path), f"{provider_name}.quota_cooldown.initial_seconds must be an integer >= 1"))
                continue
            if not isinstance(maximum, int) or isinstance(maximum, bool) or maximum < initial:
                issues.append(ValidationIssue(str(path), f"{provider_name}.quota_cooldown.max_seconds must be an integer >= initial_seconds"))
                continue
            providers[provider_name] = ProviderPolicy(max_sessions, initial, maximum)
    allocation = data.get("allocation")
    if not isinstance(allocation, dict):
        issues.append(ValidationIssue(str(path), "allocation must be a mapping"))
    else:
        issues.extend(reject_unknown_keys(allocation, {"policy", "project_weight"}, location=f"{path}:allocation"))
        if allocation.get("policy") != "equal-weight-fair-share":
            issues.append(ValidationIssue(str(path), "allocation.policy must be 'equal-weight-fair-share'"))
        if allocation.get("project_weight") != 1:
            issues.append(ValidationIssue(str(path), "allocation.project_weight must be 1"))
    raw_scheduler = data.get("scheduler")
    scheduler: dict[str, int] = {}
    if not isinstance(raw_scheduler, dict):
        issues.append(ValidationIssue(str(path), "scheduler must be a mapping"))
    else:
        issues.extend(reject_unknown_keys(raw_scheduler, {"poll_seconds", "heartbeat_seconds", "stale_daemon_seconds"}, location=f"{path}:scheduler"))
        for key in ("poll_seconds", "heartbeat_seconds", "stale_daemon_seconds"):
            value = raw_scheduler.get(key)
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                issues.append(ValidationIssue(str(path), f"scheduler.{key} must be an integer >= 1"))
            else:
                scheduler[key] = value
    governor_enabled = False
    drain_repos: list[str] = []
    raw_governor = data.get("governor")
    if raw_governor is not None:
        if not isinstance(raw_governor, dict):
            issues.append(ValidationIssue(str(path), "governor must be a mapping"))
        else:
            issues.extend(reject_unknown_keys(raw_governor, {"enabled", "drain_repos"}, location=f"{path}:governor"))
            enabled = raw_governor.get("enabled", False)
            if not isinstance(enabled, bool):
                issues.append(ValidationIssue(str(path), "governor.enabled must be a boolean"))
            else:
                governor_enabled = enabled
            raw_drain_repos = raw_governor.get("drain_repos", [])
            if not isinstance(raw_drain_repos, list):
                issues.append(ValidationIssue(str(path), "governor.drain_repos must be a list"))
            else:
                seen_repo_ids: set[str] = set()
                for index, repo_id_raw in enumerate(raw_drain_repos):
                    location = f"{path}:governor.drain_repos[{index}]"
                    repo_id = as_clean_str(repo_id_raw)
                    if not is_safe_id(repo_id):
                        issues.append(ValidationIssue(location, "repo id must match [a-z0-9][a-z0-9-]*"))
                        continue
                    if repo_id in seen_repo_ids:
                        issues.append(ValidationIssue(location, f"duplicate repo id {repo_id!r}"))
                        continue
                    seen_repo_ids.add(repo_id)
                    drain_repos.append(repo_id)
    if issues:
        raise ValidationError(issues)
    return PolicyManifest(
        providers=providers,
        scheduler=scheduler,
        governor_enabled=governor_enabled,
        drain_repos=tuple(drain_repos),
    )
