from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
FIXTURES = ROOT / "tests" / "fixtures" / "builder_project_model" / "declarations" / "v1"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from _builder_project_model.common import ValidationError
from _builder_project_model.lint import lint_home
from _builder_project_model.parsers import (
    parse_builder_manifest,
    parse_policy_manifest,
    parse_project_manifest,
    parse_release_manifest,
    parse_repositories_manifest,
)


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _repo(root: Path, name: str) -> Path:
    repo = root / name
    (repo / ".git").mkdir(parents=True)
    return repo


def _load_isanna():
    spec = importlib.util.spec_from_file_location("isanna_cli_builder_home_test", SCRIPTS / "isanna.py")
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _seed_home(tmp_path: Path) -> Path:
    home = tmp_path / ".builder-home"
    _repo(tmp_path, "hivemind-cloud")
    _repo(tmp_path, "sharedlib")
    (tmp_path / "cloud-link").symlink_to(tmp_path / "hivemind-cloud", target_is_directory=True)
    _write(home / "builder.yaml", _fixture("builder-good.yaml"))
    _write(home / "repositories.yaml", _fixture("repositories-good.yaml"))
    _write(home / "policy.yaml", _fixture("policy-good.yaml"))
    _write(home / "projects" / "bia" / "product.yaml", _fixture("product-good.yaml"))
    _write(home / "projects" / "bia" / "releases" / "bia-audit-remediation.yaml", _fixture("release-good.yaml"))
    return home


def test_v1_declaration_parsers_accept_known_good_manifests(tmp_path):
    home = _seed_home(tmp_path)

    builder = parse_builder_manifest(home / "builder.yaml")
    repos = parse_repositories_manifest(home / "repositories.yaml")
    project = parse_project_manifest(home / "projects" / "bia" / "product.yaml")
    release = parse_release_manifest(home / "projects" / "bia" / "releases" / "bia-audit-remediation.yaml", project=project)
    policy = parse_policy_manifest(home / "policy.yaml")

    assert builder.home_id == "sol"
    assert [repo.id for repo in repos.repos] == ["hivemind-cloud", "sharedlib"]
    assert project.default_repo == "hivemind-cloud"
    assert [spec.spec for spec in release.specs] == [
        "cloud/audit-inventory",
        "cloud/fix-retention",
        "flow/publish-audit-events",
    ]
    assert policy.providers["claude-code-cli"].max_sessions == 2
    assert policy.governor_enabled is False
    assert policy.drain_repos == ()


def test_strict_unknown_keys_and_duplicate_aliases_are_rejected(tmp_path):
    home = tmp_path / ".builder-home"
    _write(home / "builder.yaml", _fixture("builder-bad-unknown-key.yaml"))
    try:
        parse_builder_manifest(home / "builder.yaml")
    except ValidationError as exc:
        assert any("unknown key 'owner'" in issue.render() for issue in exc.issues)
    else:
        raise AssertionError("expected strict unknown-key rejection")

    _write(home / "projects" / "bia" / "product.yaml", _fixture("product-bad-alias.yaml"))
    try:
        parse_project_manifest(home / "projects" / "bia" / "product.yaml")
    except ValidationError as exc:
        assert any("duplicate alias 'cloud'" in issue.render() for issue in exc.issues)
    else:
        raise AssertionError("expected duplicate alias rejection")


def test_every_canonical_declaration_rejects_unknown_top_level_keys(tmp_path):
    cases = (
        ("builder", "builder.yaml", parse_builder_manifest),
        ("repositories", "repositories.yaml", parse_repositories_manifest),
        ("policy", "policy.yaml", parse_policy_manifest),
        ("project", "projects/bia/product.yaml", parse_project_manifest),
        ("release", "projects/bia/releases/bia-audit-remediation.yaml", None),
    )

    for label, relative_path, parser in cases:
        home = _seed_home(tmp_path / label)
        path = home / relative_path
        path.write_text(path.read_text(encoding="utf-8") + "unexpected: true\n", encoding="utf-8")
        try:
            if parser is None:
                project = parse_project_manifest(home / "projects" / "bia" / "product.yaml")
                parse_release_manifest(path, project=project)
            else:
                parser(path)
        except ValidationError as exc:
            assert any("unknown key 'unexpected'" in issue.render() for issue in exc.issues), label
        else:
            raise AssertionError(f"expected strict unknown-key rejection for {label}")


