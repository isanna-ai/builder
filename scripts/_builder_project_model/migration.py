from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from _dispatch_runtime.paths import runtime_dir
from _yaml import yaml  # type: ignore

from .home import load_builder_home
from .lint import lint_home


@dataclass(frozen=True)
class FileDigest:
    relative_path: str
    sha256: str


@dataclass(frozen=True)
class ReleaseEquivalence:
    product_id: str
    release_id: str
    members: tuple[str, ...]
    verified: int
    total: int
    dangling: int
    planned: int


@dataclass(frozen=True)
class MigrationVerification:
    subject: str
    source_root: Path
    home_root: Path
    lint_findings: tuple[str, ...]
    legacy_releases: tuple[ReleaseEquivalence, ...]
    home_releases: tuple[ReleaseEquivalence, ...]
    protected_before: tuple[FileDigest, ...]
    protected_after: tuple[FileDigest, ...]
    findings: tuple[str, ...]

    @property
    def success(self) -> bool:
        return not self.lint_findings and not self.findings


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fingerprint_runtime_tree(repo_root: Path) -> tuple[FileDigest, ...]:
    runtime_root = runtime_dir(repo_root.resolve())
    if not runtime_root.exists():
        return ()
    digests: list[FileDigest] = []
    for path in sorted(item for item in runtime_root.rglob("*") if item.is_file()):
        digests.append(FileDigest(str(path.relative_to(runtime_root)), _sha256(path)))
    return tuple(digests)


def _legacy_release_equivalence(*, source_root: Path, projects_root: Path, product_id: str) -> tuple[ReleaseEquivalence, ...]:
    import planning

    product = planning.parse_product(runtime_dir(source_root) / "product.yaml")
    registry = planning.Registry(projects_root, source_root, product_context=product_id)

    releases: list[ReleaseEquivalence] = []
    for release_path in sorted((runtime_dir(source_root) / "releases").glob("*.yaml")):
        release = planning.parse_release(release_path, source_root)
        raw = yaml.safe_load(release_path.read_text(encoding="utf-8")) or {}
        if (
            planning.release_uses_intents(release.status)
            and isinstance(raw, dict)
            and isinstance(raw.get("specs"), list)
        ):
            members: list[str] = []
            dangling = planned = 0
            host_verified = 0
            for item in raw.get("specs", []):
                ref, err = planning.parse_spec_ref(item)
                if err or ref is None:
                    dangling += 1
                    continue
                members.append(ref.canonical)
                if ref.alias is None:
                    spec_dir = runtime_dir(source_root) / "specs" / ref.spec_id
                    resolve_error = None
                else:
                    spec_dir, resolve_error = registry.spec_dir(ref)
                if resolve_error or spec_dir is None or not spec_dir.is_dir():
                    dangling += 1
                    continue
                spec_data = planning._safe_load(spec_dir / "spec.yaml")
                spec_status = (str(spec_data.get("status", "")).strip().lower()
                               if isinstance(spec_data, dict) else "")
                if spec_status in planning.PRE_IMPLEMENTATION_STATUSES:
                    planned += 1
                    continue
                repo_root = source_root if ref.alias is None else registry.resolve(ref)[0]
                verification = planning._spec_verification(repo_root, ref.spec_id)
                if verification == planning.HOST_VERIFIED:
                    host_verified += 1
            comp = planning.Completeness(
                release.release_id,
                [],
                1 if members and host_verified == len(members) and not dangling else 0,
                1 if members else 0,
                dangling,
                1 if members and host_verified != len(members) else 0,
                {"fulfilled": 1 if members and host_verified == len(members) and not dangling else 0,
                 "in-flight": 1 if host_verified and host_verified < len(members) and not dangling else 0,
                 "decomposed": 1 if members and not host_verified and not dangling else 0,
                 "accepted": 0},
                [],
            )
            member_refs = tuple(members)
        else:
            comp = planning.completeness(release, registry)
            member_refs = tuple(member.ref.canonical for member in comp.members)
        releases.append(
            ReleaseEquivalence(
                product_id=product_id,
                release_id=release.release_id,
                members=member_refs,
                verified=comp.verified,
                total=comp.total,
                dangling=comp.dangling,
                planned=comp.planned,
            )
        )
    return tuple(releases)


def _home_release_equivalence(*, source_root: Path, projects_root: Path, product_id: str) -> tuple[ReleaseEquivalence, ...]:
    import planning

    registry = planning.Registry(projects_root, source_root, product_context=product_id)
    releases = planning.load_releases(source_root, product_context=product_id)
    snapshots: list[ReleaseEquivalence] = []
    for release in releases:
        comp = planning.completeness(release, registry)
        snapshots.append(
            ReleaseEquivalence(
                product_id=product_id,
                release_id=release.release_id,
                members=tuple(member.ref.canonical for member in comp.members),
                verified=comp.verified,
                total=comp.total,
                dangling=comp.dangling,
                planned=comp.planned,
            )
        )
    return tuple(snapshots)


def verify_bia_import(*, home_dir: Path, source_root: Path) -> MigrationVerification:
    home_path = home_dir.resolve()
    source = source_root.resolve()
    projects_root = home_path.parent.resolve()
    product_id = "bia"
    protected_before = fingerprint_runtime_tree(source)
    lint_findings = tuple(lint_home(home_path))
    legacy_releases = _legacy_release_equivalence(source_root=source, projects_root=projects_root, product_id=product_id)
    protected_after = fingerprint_runtime_tree(source)

    findings: list[str] = []
    try:
        home = load_builder_home(home_path)
        if home.project(product_id) is None:
            findings.append(f"canonical project {product_id!r} not found in {home_path}")
            home_releases = ()
        else:
            home_releases = _home_release_equivalence(source_root=source, projects_root=projects_root, product_id=product_id)
    except Exception as exc:  # noqa: BLE001
        home_releases = ()
        findings.append(f"canonical home could not be loaded for Record equivalence: {exc}")
    if legacy_releases != home_releases:
        findings.append("legacy-vs-home release equivalence mismatch")
    if protected_before != protected_after:
        findings.append("protected runtime tree changed during migration verification")
    return MigrationVerification(
        subject=product_id,
        source_root=source,
        home_root=home_path,
        lint_findings=lint_findings,
        legacy_releases=legacy_releases,
        home_releases=home_releases,
        protected_before=protected_before,
        protected_after=protected_after,
        findings=tuple(findings),
    )


def render_migration_verification(report: MigrationVerification) -> str:
    lines = [
        f"Verification subject: {report.subject}",
        f"Selected home: {report.home_root}",
        f"Source root: {report.source_root}",
        f"lint: {'clean' if not report.lint_findings else f'{len(report.lint_findings)} finding(s)'}",
        f"record-equivalence: {'ok' if report.legacy_releases == report.home_releases else 'mismatch'}",
        f"runtime-bytes: {'unchanged' if report.protected_before == report.protected_after else 'changed'}",
    ]
    for finding in report.lint_findings:
        lines.append(f"lint finding: {finding}")
    for finding in report.findings:
        lines.append(f"finding: {finding}")
    return "\n".join(lines) + "\n"
