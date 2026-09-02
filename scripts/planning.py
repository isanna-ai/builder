#!/usr/bin/env python3
"""The planning layer — Product -> Release -> Spec, with a % done agents cannot inflate.

A Release is a human-authored set of specs (possibly across repos) that ships a coherent product
evolution. Its completeness is `verified specs / manifest specs`:

  * DENOMINATOR — the spec list in the human-authored release file. Read-only to agents, absent from
    every packet's allowed_change_files. It changes only by a human editing the file.
  * NUMERATOR — manifest specs the HOST stamped `host-verified`. The sole self-declared status this
    module recognizes is `planned`, which can only keep a member out of the numerator; every other
    status reuses gate-coverage's `scan_repo`, which counts a spec verified only when the host itself
    ran the verify commands on every accepted turn. An agent that writes more tasks, pads
    phase-log.yaml, or sets `status: verified` moves this number by exactly zero.

That is the whole thesis one level up: every point on the bar is a host event the agent cannot forge.

Tiers (the zero-dependency pitch stays literally true at each):
  0  one repo, specs, host gates                                    — no server, no DB, no network
  1  releases/ in your one repo -> `isanna release status`          — still no server, no DB
  2  product.yaml spanning repos -> cross-repo refs + a viewer      — the server only VIEWS files

With no product.yaml a repo is its own implicit product; with no release files nothing changes.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from _intent_model import (
    BacklogCapabilityOwner,
    BacklogCapabilityOwners,
    IntentFileDiagnostic,
    IntentMemberState,
    IMPLEMENTATION_OR_LATER_STATUSES,
    PRE_IMPLEMENTATION_STATUSES,
    VisibleIntent,
    load_repo_intents,
    project_visible_state,
    validate_intent_target,
)
from _dispatch_runtime.paths import resolve_spec_dir, runtime_dir
from _dispatch_runtime.phase_runtime import sync_visibility
from _builder_project_model.common import (
    HISTORICAL_RELEASE_STATUSES,
    LIVE_RELEASE_STATUSES,
    RELEASE_STATUSES,
    release_uses_intents,
)
from _builder_project_model import BuilderHome, load_optional_home, lint_loaded_home
from _builder_project_model.common import ValidationError
from _builder_project_model.home import resolve_home_dir
from _builder_project_model.parsers import (
    parse_builder_manifest,
    parse_project_manifest,
    parse_release_manifest,
    parse_repositories_manifest,
)

sys.dont_write_bytecode = True

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

ALIAS_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
# Verify-phase closure the HOST adjudicates. gate-coverage collapses a spec's turns to one of these.
HOST_VERIFIED = "host-verified"
PLANNED = "planned"
SELF_REPORTED = "self-reported"
UNKNOWN = "unknown"
SYNCED = "synced"
VERIFIED_AWAITING_SYNC = "verified-awaiting-sync"
PLANNED_DECOMPOSING = "planned-decomposing"

TEMPLATES = SCRIPTS.parent / "templates"


# --------------------------------------------------------------------------- yaml (shim or real)

def _yaml():
    from _yaml import yaml
    return yaml


def _safe_load(path: Path) -> Any:
    # NEVER follow a symlinked YAML. Containment guards the spec DIR, but the files read inside it
    # (dependencies.yaml, spec.yaml, release/product files) are read at fixed paths -- a committed
    # `dependencies.yaml -> /tmp/outside.yaml` inside an otherwise-valid dir would otherwise escape
    # with no race at all (adversarial review, final round). A planning artifact is never
    # legitimately a symlink, so refusing one is a safe, static close: treat it as unreadable.
    try:
        if path.is_symlink():
            return None
    except OSError:
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    try:
        return _yaml().safe_load(text)
    except Exception:
        return None


# --------------------------------------------------------------------------- gate-coverage reuse

_scan_cache: dict[str, dict] = {}


def _gate_coverage():
    spec = importlib.util.spec_from_file_location("planning_gate_coverage", SCRIPTS / "gate-coverage.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["planning_gate_coverage"] = module  # dataclasses look themselves up here
    spec.loader.exec_module(module)
    return module


def _scan_repo(repo_root: Path) -> dict:
    """gate-coverage's host-observed verdict per spec, cached per repo. The ONLY source of the
    completeness numerator — never a spec's self-declared status.

    Passes no `check_chain` kwarg deliberately: scan_repo's own default (True) governs here.
    Chain-checking is load-bearing for the numerator — a gate-evidence bundle whose hash chain
    was mutated after the gate ran must NOT count as host-verified, and that only happens when
    the chain is actually walked. Do not add `check_chain=False` here."""
    key = str(repo_root.resolve())
    if key not in _scan_cache:
        try:
            _scan_cache[key] = _gate_coverage().scan_repo(repo_root)
        except Exception as exc:  # a repo we cannot audit is BLIND, never silently "verified"
            _scan_cache[key] = {"error": str(exc), "specs": []}
    return _scan_cache[key]


def _spec_verification(repo_root: Path, spec_id: str) -> str:
    scan = _scan_repo(repo_root)
    for row in scan.get("specs", []):
        # gate-coverage's scan_repo emits the id under `spec` (see stamp_spec); accept `spec_id`
        # too for any caller that seeds the cache with that shape. Matching only `spec_id` silently
        # never matched a real scan, so EVERY host-verified member fell through to UNKNOWN and the
        # numerator always read 0.
        if (row.get("spec") or row.get("spec_id")) == spec_id:
            return row.get("verification") or UNKNOWN
    return UNKNOWN  # a manifest spec with no scan row does not exist / was never run -> not verified


# --------------------------------------------------------------------------- entity model

@dataclass(frozen=True)
class SpecRef:
    """A member of a release. `alias` is None for a bare (home-repo) ref."""
    alias: str | None
    spec_id: str
    weight: int = 1
    raw: str = ""

    @property
    def canonical(self) -> str:
        return f"{self.alias}/{self.spec_id}" if self.alias else self.spec_id


@dataclass
class Release:
    release_id: str
    product: str
    title: str
    goal: str
    status: str
    specs: list[SpecRef]
    intents: tuple[str, ...]
    scope_ratified_at: str | None
    path: Path
    home_repo: Path
    parse_errors: list[str] = field(default_factory=list)
    # Owner-adopted intents: release-level reconciliation for intents whose member specs are
    # host-verified + merged to main but whose spec-level sync bookkeeping can't be reconstructed
    # (already-merged work). Honored by completeness() ONLY when every member is host-verified,
    # and disclosed separately in `release status`. Never touches spec-level sync artifacts.
    adopted_intents: tuple[str, ...] = ()


@dataclass
class Product:
    product: str
    title: str
    repo_aliases: list[str]
    home_repo: Path
    path: Path
    parse_errors: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- ref grammar

def parse_spec_ref(raw: Any) -> tuple[SpecRef | None, str | None]:
    """`bare-id` -> home repo; `<alias>/<spec-id>` -> cross-repo. Rejects anything that could
    escape a specs dir: `.`/`..` segments, extra slashes, backslashes, empty parts."""
    weight = 1
    if isinstance(raw, dict):
        spec_val = raw.get("spec")
        w = raw.get("weight", 1)
        if isinstance(w, int) and not isinstance(w, bool) and w >= 1:
            weight = w
        raw_ref = spec_val
    else:
        raw_ref = raw
    if not isinstance(raw_ref, str) or not raw_ref.strip():
        return None, f"spec ref is not a non-empty string: {raw!r}"
    ref = raw_ref.strip()
    if "\\" in ref:
        return None, f"spec ref contains a backslash: {ref!r}"
    parts = ref.split("/")
    if len(parts) == 1:
        alias, spec_id = None, parts[0]
    elif len(parts) == 2:
        alias, spec_id = parts[0], parts[1]
    else:
        return None, f"spec ref has too many segments (expected `<alias>/<spec-id>`): {ref!r}"
    if alias is not None and not ALIAS_RE.match(alias):
        return None, f"repo alias is not [a-z0-9][a-z0-9-]*: {alias!r}"
    if not _safe_spec_id(spec_id):
        return None, f"spec id is unsafe (empty, '.', '..', or contains a separator): {spec_id!r}"
    return SpecRef(alias=alias, spec_id=spec_id, weight=weight, raw=ref), None


def _safe_spec_id(spec_id: str) -> bool:
    """Mirror of the dispatcher's _dep_target_safe: a spec id must never be turnable into a
    traversal. Non-empty, not '.'/'..', no path separator."""
    return bool(spec_id) and spec_id not in (".", "..") and "/" not in spec_id and "\\" not in spec_id


# --------------------------------------------------------------------------- discovery / registry

def default_projects_root(home_repo: Path) -> Path:
    for env in ("ISANNA_PROJECTS_ROOT", "BUILDER_PROJECTS_DIR"):
        val = os.environ.get(env)
        if val and Path(val).is_dir():
            return Path(val).resolve()
    return home_repo.resolve().parent


class Registry:
    """Maps a repo alias to its root, safely. Alias -> root only if a discovered product.yaml
    declares it and the resolved root is contained under the projects root. Aliases cannot contain
    `.` or separators (ALIAS_RE), so `..` is unrepresentable and no ref becomes a path by string
    concatenation."""

    def __init__(self, projects_root: Path, home_repo: Path, product_context: str | None = None):
        self.projects_root = projects_root.resolve()
        self.home_repo = home_repo.resolve()
        self.product_context = product_context
        self.products: list[Product] = []
        self.alias_to_root: dict[str, Path] = {}
        self.findings: list[str] = []
        self.builder_home: BuilderHome | None = None
        self.home_project_id: str | None = None
        self._discover()

    def _discover(self) -> None:
        try:
            self.builder_home = load_optional_home(start=self.home_repo, projects_root=self.projects_root)
        except ValidationError:
            self.builder_home = None
        except Exception as exc:
            self.findings.append(str(exc))
            self.builder_home = None
        if self.builder_home is not None:
            self._discover_builder_home()
            return
        self._discover_legacy()

    def _legacy_product_files(self) -> list[Path]:
        """Every readable `<sibling>/.builder/product.yaml` beside this repo.

        A directory we are not allowed to read is not a project, and it is not a crash either.
        `Path.is_file()` propagates EACCES (pathlib ignores only ENOENT/ENOTDIR/EBADF/ELOOP), so
        scanning a projects root that happens to contain one unreadable sibling -- another user's
        directory, a root-owned mode-700 temp dir, anything -- used to raise PermissionError out
        of discovery and take the whole command down with it. Someone else's permissions are not
        a fact about this repo.
        """
        found: list[Path] = []
        try:
            siblings = list(self.projects_root.iterdir())
        except OSError as exc:
            self.findings.append(f"cannot enumerate projects root {self.projects_root}: {exc}")
            return found
        for project_dir in siblings:
            try:
                if not project_dir.is_dir():
                    continue
                candidate = runtime_dir(project_dir) / "product.yaml"
                if candidate.is_file():
                    found.append(candidate)
            except OSError:
                continue
        return found

    def _discover_legacy(self) -> None:
        claimed: dict[str, str] = {}  # alias -> product that claimed it
        home_of: dict[str, Path] = {}  # product name -> home repo that declared it
        for product_file in sorted(self._legacy_product_files()):
            product = parse_product(product_file)
            self.products.append(product)
            self.findings.extend(product.parse_errors)
            if product.product:
                # A product has EXACTLY one home repo. Two product.yaml declaring the same product
                # name is an ambiguous registry, not a silently-picked winner.
                if product.product in home_of and home_of[product.product] != product.home_repo:
                    self.findings.append(
                        f"product '{product.product}' has two home repos: "
                        f"{home_of[product.product]} and {product.home_repo}")
                else:
                    home_of[product.product] = product.home_repo
            for alias in product.repo_aliases:
                if alias in claimed and claimed[alias] != product.product:
                    self.findings.append(
                        f"repo alias '{alias}' claimed by two products: "
                        f"'{claimed[alias]}' and '{product.product}'")
                    continue
                claimed[alias] = product.product
                root = self._alias_root_safe(alias)
                if root is not None:
                    self.alias_to_root[alias] = root
                else:
                    self.findings.append(f"repo alias '{alias}' does not resolve to a directory "
                                         f"under {self.projects_root}")

    def _discover_builder_home(self) -> None:
        assert self.builder_home is not None
        self.findings.extend(lint_loaded_home(self.builder_home))
        canonical_ids = {project.id for project in self.builder_home.projects}
        for project in self.builder_home.projects:
            default_repo_id = project.declaration.default_repo or (project.declaration.repos[0].repo_id if project.declaration.repos else "")
            home_repo = self.builder_home.repo_roots_by_id.get(default_repo_id, self.home_repo)
            aliases = [entry.alias for entry in project.declaration.repos]
            self.products.append(Product(project.id, project.declaration.title or project.id, aliases, home_repo, project.manifest_path, []))
        self.home_project_id = self.product_context
        if self.home_project_id is None:
            chosen = self.builder_home.default_project_for_repo(self.home_repo)
            if chosen is not None:
                self.home_project_id = chosen.id
        if self.home_project_id is not None:
            project = self.builder_home.project(self.home_project_id)
            if project is not None:
                for entry in project.declaration.repos:
                    root = self.builder_home.repo_roots_by_id.get(entry.repo_id)
                    if root is not None:
                        self.alias_to_root[entry.alias] = root
        # Standalone legacy discovery remains visible for repos/projects not assigned canonically.
        claimed = {product.product for product in self.products}
        for product_file in sorted(
            runtime_dir(project_dir) / "product.yaml"
            for project_dir in self.projects_root.iterdir()
            if project_dir.is_dir() and (runtime_dir(project_dir) / "product.yaml").is_file()
        ):
            product = parse_product(product_file)
            if not product.product:
                continue
            if product.product in claimed:
                # Canonical products are already loaded from the home-projects loop above, and
                # lint_loaded_home() (called at the top of this method) reports genuine orphans via
                # the refined home.py check (a repo whose legacy owner project disowns it). Re-flagging
                # every canonical product's legacy product.yaml here was a false positive for any
                # --root-scoped command run outside a home-project context (home_project_id is None) —
                # it BLOCKED the whole portfolio. So we only skip re-adding it as a standalone product.
                continue
            self.products.append(product)
            self.findings.extend(product.parse_errors)
            if self.home_project_id is None or self.home_project_id == product.product:
                for alias in product.repo_aliases:
                    root = self._alias_root_safe(alias)
                    if root is not None and alias not in self.alias_to_root:
                        self.alias_to_root[alias] = root

    def _alias_root_safe(self, alias: str) -> Path | None:
        if not ALIAS_RE.match(alias):
            return None
        candidate = (self.projects_root / alias).resolve()
        try:
            candidate.relative_to(self.projects_root)  # containment: reject any escape
        except ValueError:
            return None
        if candidate.parent != self.projects_root or not candidate.is_dir():
            return None
        return candidate

    def resolve(self, ref: SpecRef) -> tuple[Path | None, str | None]:
        """Return (repo_root, error). A bare ref resolves to the home repo. A cross-repo ref
        resolves only through a declared, contained alias."""
        if self.builder_home is not None and self.home_project_id is not None:
            project = self.builder_home.project(self.home_project_id)
            if project is not None:
                if ref.alias is None:
                    default_repo_id = project.declaration.default_repo
                    if default_repo_id is None:
                        return None, f"bare spec ref requires default_repo in canonical project {self.home_project_id!r}"
                    root = self.builder_home.repo_roots_by_id.get(default_repo_id)
                    return (root, None) if root is not None else (None, f"unknown repo_id {default_repo_id!r}")
                root = self.builder_home.resolve_project_alias(self.home_project_id, ref.alias)
                if root is None:
                    return None, f"unknown_repo_alias: '{ref.alias}' (not declared in canonical project {self.home_project_id})"
                if not _safe_spec_id(ref.spec_id):
                    return None, f"unsafe spec id: {ref.spec_id!r}"
                return root, None
        if ref.alias is None:
            return self.home_repo, None
        root = self.alias_to_root.get(ref.alias)
        if root is None:
            return None, f"unknown_repo_alias: '{ref.alias}' (not declared in any product.yaml)"
        if not _safe_spec_id(ref.spec_id):
            return None, f"unsafe spec id: {ref.spec_id!r}"
        return root, None

    def spec_dir(self, ref: SpecRef) -> tuple[Path | None, str | None]:
        root, err = self.resolve(ref)
        if err:
            return None, err
        root = root.resolve()
        specs_root = runtime_dir(root) / "specs"
        # Live spec, else its archived form. Falling back to the LIVE path when neither exists
        # keeps the caller's "no spec dir at <path>" diagnostic pointing at where the spec
        # should be, rather than at an archive location it was never in.
        located = resolve_spec_dir(specs_root, ref.spec_id) or (specs_root / ref.spec_id)
        target = located.resolve()
        # Contain against the REAL repo root, not against the resolved specs dir. If
        # `.builder/specs` (or the spec dir itself) is a symlink to somewhere outside the
        # repo, resolving the specs dir first would happily accept the target as "inside" it.
        # An agent runs as the same OS user and can plant such a symlink, so containment must be
        # judged against the repo the ref names -- specs must physically live under it.
        try:
            target.relative_to(root)
        except ValueError:
            return None, f"spec ref resolves outside its repo (symlink escape?): {ref.canonical!r}"
        specs_real = (runtime_dir(root) / "specs").resolve()
        try:
            target.relative_to(specs_real)
        except ValueError:
            return None, f"spec ref escapes the specs dir: {ref.canonical!r}"
        # Defense in depth: refuse a spec dir (or a symlinked specs/ parent) that is a symlink at
        # all. This makes every STATIC symlink case a hard reject and narrows the TOCTOU window.
        # Full TOCTOU safety (O_NOFOLLOW/openat) is deliberately out of scope: planning.py is a
        # READ-ONLY reporting tool run outside any agent lane, the only "attacker" is a process
        # running as the SAME OS user with full disk read already, and a won race lets it read a
        # spec.yaml-shaped file it could `cat` directly -- it crosses no privilege boundary and
        # cannot move the number (the numerator is gate-coverage's, not this path's). Same honest
        # posture as the evidence bundles: bounded by provenance, not by a lock we cannot hold.
        specs_link = runtime_dir(root) / "specs"
        if specs_link.is_symlink() or target.is_symlink():
            return None, f"spec ref path is a symlink; refused: {ref.canonical!r}"
        return target, None


# --------------------------------------------------------------------------- parsers

def parse_product(path: Path) -> Product:
    home_repo = path.resolve().parents[1]  # <repo>/.builder/product.yaml -> <repo>
    data = _safe_load(path)
    errors: list[str] = []
    if not isinstance(data, dict):
        return Product("", "", [], home_repo, path, [f"{path}: not a mapping / unreadable"])
    product = str(data.get("product", "")).strip()
    if not ALIAS_RE.match(product):
        errors.append(f"{path}: product name is not [a-z0-9][a-z0-9-]*: {product!r}")
    aliases: list[str] = []
    repos = data.get("repos")
    for entry in repos if isinstance(repos, list) else []:
        alias = entry.get("alias") if isinstance(entry, dict) else entry
        if isinstance(alias, str) and ALIAS_RE.match(alias.strip()):
            aliases.append(alias.strip())
        else:
            errors.append(f"{path}: repo alias invalid: {alias!r}")
    if not aliases:
        errors.append(f"{path}: product declares no repos")
    return Product(product, str(data.get("title", product)), aliases, home_repo, path, errors)


def parse_release(path: Path, home_repo: Path) -> Release:
    data = _safe_load(path)
    errors: list[str] = []
    release_id = path.stem
    if not isinstance(data, dict):
        return Release(release_id, "", "", "", "draft", [], (), None, path, home_repo,
                       [f"{path}: not a mapping / unreadable"])
    declared_id = str(data.get("release", release_id)).strip()
    if declared_id and declared_id != release_id:
        errors.append(f"{path}: release id '{declared_id}' != filename '{release_id}'")
    status = str(data.get("status", "draft")).strip().lower()
    if status not in RELEASE_STATUSES:
        errors.append(f"{path}: unknown status {status!r} (allowed: {', '.join(RELEASE_STATUSES)})")
        status = "draft"
    specs: list[SpecRef] = []
    intents: list[str] = []
    raw_specs = data.get("specs")
    raw_intents = data.get("intents")
    seen: set[str] = set()
    if raw_specs is not None and raw_intents is not None:
        errors.append(f"{path}: release may declare only one membership field: specs or intents")
    if release_uses_intents(status):
        if raw_specs is not None:
            errors.append(f"{path}: live releases must not declare specs")
        if not isinstance(raw_intents, list):
            errors.append(f"{path}: intents must be a list")
        elif not raw_intents:
            errors.append(f"{path}: intents must be a non-empty list")
        else:
            for index, raw in enumerate(raw_intents):
                intent_id = str(raw).strip() if isinstance(raw, str) else ""
                if not ALIAS_RE.match(intent_id):
                    errors.append(f"{path}: intents[{index}] must match [a-z0-9][a-z0-9-]*")
                    continue
                if intent_id in seen:
                    errors.append(f"{path}: duplicate intent {intent_id!r}")
                    continue
                seen.add(intent_id)
                intents.append(intent_id)
    else:
        if raw_intents is not None:
            errors.append(f"{path}: historical releases must not declare intents")
        for raw in raw_specs if isinstance(raw_specs, list) else []:
            ref, err = parse_spec_ref(raw)
            if err:
                errors.append(f"{path}: {err}")
                continue
            if ref.canonical in seen:
                errors.append(f"{path}: duplicate member {ref.canonical!r}")
                continue
            seen.add(ref.canonical)
            specs.append(ref)
        if raw_specs is not None and not isinstance(raw_specs, list):
            errors.append(f"{path}: specs must be a list")
        if status in HISTORICAL_RELEASE_STATUSES and not specs:
            errors.append(f"{path}: historical releases must declare a non-empty specs list")
    return Release(
        release_id=release_id,
        product=str(data.get("product", "")).strip(),
        title=str(data.get("title", release_id)),
        goal=str(data.get("goal", "")).strip(),
        status=status,
        specs=specs,
        intents=tuple(intents),
        adopted_intents=tuple(
            str(a.get("intent")).strip()
            for a in (data.get("adopted_intents") or [])
            if isinstance(a, dict) and str(a.get("intent") or "").strip()
        ),
        scope_ratified_at=(str(data.get("scope_ratified_at")).strip()
                           if data.get("scope_ratified_at") else None),
        path=path,
        home_repo=home_repo,
        parse_errors=errors,
    )


def _home_release_to_release(product_id: str, home_repo: Path, release_path: Path, release) -> Release:
    specs = []
    for member in release.specs:
        ref, err = parse_spec_ref({"spec": member.spec, "weight": member.weight})
        if err or ref is None:
            continue
        specs.append(ref)
    adopted_intents: tuple[str, ...] = ()
    try:
        raw = _safe_load(release_path)
        if isinstance(raw, dict):
            adopted_intents = tuple(
                str(a.get("intent")).strip()
                for a in (raw.get("adopted_intents") or [])
                if isinstance(a, dict) and str(a.get("intent") or "").strip()
            )
    except Exception:
        adopted_intents = ()
    return Release(
        release_id=release.name,
        product=product_id,
        title=release.name,
        goal=release.description,
        status=release.status,
        specs=specs,
        intents=release.intents,
        scope_ratified_at=None,
        path=release_path,
        home_repo=home_repo,
        parse_errors=[],
        adopted_intents=adopted_intents,
    )


def load_releases(home_repo: Path, product_context: str | None = None) -> list[Release]:
    try:
        builder_home = load_optional_home(start=home_repo)
    except ValidationError:
        builder_home = None
    if builder_home is not None:
        project = builder_home.project(product_context) if product_context else builder_home.default_project_for_repo(home_repo.resolve())
        if project is not None:
            default_repo_id = project.declaration.default_repo or (project.declaration.repos[0].repo_id if project.declaration.repos else "")
            canonical_home_repo = builder_home.repo_roots_by_id.get(default_repo_id, home_repo.resolve())
            return [
                _home_release_to_release(project.id, canonical_home_repo, release.manifest_path, release.declaration)
                for release in project.releases
            ]
    releases_dir = runtime_dir(home_repo) / "releases"
    if not releases_dir.is_dir():
        return []
    out: list[Release] = []
    for f in sorted(releases_dir.glob("*.yaml")) + sorted(releases_dir.glob("*.yml")):
        out.append(parse_release(f, home_repo))
    return out


def find_release(home_repo: Path, release_id: str, product_context: str | None = None) -> Release | None:
    for rel in load_releases(home_repo, product_context=product_context):
        if rel.release_id == release_id:
            return rel
    return None


# --------------------------------------------------------------------------- the completeness metric

@dataclass
class MemberStatus:
    ref: SpecRef
    verification: str
    resolved: bool
    error: str | None = None
    weight: int = 1
    status: str | None = None


@dataclass
class IntentStatus:
    intent_id: str
    title: str
    declared_status: str | None
    visible_state: str
    members: list[MemberStatus]
    resolved: bool
    error: str | None = None
    path: str | None = None


@dataclass
class Completeness:
    release_id: str
    members: list[MemberStatus]
    verified: int
    total: int
    dangling: int
    planned: int
    claimed_states: dict[str, int] = field(default_factory=dict)
    intents: list[IntentStatus] = field(default_factory=list)
    adopted: int = 0  # intents counted fulfilled via owner-adoption (disclosed separately)

    @property
    def fraction(self) -> str:
        return f"{self.verified}/{self.total}"

    @property
    def percent(self) -> int:
        return round(100 * self.verified / self.total) if self.total else 0


def validate_backlog_target(raw: str) -> str:
    return validate_intent_target(raw, "backlog target")


def _active_release_inventory(home_repo: Path, product_context: str | None = None) -> list[Release]:
    return [
        release for release in load_releases(home_repo, product_context=product_context)
        if release.status in LIVE_RELEASE_STATUSES
    ]


def _scoped_home_release_inventory(
    home_repo: Path,
    registry: Registry,
    product_context: str | None,
) -> tuple[list[Release] | None, list[str]]:
    """Recover only the selected canonical project's releases after a home-wide error.

    ``load_builder_home`` validates every project together, so an unrelated broken
    project can make the optional-home load fail.  Backlog queries must neither
    inherit that unrelated failure nor silently lose a selected project's releases.
    This fallback reuses the canonical parsers and resolves just the project that
    owns the invoking repository.
    """
    try:
        home_dir = resolve_home_dir(
            start=home_repo, projects_root=registry.projects_root
        )
    except ValidationError:
        return None, []
    if home_dir is None:
        return None, []
    try:
        builder = parse_builder_manifest(home_dir / "builder.yaml")
        catalog = parse_repositories_manifest(builder.repositories)
    except (OSError, ValidationError):
        return None, []

    home_real = home_repo.resolve()
    repo_ids = {entry.id for entry in catalog.repos if entry.path.resolve() == home_real}
    if not repo_ids:
        return None, []

    parsed_projects = []
    selected_errors: list[str] = []
    for project_ref in builder.projects:
        if product_context is not None and project_ref.id != product_context:
            continue
        try:
            project = parse_project_manifest(project_ref.manifest)
        except (OSError, ValidationError) as exc:
            if product_context == project_ref.id and isinstance(exc, ValidationError):
                selected_errors.extend(issue.render() for issue in exc.issues)
            continue
        if any(entry.repo_id in repo_ids for entry in project.repos):
            parsed_projects.append((project_ref, project))

    if selected_errors:
        return [], selected_errors
    if not parsed_projects:
        return None, []
    if len(parsed_projects) > 1:
        default_matches = [
            item for item in parsed_projects if item[1].default_repo in repo_ids
        ]
        if len(default_matches) != 1:
            return [], [f"cannot select one canonical project for {home_repo}"]
        selected_ref, selected_project = default_matches[0]
    else:
        selected_ref, selected_project = parsed_projects[0]

    default_repo_id = selected_project.default_repo or (
        selected_project.repos[0].repo_id if selected_project.repos else ""
    )
    canonical_home_repo = next(
        (entry.path.resolve() for entry in catalog.repos if entry.id == default_repo_id),
        home_real,
    )
    releases: list[Release] = []
    findings: list[str] = []
    for release_ref in selected_project.releases:
        try:
            declaration = parse_release_manifest(
                release_ref.manifest, project=selected_project
            )
        except OSError as exc:
            findings.append(f"{release_ref.manifest}: unreadable release ({exc})")
            continue
        except ValidationError as exc:
            findings.extend(issue.render() for issue in exc.issues)
            continue
        releases.append(
            _home_release_to_release(
                selected_ref.id,
                canonical_home_repo,
                release_ref.manifest,
                declaration,
            )
        )
    return releases, findings


def active_backlog_capability_index(
    home_repo: Path,
    registry: Registry | None = None,
    *,
    product_context: str | None = None,
) -> tuple[dict[str, BacklogCapabilityOwners], list[str]]:
    registry = registry or _registry(home_repo, projects_root=None)
    active_releases = _active_release_inventory(home_repo, product_context=product_context)
    scoped_findings: list[str] = []
    if registry.builder_home is None:
        scoped_releases, scoped_findings = _scoped_home_release_inventory(
            home_repo, registry, product_context
        )
        if scoped_releases is not None:
            active_releases = [
                release
                for release in scoped_releases
                if release.status in LIVE_RELEASE_STATUSES
            ]
    # Canonical Builder Home releases carry the project's default repository as
    # their ownership root.  The command may have been invoked from another repo
    # in that project, so load intent objects from the resolved release home rather
    # than blindly from the CLI's --root directory.
    intent_repo = active_releases[0].home_repo if active_releases else home_repo
    referenced_intent_ids = {
        intent_id
        for release in active_releases
        for intent_id in release.intents
    }
    visible, diagnostics = intent_inventory(intent_repo, registry)
    visible_by_id = {
        item.intent.intent: item for item in visible if item.intent is not None
    }
    findings = [*registry.findings, *scoped_findings]
    # The backlog is the selected active release inventory, not every intent file
    # that happens to remain in the repository.  In particular, an old malformed
    # intent object that is not reachable from a draft/active release must not make
    # active queries fail.  An inventory-level diagnostic (for example an unreadable
    # .builder/intents directory) has no intent-id path component and remains global.
    for diagnostic in diagnostics:
        path = Path(diagnostic.path)
        diagnostic_intent_id = path.parent.name if path.name == "intent.yaml" else None
        if diagnostic_intent_id is not None and diagnostic_intent_id not in referenced_intent_ids:
            continue
        if diagnostic.findings:
            findings.append(
                f"{diagnostic.path}: {'; '.join(diagnostic.findings)}"
                if len(diagnostic.findings) > 1
                else diagnostic.findings[0]
            )
    by_target: dict[str, list[BacklogCapabilityOwner]] = {}
    for release in active_releases:
        release_findings = list(release.parse_errors)
        if release_findings:
            findings.extend(release_findings)
            continue
        for intent_id in release.intents:
            visible_intent = visible_by_id.get(intent_id)
            if visible_intent is None or visible_intent.intent is None:
                findings.append(f"{release.release_id}: missing backlog intent {intent_id!r}")
                continue
            if visible_intent.visible_state in {"fulfilled", "rejected", "superseded"}:
                continue
            if visible_intent.findings:
                findings.append(
                    f"{release.release_id}: intent {intent_id}: {'; '.join(visible_intent.findings)}"
                )
                continue
            for delta in visible_intent.intent.ssot_delta["capabilities"]:
                owner = BacklogCapabilityOwner(
                    target=delta.target,
                    change=delta.change,
                    intent_id=intent_id,
                    release_id=release.release_id,
                    visible_state=visible_intent.visible_state,
                    intent_path=visible_intent.path,
                )
                by_target.setdefault(delta.target, []).append(owner)
    index = {
        target: BacklogCapabilityOwners(
            rows=tuple(rows),
            collision_intent_ids=tuple(sorted({row.intent_id for row in rows})),
        )
        for target, rows in by_target.items()
    }
    return index, findings


def backlog_capability_owners(
    home_repo: Path,
    target: str,
    registry: Registry | None = None,
    *,
    product_context: str | None = None,
) -> tuple[list[BacklogCapabilityOwner], list[str]]:
    canonical_target = validate_backlog_target(target)
    index, diagnostics = active_backlog_capability_index(
        home_repo, registry, product_context=product_context
    )
    owners = index.get(canonical_target)
    return list(owners.rows) if owners else [], diagnostics


def adoption_satisfied(
    intent_id: str,
    adopted_intents,
    members,
    has_findings: bool,
    terminal_reference: bool,
) -> bool:
    """Release-level owner-adoption precondition. An accepted intent counts fulfilled-by-adoption
    ONLY when the release explicitly declares it adopted AND every member is host-verified (or
    synced) — i.e. the work is genuinely done, merely lacking reconstructable spec-level sync
    provenance. Refuses to adopt un-done work, findings, or terminal references. Disclosed as
    `adopted` in release status; never fakes spec-level sync artifacts."""
    if intent_id not in adopted_intents:
        return False
    if has_findings or terminal_reference:
        return False
    if not members:
        return False
    return all((m.verification or "") in {HOST_VERIFIED, SYNCED} for m in members)


def completeness(release: Release, registry: Registry) -> Completeness:
    """verified / manifest specs. `verified` counts ONLY host-verified members (gate-coverage's
    stamp). A member that fails to resolve is dangling and counts toward the denominator but never
    the numerator — a plan referencing a spec that does not exist is not 'done', it is broken."""
    if release_uses_intents(release.status):
        inventory, diagnostics = intent_inventory(release.home_repo, registry)
        visible_by_id = {
            item.intent.intent: item
            for item in inventory
            if item.intent is not None
        }
        diagnostic_by_path = {item.path: item for item in diagnostics}
        members: list[MemberStatus] = []
        intent_statuses: list[IntentStatus] = []
        verified = dangling = planned = adopted = 0
        claimed_states: dict[str, int] = {}
        for intent_id in release.intents:
            visible = visible_by_id.get(intent_id)
            if visible is None:
                relpath = f".builder/intents/{intent_id}/intent.yaml"
                finding = diagnostic_by_path.get(relpath)
                dangling += 1
                error = finding.findings[0] if finding and finding.findings else f"missing intent object {relpath}"
                intent_statuses.append(
                    IntentStatus(intent_id, intent_id, None, "missing", [], False, error, relpath)
                )
                continue
            assert visible.intent is not None
            intent_members: list[MemberStatus] = []
            for member in visible.members:
                ref, ref_error = parse_spec_ref(member.canonical_ref)
                if ref is None:
                    ref = SpecRef(alias=None, spec_id=member.canonical_ref, raw=member.canonical_ref)
                converted = MemberStatus(
                    ref=ref,
                    verification=member.verification or UNKNOWN,
                    resolved=member.resolved and ref_error is None,
                    error=member.finding or ref_error,
                    status=member.status,
                )
                intent_members.append(converted)
                members.append(converted)
            terminal_reference = visible.intent.status in {"rejected", "superseded"}
            ownership_error = (
                f"release references {visible.intent.status} intent {intent_id!r}; remove or replace it"
                if terminal_reference else None
            )
            visible_error = "; ".join((*visible.findings, *((ownership_error,) if ownership_error else ()))) or None
            visible_state = visible.visible_state
            # Owner-adoption (release-level reconciliation): an accepted intent whose members are
            # ALL host-verified but not synced counts fulfilled iff the release declares it adopted.
            # Refuses to adopt un-done work (requires every member host-verified). Disclosed as `adopted`.
            adopted_ok = visible_state != "fulfilled" and adoption_satisfied(
                intent_id, release.adopted_intents, intent_members,
                bool(visible.findings), terminal_reference,
            )
            claimed_states[visible_state] = claimed_states.get(visible_state, 0) + 1
            if visible.visible_state == "fulfilled":
                verified += 1
            elif adopted_ok:
                verified += 1
                adopted += 1
            elif visible.visible_state in {"accepted", "decomposed"}:
                planned += 1
            if visible.findings or terminal_reference:
                dangling += 1
            intent_statuses.append(
                IntentStatus(
                    intent_id=intent_id,
                    title=visible.intent.title,
                    declared_status=visible.intent.status,
                    visible_state=visible_state,
                    members=intent_members,
                    resolved=not bool(visible.findings) and not terminal_reference,
                    error=visible_error,
                    path=visible.path,
                )
            )
        return Completeness(
            release.release_id, members, verified, len(release.intents), dangling, planned,
            claimed_states, intent_statuses, adopted=adopted,
        )
    members: list[MemberStatus] = []
    verified = dangling = planned = 0
    for ref in release.specs:
        spec_dir, err = registry.spec_dir(ref)
        if err or spec_dir is None or not spec_dir.is_dir():
            dangling += 1
            members.append(MemberStatus(ref, UNKNOWN, resolved=False,
                                        error=err or "spec dir not found", weight=ref.weight))
            continue
        spec_data = _safe_load(spec_dir / "spec.yaml")
        spec_status = (str(spec_data.get("status", "")).strip().lower()
                       if isinstance(spec_data, dict) else "")
        if spec_status in PRE_IMPLEMENTATION_STATUSES:
            planned += 1
            members.append(MemberStatus(ref, PLANNED, resolved=True, weight=ref.weight, status=spec_status))
            continue
        repo_root, _ = registry.resolve(ref)
        verification = _spec_verification(repo_root, ref.spec_id)
        if verification == HOST_VERIFIED:
            if not (spec_dir / "ssot-delta.yaml").is_file():
                # Historical/pre-sync specs retain the legacy host-verified contract.
                verified += 1
            else:
                visible = sync_visibility(spec_dir)
                if visible == SYNCED:
                    verified += 1
                    verification = SYNCED
                elif visible == VERIFIED_AWAITING_SYNC:
                    verification = VERIFIED_AWAITING_SYNC
                else:
                    verification = UNKNOWN
            members.append(MemberStatus(ref, verification, resolved=True, weight=ref.weight, status=spec_status))
            continue
        members.append(MemberStatus(
            ref, verification,
            resolved=True, weight=ref.weight, status=spec_status,
        ))
    return Completeness(release.release_id, members, verified, len(release.specs), dangling, planned, {}, [])


def intent_inventory(repo_root: Path, registry: Registry | None = None) -> tuple[list[VisibleIntent], list[IntentFileDiagnostic]]:
    registry = registry or _registry(repo_root, projects_root=None)
    intents, diagnostics = load_repo_intents(repo_root, parse_spec_ref)
    visible: list[VisibleIntent] = []
    for intent in intents:
        members: list[IntentMemberState] = []
        for canonical_ref in intent.specs:
            ref, err = parse_spec_ref(canonical_ref)
            if err or ref is None:
                members.append(IntentMemberState(
                    ref=canonical_ref,
                    canonical_ref=canonical_ref,
                    resolved=False,
                    status=None,
                    verification=None,
                    finding=f"{intent.repo_relpath}: invalid member ref {canonical_ref!r}",
                ))
                continue
            spec_dir, resolve_error = registry.spec_dir(ref)
            if resolve_error or spec_dir is None or not spec_dir.is_dir():
                members.append(IntentMemberState(
                    ref=canonical_ref,
                    canonical_ref=canonical_ref,
                    resolved=False,
                    status=None,
                    verification=None,
                    finding=f"{intent.repo_relpath}: dangling member {canonical_ref}: {resolve_error or 'spec dir not found'}",
                ))
                continue
            spec_data = _safe_load(spec_dir / "spec.yaml")
            status = str(spec_data.get("status", "")).strip() if isinstance(spec_data, dict) else ""
            if status not in PRE_IMPLEMENTATION_STATUSES and status not in IMPLEMENTATION_OR_LATER_STATUSES:
                members.append(IntentMemberState(
                    ref=canonical_ref,
                    canonical_ref=canonical_ref,
                    resolved=True,
                    status=status or None,
                    verification=None,
                    finding=f"{intent.repo_relpath}: member {canonical_ref} has unrecognized status {status!r}",
                ))
                continue
            repo_for_ref, _ = registry.resolve(ref)
            verification = _spec_verification(repo_for_ref, ref.spec_id)
            members.append(IntentMemberState(
                ref=canonical_ref,
                canonical_ref=canonical_ref,
                resolved=True,
                status=status,
                verification=verification,
            ))
        visible.append(project_visible_state(intent, members))
    return visible, diagnostics


# --------------------------------------------------------------------------- lint

def lint_release(release: Release, registry: Registry) -> list[str]:
    findings = list(release.parse_errors)
    if release_uses_intents(release.status):
        comp = completeness(release, registry)
        seen_specs: dict[str, str] = {}
        for intent in comp.intents:
            if intent.error:
                findings.append(f"{release.release_id}: intent {intent.intent_id}: {intent.error}")
            for member in intent.members:
                prior = seen_specs.get(member.ref.canonical)
                if prior is not None:
                    findings.append(
                        f"{release.release_id}: spec {member.ref.canonical!r} is owned by both "
                        f"intents {prior!r} and {intent.intent_id!r}"
                    )
                else:
                    seen_specs[member.ref.canonical] = intent.intent_id
        return findings
    for ref in release.specs:
        spec_dir, err = registry.spec_dir(ref)
        if err:
            findings.append(f"{release.release_id}: {ref.canonical}: {err}")
        elif spec_dir is None or not spec_dir.is_dir():
            findings.append(f"{release.release_id}: dangling ref {ref.canonical!r} "
                            f"(no spec dir at {spec_dir})")
    return findings


def cross_repo_cycles(releases: list[Release], registry: Registry) -> list[list[str]]:
    """Build the cross-repo dependency DAG from each member spec's dependencies.yaml and report
    cycles the per-repo detector cannot see (repo-a -> repo-b -> repo-a). Nodes are canonical
    `<alias>/<spec-id>` refs."""
    graph: dict[str, set[str]] = {}
    for release in releases:
        refs = release.specs
        if release_uses_intents(release.status):
            refs = [member.ref for member in completeness(release, registry).members]
        for ref in refs:
            node = ref.canonical
            graph.setdefault(node, set())
            spec_dir, err = registry.spec_dir(ref)
            if err or spec_dir is None:
                continue
            deps = _safe_load(spec_dir / "dependencies.yaml")
            for dep in (deps.get("dependencies", []) if isinstance(deps, dict) else []):
                dref_raw = dep.get("spec") if isinstance(dep, dict) else dep
                dref, derr = parse_spec_ref(dref_raw)
                if derr or dref is None:
                    continue
                # a bare dep in a cross-repo member is same-repo; qualify it with the member's alias
                target = dref.canonical if dref.alias else (
                    f"{ref.alias}/{dref.spec_id}" if ref.alias else dref.spec_id)
                graph[node].add(target)
    return _find_cycles(graph)


def _find_cycles(graph: dict[str, set[str]]) -> list[list[str]]:
    cycles: list[list[str]] = []
    WHITE, GREY, BLACK = 0, 1, 2
    color: dict[str, int] = {n: WHITE for n in graph}

    def visit(node: str, stack: list[str]) -> None:
        color[node] = GREY
        stack.append(node)
        for nxt in sorted(graph.get(node, ())):
            if nxt not in color:
                color[nxt] = WHITE
                graph.setdefault(nxt, set())
            if color[nxt] == GREY:
                i = stack.index(nxt)
                cycles.append(stack[i:] + [nxt])
            elif color[nxt] == WHITE:
                visit(nxt, stack)
        stack.pop()
        color[node] = BLACK

    for node in sorted(graph):
        if color.get(node, WHITE) == WHITE:
            visit(node, [])
    return cycles


# --------------------------------------------------------------------------- CLI

def _home_repo(root: str | None) -> Path:
    return Path(root).resolve() if root else Path.cwd().resolve()


def _registry(home_repo: Path, projects_root: str | None) -> Registry:
    pr = Path(projects_root).resolve() if projects_root else default_projects_root(home_repo)
    return Registry(pr, home_repo)


def cmd_release_status(args) -> int:
    home = _home_repo(args.root)
    registry = _registry(home, args.projects_root)
    releases = load_releases(home)
    blocking_findings = list(registry.findings)
    if args.release_id:
        releases = [r for r in releases if r.release_id == args.release_id]
        if not releases:
            for finding in blocking_findings:
                print(f"BLOCKED  {finding}", file=sys.stderr)
            print(f"no release '{args.release_id}' under {runtime_dir(home) / 'releases'}", file=sys.stderr)
            return 1 if blocking_findings else 2
    if not releases:
        if blocking_findings:
            for finding in blocking_findings:
                print(f"BLOCKED  {finding}", file=sys.stderr)
            return 1
        print(f"no releases under {runtime_dir(home) / 'releases'} (this repo is its own product; add "
              f"release files to plan forward).")
        return 0
    for release in releases:
        comp = completeness(release, registry)
        seg = _segments(comp)
        print(f"\n{release.title}   [{release.status}]")
        unit = "intents fulfilled" if release_uses_intents(release.status) else "specs host-verified"
        adopted_note = f"  ({comp.adopted} by owner-adoption)" if comp.adopted else ""
        print(f"  COMPLETENESS  {comp.fraction} {unit}{adopted_note}  ({comp.percent}%)   {seg}")
        if comp.dangling:
            print(f"  ⚠ {comp.dangling} dangling ref(s) — counted against the denominator, never done")
        release_findings = list(release.parse_errors)
        release_findings.extend(
            f"{release.release_id}: intent {intent.intent_id}: {intent.error}"
            for intent in comp.intents
            if intent.error
        )
        for finding in release_findings:
            print(f"  BLOCKED  {finding}", file=sys.stderr)
        blocking_findings.extend(release_findings)
        if args.verbose:
            if comp.intents:
                for intent in comp.intents:
                    extra = f"  ({intent.error})" if intent.error else ""
                    print(f"      · {intent.intent_id:<40} {intent.visible_state}{extra}")
                    for member in intent.members:
                        verdict = member.verification or UNKNOWN
                        print(
                            f"          {member.ref.canonical:<36} "
                            f"status={member.status or 'unknown'} verification={verdict}"
                        )
            else:
                for m in comp.members:
                    mark = {HOST_VERIFIED: "✓", PLANNED: "·", SELF_REPORTED: "~", UNKNOWN: "?"}.get(
                        m.verification, "?")
                    extra = "" if m.resolved else f"  ({m.error})"
                    print(f"      {mark} {m.ref.canonical:<40} {m.verification}{extra}")
    return 1 if blocking_findings else 0


def _segments(comp: Completeness) -> str:
    if comp.intents:
        return (
            f"[fulfilled {comp.claimed_states.get('fulfilled', 0)} · "
            f"in-flight {comp.claimed_states.get('in-flight', 0)} · "
            f"decomposed {comp.claimed_states.get('decomposed', 0)} · "
            f"accepted {comp.claimed_states.get('accepted', 0)} · "
            f"blocked {comp.dangling}]"
        )
    sync_era = any(m.verification in {SYNCED, VERIFIED_AWAITING_SYNC, PLANNED_DECOMPOSING} for m in comp.members)
    if not sync_era:
        host_verified = sum(1 for m in comp.members if m.verification == HOST_VERIFIED)
        planned = sum(1 for m in comp.members if m.verification == PLANNED)
        self_reported = sum(1 for m in comp.members if m.verification == SELF_REPORTED)
        unknown = comp.total - host_verified - planned - self_reported
        return (
            f"[host-verified {host_verified} · planned {planned} · "
            f"self-reported {self_reported} · unknown {unknown}]"
        )
    synced = sum(1 for m in comp.members if m.verification == SYNCED)
    awaiting = sum(1 for m in comp.members if m.verification == VERIFIED_AWAITING_SYNC)
    planned = sum(1 for m in comp.members if m.verification in {PLANNED, PLANNED_DECOMPOSING})
    self_reported = sum(1 for m in comp.members if m.verification == SELF_REPORTED)
    unknown = comp.total - synced - awaiting - planned - self_reported
    return (
        f"[synced {synced} · verified-awaiting-sync {awaiting} · planned-decomposing {planned} · "
        f"self-reported {self_reported} · unknown {unknown}]"
    )


def _template_data(name: str) -> dict[str, Any]:
    data = _safe_load(TEMPLATES / name)
    if not isinstance(data, dict):
        raise RuntimeError(f"planning template is missing or invalid: {TEMPLATES / name}")
    return data


def _intent_stub(intent_id: str, title: str, spec_ids: list[str]) -> dict[str, Any]:
    return {
        "artifact": "intent-object",
        "intent": intent_id,
        "title": title,
        "status": "accepted",
        "problem": f"Track release {title}.",
        "why": "Keep live release membership canonical via intent objects.",
        "success_criteria": [
            {"id": "SC-1", "statement": "All scoped release members are represented in this intent."}
        ],
        "non_goals": ["Change release scope beyond the authored spec list."],
        "ssot_delta": {"capabilities": [], "behaviors": [], "journeys": []},
        "specs": spec_ids,
    }


def cmd_release_create(args) -> int:
    home = _home_repo(args.root)
    release_id = str(args.release_id).strip()
    if not ALIAS_RE.match(release_id):
        print(f"release id is not [a-z0-9][a-z0-9-]*: {release_id!r}", file=sys.stderr)
        return 2

    raw_specs = str(args.specs or "")
    spec_ids = [item.strip() for item in raw_specs.split(",")] if raw_specs else []
    if any(not _safe_spec_id(spec_id) for spec_id in spec_ids):
        bad = next(spec_id for spec_id in spec_ids if not _safe_spec_id(spec_id))
        print(f"spec id is unsafe (empty, '.', '..', or contains a separator): {bad!r}",
              file=sys.stderr)
        return 2
    spec_ids = list(dict.fromkeys(spec_ids))
    raw_intents = str(args.intents or "")
    intent_ids = [item.strip() for item in raw_intents.split(",")] if raw_intents else []
    if not intent_ids and spec_ids:
        intent_ids = [f"{release_id}-intent"]
    if not intent_ids or any(not ALIAS_RE.match(intent_id) for intent_id in intent_ids):
        print("draft releases require --intents with one or more comma-separated intent ids", file=sys.stderr)
        return 2
    intent_ids = list(dict.fromkeys(intent_ids))

    repo_alias = home.name
    product_name = str(args.product or repo_alias).strip()
    if not ALIAS_RE.match(product_name):
        print(f"product name is not [a-z0-9][a-z0-9-]*: {product_name!r}", file=sys.stderr)
        return 2
    if not ALIAS_RE.match(repo_alias):
        print(f"repo directory name cannot be used as an alias: {repo_alias!r}", file=sys.stderr)
        return 2

    builder_dir = runtime_dir(home)
    releases_dir = builder_dir / "releases"
    specs_dir = builder_dir / "specs"
    intents_dir = builder_dir / "intents"
    for directory in (builder_dir, releases_dir, specs_dir, intents_dir):
        if directory.is_symlink():
            print(f"refusing to scaffold through a symlinked directory: {directory}", file=sys.stderr)
            return 2
    release_path = builder_dir / "releases" / f"{release_id}.yaml"
    if release_path.is_symlink():
        print(f"refusing to write through a symlinked release file: {release_path}", file=sys.stderr)
        return 2
    if release_path.exists():
        print(f"release already exists; refusing to overwrite: {release_path}", file=sys.stderr)
        return 2

    product_path = builder_dir / "product.yaml"
    if product_path.is_symlink():
        print(f"refusing to write through a symlinked product file: {product_path}", file=sys.stderr)
        return 2
    if product_path.exists():
        existing_product = _safe_load(product_path)
        if not args.product and isinstance(existing_product, dict):
            existing_name = str(existing_product.get("product", "")).strip()
            if existing_name:
                product_name = existing_name
        print(f"kept existing product: {product_path}")
    else:
        product = _template_data("product.yaml")
        product.update({"product": product_name, "title": product_name,
                        "repos": [{"alias": repo_alias}]})
        builder_dir.mkdir(parents=True, exist_ok=True)
        product_path.write_text(_yaml().safe_dump(product, sort_keys=False), encoding="utf-8")
        print(f"created product: {product_path}")

    release = _template_data("release.yaml")
    release.update({"release": release_id, "product": product_name,
                    "title": args.title or release_id, "status": "draft", "intents": intent_ids})
    release.pop("specs", None)
    release_path.parent.mkdir(parents=True, exist_ok=True)
    release_path.write_text(_yaml().safe_dump(release, sort_keys=False), encoding="utf-8")
    print(f"created release: {release_path}")

    if spec_ids and len(intent_ids) == 1:
        intent_id = intent_ids[0]
        intent_path = intents_dir / intent_id / "intent.yaml"
        if intent_path.exists() or intent_path.is_symlink():
            print(f"kept existing intent: {intent_id}")
        else:
            intent_path.parent.mkdir(parents=True, exist_ok=True)
            intent_title = args.title or release_id
            intent_path.write_text(
                _yaml().safe_dump(_intent_stub(intent_id, intent_title, spec_ids), sort_keys=False),
                encoding="utf-8",
            )
            print(f"created intent: {intent_id}")

    for spec_id in spec_ids:
        spec_dir = specs_dir / spec_id
        if spec_dir.exists() or spec_dir.is_symlink():
            print(f"kept existing spec: {spec_id}")
            continue
        spec_dir.mkdir(parents=True)
        stub = {"id": spec_id, "title": spec_id, "status": PLANNED}
        (spec_dir / "spec.yaml").write_text(
            _yaml().safe_dump(stub, sort_keys=False), encoding="utf-8")
        print(f"created planned spec: {spec_id}")
    return 0


def cmd_release_lint(args) -> int:
    home = _home_repo(args.root)
    registry = _registry(home, args.projects_root)
    releases = load_releases(home)
    if args.release_id:
        releases = [release for release in releases if release.release_id == args.release_id]
        if not releases:
            print(f"no release '{args.release_id}'", file=sys.stderr)
            return 2
    findings = list(registry.findings)
    for release in releases:
        findings.extend(lint_release(release, registry))
    backlog_index, backlog_diagnostics = active_backlog_capability_index(home, registry)
    findings.extend(backlog_diagnostics)
    for target, owners in backlog_index.items():
        if len(owners.collision_intent_ids) < 2:
            continue
        owner_text = ", ".join(
            f"{row.intent_id} [{row.release_id}] {row.visible_state} {row.change}"
            for row in owners.rows
        )
        findings.append(f"backlog capability collision {target}: {owner_text}")
    for cyc in cross_repo_cycles(releases, registry):
        findings.append("cross-repo dependency cycle: " + " -> ".join(cyc))
    for f in findings:
        print(f, file=sys.stderr)
    if findings:
        print(f"\n{len(findings)} finding(s).", file=sys.stderr)
        return 1
    print(f"release lint: {len(releases)} release(s) clean.")
    return 0


def cmd_release_ship(args) -> int:
    # `shipped` is ALWAYS a human act. The system computes shippable; it never transitions.
    home = _home_repo(args.root)
    release = find_release(home, args.release_id)
    if release is None:
        print(f"no release '{args.release_id}'", file=sys.stderr)
        return 2
    registry = _registry(home, args.projects_root)
    comp = completeness(release, registry)
    if comp.verified < comp.total or comp.dangling:
        print(f"release '{args.release_id}' is {comp.fraction} host-verified"
              f"{f' with {comp.dangling} dangling' if comp.dangling else ''} — NOT shippable.",
              file=sys.stderr)
        print("Shipping is a human judgment; the system will not transition an incomplete release.",
              file=sys.stderr)
        return 1
    data = _safe_load(release.path) or {}
    data["status"] = "shipped"
    if release_uses_intents(release.status):
        data.pop("intents", None)
        data["specs"] = [member.ref.canonical for member in comp.members]
    release.path.write_text(_yaml().safe_dump(data, sort_keys=False), encoding="utf-8")
    print(f"release '{args.release_id}' marked shipped ({comp.fraction} host-verified).")
    return 0


def cmd_release_capability_owners(args) -> int:
    home = _home_repo(args.root)
    registry = _registry(home, args.projects_root)
    try:
        owners, diagnostics = backlog_capability_owners(home, args.target, registry)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    for finding in diagnostics:
        print(finding, file=sys.stderr)
    if diagnostics:
        return 1
    if not owners:
        print(f"no active backlog intents declare {validate_backlog_target(args.target)}")
        return 0
    for row in owners:
        print(f"{row.intent_id} [{row.release_id}] {row.visible_state} {row.change}")
    return 0


def cmd_release_backlog_summary(args) -> int:
    home = _home_repo(args.root)
    registry = _registry(home, args.projects_root)
    index, diagnostics = active_backlog_capability_index(home, registry)
    for finding in diagnostics:
        print(finding, file=sys.stderr)
    if diagnostics:
        return 1
    for target, owners in index.items():
        for row in owners.rows:
            print(f"{target}: {row.intent_id} [{row.release_id}] {row.visible_state} {row.change}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="planning", description="Product -> Release -> Spec planning layer")
    sub = p.add_subparsers(dest="verb", required=True)
    create = sub.add_parser("create")
    create.add_argument("release_id")
    create.add_argument("--specs", default="", help="comma-separated local spec ids")
    create.add_argument("--intents", default="", help="comma-separated intent ids (required for live releases)")
    create.add_argument("--title", default=None)
    create.add_argument("--product", default=None)
    create.add_argument("--root", default=None, help="home repo (default: cwd)")
    create.set_defaults(fn=cmd_release_create)
    for name, fn, needs_id in (("status", cmd_release_status, False),
                               ("lint", cmd_release_lint, False),
                               ("ship", cmd_release_ship, True)):
        sp = sub.add_parser(name)
        sp.add_argument("release_id", nargs=None if needs_id else "?", default=None)
        sp.add_argument("--root", default=None, help="home repo (default: cwd)")
        sp.add_argument("--projects-root", default=None, help="parent of the repos (default: repo parent)")
        sp.add_argument("-v", "--verbose", action="store_true")
        sp.set_defaults(fn=fn)
    owners = sub.add_parser("capability-owners")
    owners.add_argument("target")
    owners.add_argument("--root", default=None, help="home repo (default: cwd)")
    owners.add_argument("--projects-root", default=None, help="parent of the repos (default: repo parent)")
    owners.set_defaults(fn=cmd_release_capability_owners)
    summary = sub.add_parser("backlog-summary")
    summary.add_argument("--root", default=None, help="home repo (default: cwd)")
    summary.add_argument("--projects-root", default=None, help="parent of the repos (default: repo parent)")
    summary.set_defaults(fn=cmd_release_backlog_summary)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(list(sys.argv[1:] if argv is None else argv))
    return int(args.fn(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