def test_every_canonical_declaration_requires_supported_schema_version(tmp_path):
    cases = (
        ("builder", "builder.yaml", parse_builder_manifest),
        ("repositories", "repositories.yaml", parse_repositories_manifest),
        ("policy", "policy.yaml", parse_policy_manifest),
        ("project", "projects/bia/product.yaml", parse_project_manifest),
        ("release", "projects/bia/releases/bia-audit-remediation.yaml", None),
    )

    for label, relative_path, parser in cases:
        home = _seed_home(tmp_path / label)
        path = home / relative_path
        path.write_text(path.read_text(encoding="utf-8").replace("schema_version: 1", "schema_version: 2", 1), encoding="utf-8")
        try:
            if parser is None:
                project = parse_project_manifest(home / "projects" / "bia" / "product.yaml")
                parse_release_manifest(path, project=project)
            else:
                parser(path)
        except ValidationError as exc:
            assert any("unsupported schema_version 2" in issue.render() for issue in exc.issues), label
        else:
            raise AssertionError(f"expected schema-version rejection for {label}")


def test_repo_catalog_flags_duplicate_realpaths_and_non_portable_absolutes(tmp_path):
    home = tmp_path / ".builder-home"
    _repo(tmp_path, "hivemind-cloud")
    (tmp_path / "cloud-link").symlink_to(tmp_path / "hivemind-cloud", target_is_directory=True)
    _write(home / "repositories.yaml", _fixture("repositories-bad-duplicate-realpath.yaml"))

    try:
        parse_repositories_manifest(home / "repositories.yaml")
    except ValidationError as exc:
        rendered = [issue.render() for issue in exc.issues]
        assert any("duplicate repo real path" in issue for issue in rendered)
    else:
        raise AssertionError("expected duplicate-realpath rejection")


def test_release_parser_rejects_unknown_aliases_and_bare_refs_without_default_repo(tmp_path):
    home = _seed_home(tmp_path)
    product = parse_project_manifest(home / "projects" / "bia" / "product.yaml")
    _write(home / "projects" / "bia" / "releases" / "bia-audit-remediation.yaml", _fixture("release-bad-dangling-ref.yaml"))

    try:
        parse_release_manifest(home / "projects" / "bia" / "releases" / "bia-audit-remediation.yaml", project=product)
    except ValidationError as exc:
        rendered = [issue.render() for issue in exc.issues]
        assert any("unknown project alias 'unknown'" in issue for issue in rendered)
    else:
        raise AssertionError("expected alias validation failure")


def test_policy_parser_requires_both_canonical_provider_keys_and_equal_weight_allocation(tmp_path):
    home = tmp_path / ".builder-home"
    _write(home / "policy.yaml", _fixture("policy-bad-provider.yaml"))
    try:
        parse_policy_manifest(home / "policy.yaml")
    except ValidationError as exc:
        rendered = [issue.render() for issue in exc.issues]
        assert any("provider 'codex-cli' must be a mapping" in issue for issue in rendered)
        assert any("allocation.project_weight must be 1" in issue for issue in rendered)
    else:
        raise AssertionError("expected policy validation failure")

    unknown_provider = _fixture("policy-good.yaml").replace(
        "providers:\n",
        "providers:\n  gemini-cli: {}\n",
        1,
    )
    _write(home / "policy.yaml", unknown_provider)
    try:
        parse_policy_manifest(home / "policy.yaml")
    except ValidationError as exc:
        assert any("unknown provider key 'gemini-cli'" in issue.render() for issue in exc.issues)
    else:
        raise AssertionError("expected unknown-provider rejection")


def test_policy_parser_defaults_governor_off_accepts_true_and_rejects_unknown_governor_keys(tmp_path):
    home = tmp_path / ".builder-home"

    _write(home / "policy.yaml", _fixture("policy-governor-implicit-off.yaml"))
    assert parse_policy_manifest(home / "policy.yaml").governor_enabled is False
    assert parse_policy_manifest(home / "policy.yaml").drain_repos == ()

    _write(home / "policy.yaml", _fixture("policy-governor-enabled.yaml"))
    enabled = parse_policy_manifest(home / "policy.yaml")
    assert enabled.governor_enabled is True
    assert enabled.drain_repos == ("hivemind-cloud",)

    _write(home / "policy.yaml", _fixture("policy-bad-governor-key.yaml"))
    try:
        parse_policy_manifest(home / "policy.yaml")
    except ValidationError as exc:
        assert any("unknown key 'mode'" in issue.render() for issue in exc.issues)
    else:
        raise AssertionError("expected unknown governor-key rejection")


