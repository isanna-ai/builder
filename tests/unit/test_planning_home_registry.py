from __future__ import annotations

import importlib.util
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
FIXTURE = ROOT / "tests" / "fixtures" / "builder_project_model" / "home" / "portfolio"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

spec = importlib.util.spec_from_file_location("planning_home_registry", SCRIPTS / "planning.py")
planning = importlib.util.module_from_spec(spec)
assert spec and spec.loader
sys.modules[spec.name] = planning
spec.loader.exec_module(planning)


def _copy_fixture(tmp_path: Path) -> Path:
    target = tmp_path / "portfolio"
    shutil.copytree(FIXTURE, target)
    for repo in ("alpha-repo", "beta-repo", "shared-repo"):
        (target / repo / ".git").mkdir(parents=True)
    return target


def _stub_scan(repo: Path, rows: dict[str, str]) -> None:
    planning._scan_cache[str(repo.resolve())] = {"specs": [{"spec": spec_id, "verification": verdict} for spec_id, verdict in rows.items()]}


def test_registry_discovers_canonical_home_projects_and_project_scoped_aliases(tmp_path):
    portfolio = _copy_fixture(tmp_path)
    alpha_repo = portfolio / "alpha-repo"
    beta_repo = portfolio / "beta-repo"
    shared_repo = portfolio / "shared-repo"

    alpha_registry = planning.Registry(portfolio, alpha_repo, product_context="alpha")
    beta_registry = planning.Registry(portfolio, beta_repo, product_context="beta")

    assert {product.product for product in alpha_registry.products} == {"alpha", "beta"}
    shared_ref, err = planning.parse_spec_ref("shared/shared-spec")
    assert err is None and shared_ref is not None
    shared_dir, shared_err = alpha_registry.spec_dir(shared_ref)
    assert shared_err is None
    assert shared_dir == (shared_repo / ".builder" / "specs" / "shared-spec").resolve()

    bare_ref, bare_err = planning.parse_spec_ref("beta-fix")
    assert bare_err is None and bare_ref is not None
    beta_dir, beta_dir_err = beta_registry.spec_dir(bare_ref)
    assert beta_dir_err is None
    assert beta_dir == (beta_repo / ".builder" / "specs" / "beta-fix").resolve()


def test_home_releases_use_intent_denominator_and_preserve_member_verdicts(tmp_path):
    planning._scan_cache.clear()
    portfolio = _copy_fixture(tmp_path)
    alpha_repo = portfolio / "alpha-repo"
    shared_repo = portfolio / "shared-repo"
    beta_repo = portfolio / "beta-repo"
    _stub_scan(alpha_repo, {"alpha-core": planning.HOST_VERIFIED, "alpha-backlog": planning.UNKNOWN})
    _stub_scan(shared_repo, {"shared-spec": planning.SELF_REPORTED})
    _stub_scan(beta_repo, {"beta-fix": planning.UNKNOWN, "beta-backlog": planning.UNKNOWN})

    alpha_registry = planning.Registry(portfolio, alpha_repo, product_context="alpha")
    releases = planning.load_releases(alpha_repo, product_context="alpha")
    assert [release.release_id for release in releases] == ["alpha-release"]

    comp = planning.completeness(releases[0], alpha_registry)
    assert comp.fraction == "0/1"
    assert [member.ref.canonical for member in comp.members] == ["alpha-core", "shared/shared-spec"]
    assert [member.verification for member in comp.members] == [planning.HOST_VERIFIED, planning.SELF_REPORTED]


def test_external_dependency_never_enters_release_membership_or_percent_denominator(tmp_path):
    planning._scan_cache.clear()
    portfolio = _copy_fixture(tmp_path)
    alpha_repo = portfolio / "alpha-repo"
    shared_repo = portfolio / "shared-repo"
    beta_repo = portfolio / "beta-repo"
    spec_dir = alpha_repo / ".builder" / "specs" / "alpha-core"
    (spec_dir / "dependencies.yaml").write_text(
        "dependencies:\n  - spec: beta/beta-fix\n    kind: required\n",
        encoding="utf-8",
    )
    _stub_scan(alpha_repo, {"alpha-core": planning.HOST_VERIFIED})
    _stub_scan(shared_repo, {"shared-spec": planning.SELF_REPORTED})
    _stub_scan(beta_repo, {"beta-fix": planning.UNKNOWN})

    registry = planning.Registry(portfolio, alpha_repo, product_context="alpha")
    release = planning.load_releases(alpha_repo, product_context="alpha")[0]
    comp = planning.completeness(release, registry)

    assert [member.ref.canonical for member in comp.members] == ["alpha-core", "shared/shared-spec"]
    assert comp.total == 1 and comp.fraction == "0/1" and comp.percent == 0


def test_home_fixture_exposes_backlog_and_shared_specs_without_legacy_product_files(tmp_path):
    portfolio = _copy_fixture(tmp_path)
    alpha_repo = portfolio / "alpha-repo"

    registry = planning.Registry(portfolio, alpha_repo, product_context="alpha")

    assert registry.builder_home is not None
    project = registry.builder_home.project("alpha")
    assert project is not None
    assert project.declaration.backlog == ["alpha-backlog"]
    assert project.declaration.repos[1].alias == "shared"
    assert project.releases[0].declaration.specs[1].spec == "shared/shared-spec"