def test_policy_parser_rejects_bad_drain_repo_ids_and_lint_flags_unknown_known_catalog_mismatches(tmp_path):
    home = _seed_home(tmp_path)

    _write(home / "policy.yaml", _fixture("policy-bad-drain-repo.yaml"))
    try:
        parse_policy_manifest(home / "policy.yaml")
    except ValidationError as exc:
        assert any("repo id must match" in issue.render() for issue in exc.issues)
    else:
        raise AssertionError("expected invalid drain repo id rejection")

    _write(home / "policy.yaml", _fixture("policy-governor-unknown-drain-repo.yaml"))
    findings = lint_home(home)
    assert any("unknown repo id 'missing-repo'" in finding for finding in findings)


def test_lint_home_and_isanna_lint_exit_non_zero_on_canonical_declaration_violations(tmp_path):
    home = _seed_home(tmp_path)
    _write(home / "projects" / "bia" / "product.yaml", _fixture("product-bad-alias.yaml"))
    findings = lint_home(home)
    assert findings
    assert any("duplicate alias 'cloud'" in finding for finding in findings)

    isanna = _load_isanna()
    code = isanna.main(["lint", str(home)])
    assert code == 1


def test_legacy_release_lint_path_is_unchanged_and_still_lenient(tmp_path):
    isanna = _load_isanna()
    (tmp_path / ".builder").mkdir()
    saved = os.environ.get("ISANNA_PROJECTS_ROOT")
    os.environ["ISANNA_PROJECTS_ROOT"] = str(tmp_path)
    try:
        code = isanna.main(["release", "lint", "--root", str(tmp_path)])
    finally:
        if saved is None:
            os.environ.pop("ISANNA_PROJECTS_ROOT", None)
        else:
            os.environ["ISANNA_PROJECTS_ROOT"] = saved
    assert code == 0


def test_shared_repo_across_projects_is_allowed_but_disowned_owner_is_flagged(tmp_path):
    """Design §4: many-to-many membership is supported. A repo whose legacy product.yaml names a
    canonical project that DOES declare it (a legitimate shared member, e.g. a design system owned by
    `isanna` and shared into `studio`) must NOT be flagged; only a repo whose named owner DISOWNS it
    is a real conflict."""
    home = tmp_path / ".builder-home"
    for name in ("r-owner", "r-sharer", "r-other"):
        _repo(tmp_path, name)
    # r-owner's legacy product.yaml declares it belongs to project 'owner' — matching the canonical
    # 'owner' declaration below, so the same-project drift check (legacy aliases == canonical) passes.
    _write(tmp_path / "r-owner" / ".builder" / "product.yaml", "product: owner\ntitle: Owner\nrepos:\n- alias: r-owner\n")
    _write(home / "policy.yaml", _fixture("policy-good.yaml"))
    _write(
        home / "builder.yaml",
        "schema_version: 1\nhome_id: t\nrepositories: repositories.yaml\npolicy: policy.yaml\n"
        "projects:\n- id: owner\n  manifest: projects/owner/product.yaml\n"
        "- id: sharer\n  manifest: projects/sharer/product.yaml\n",
    )
    _write(
        home / "repositories.yaml",
        "schema_version: 1\nrepos:\n- id: r-owner\n  path: ../r-owner\n"
        "- id: r-sharer\n  path: ../r-sharer\n- id: r-other\n  path: ../r-other\n",
    )

    def _proj(pid: str, default: str, repos: list[str]) -> str:
        lines = ["schema_version: 1", f"product: {pid}", f"title: {pid}", "description: ''", f"default_repo: {default}", "repos:"]
        for r in repos:
            lines += [f"- alias: {r}", f"  repo_id: {r}"]
        lines += ["backlog: []", "releases: []"]
        return "\n".join(lines) + "\n"

    owner = home / "projects" / "owner" / "product.yaml"
    _write(owner, _proj("owner", "r-owner", ["r-owner"]))
    _write(home / "projects" / "sharer" / "product.yaml", _proj("sharer", "r-sharer", ["r-sharer", "r-owner"]))

    # Shared membership: 'owner' declares r-owner and 'sharer' also uses it -> allowed (no conflict).
    assert not any("conflicts with canonical" in f for f in lint_home(home)), lint_home(home)

    # Disowned: 'owner' now declares only r-other, so r-owner's legacy claims an owner that disowns it.
    _write(owner, _proj("owner", "r-other", ["r-other"]))
    assert any("legacy product 'owner' conflicts" in f for f in lint_home(home)), lint_home(home)
