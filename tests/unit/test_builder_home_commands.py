from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from _builder_project_model.home import load_builder_home
from _builder_project_model.importer import apply_import_preview, preview_bia_import, render_import_preview
from _builder_project_model.init import apply_plan, render_plan, scaffold_home


def _load_isanna():
    spec = importlib.util.spec_from_file_location("isanna_builder_home_commands", SCRIPTS / "isanna.py")
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _repo(root: Path, name: str) -> Path:
    repo = root / name
    (repo / ".git").mkdir(parents=True)
    return repo


def _seed_legacy_bia(root: Path) -> Path:
    repo = _repo(root, "hivemind-cloud")
    builder = repo / ".builder"
    (builder / "releases").mkdir(parents=True)
    (builder / "specs" / "audit-log-retention").mkdir(parents=True)
    (builder / "specs" / "audit-inventory").mkdir(parents=True)
    _write(
        builder / "product.yaml",
        "product: bia\n"
        "title: Bia\n"
        "repos:\n"
        "  - alias: hivemind-cloud\n",
    )
    _write(
        builder / "releases" / "bia-audit-remediation.yaml",
        "release: bia-audit-remediation\n"
        "product: bia\n"
        "title: Bia audit remediation\n"
        "goal: Remediate the confirmed audit findings\n"
        "status: draft\n"
        "specs:\n"
        "  - spec: audit-inventory\n",
    )
    _write(builder / "specs" / "audit-log-retention" / "spec.yaml", "status: planned\n")
    _write(builder / "specs" / "audit-inventory" / "spec.yaml", "status: specified\n")
    return repo


def test_home_init_preview_is_explicit_and_side_effect_free(tmp_path):
    plan = scaffold_home(projects_root=tmp_path)
    preview = render_plan(tmp_path, plan)

    assert "Selected projects root:" in preview
    assert "write" in preview
    assert not (tmp_path / ".builder-home").exists()


def test_home_init_writes_only_after_apply(tmp_path):
    plan = scaffold_home(projects_root=tmp_path, home_id="sol")

    apply_plan(plan)

    home = tmp_path / ".builder-home"
    assert (home / "builder.yaml").is_file()
    assert (home / "repositories.yaml").is_file()
    assert (home / "policy.yaml").is_file()
    assert (home / "projects").is_dir()


def test_bia_import_preview_never_writes_unconfirmed(tmp_path):
    source_root = _seed_legacy_bia(tmp_path)
    apply_plan(scaffold_home(projects_root=tmp_path, home_id="sol"))
    home = load_builder_home(tmp_path / ".builder-home")

    preview = preview_bia_import(home=home, source_root=source_root)
    rendered = render_import_preview(home, preview)

    assert "Import subject: bia" in rendered
    assert "projects/bia/product.yaml" in rendered
    assert not (tmp_path / ".builder-home" / "projects" / "bia" / "product.yaml").exists()


def test_bia_import_writes_only_after_apply(tmp_path):
    source_root = _seed_legacy_bia(tmp_path)
    apply_plan(scaffold_home(projects_root=tmp_path, home_id="sol"))
    home = load_builder_home(tmp_path / ".builder-home")

    preview = preview_bia_import(home=home, source_root=source_root)
    apply_import_preview(preview)

    project_manifest = tmp_path / ".builder-home" / "projects" / "bia" / "product.yaml"
    release_manifest = tmp_path / ".builder-home" / "projects" / "bia" / "releases" / "bia-audit-remediation.yaml"
    assert project_manifest.is_file()
    assert release_manifest.is_file()
    assert "default_repo: hivemind-cloud" in project_manifest.read_text(encoding="utf-8")


def test_isanna_bia_import_requires_confirm_to_write(tmp_path):
    isanna = _load_isanna()
    source_root = _seed_legacy_bia(tmp_path)
    assert isanna.main(["home", "init", "--projects-root", str(tmp_path), "--home-id", "sol", "--confirm"]) == 0
    home = tmp_path / ".builder-home"
    project_manifest = home / "projects" / "bia" / "product.yaml"
    command = ["home", "import-bia", "--home", str(home), "--source-root", str(source_root)]

    assert isanna.main(command) == 0
    assert not project_manifest.exists()

    assert isanna.main([*command, "--confirm"]) == 0
    assert project_manifest.is_file()


def test_import_legacy_and_its_import_bia_alias_are_the_same_command(tmp_path):
    """Both spellings must drive the same import.

    `import-bia` was renamed to `import-legacy` because the old name is a product this repo
    never defines, and it is the first thing a cloner meets in `isanna home --help`. The old
    name stays as an argparse alias so existing invocations keep working -- but every other test
    in this file drives the ALIAS, so without this one the primary verb is reachable only by
    hand. argparse sets the dest to whatever the user typed, so a dispatch written as
    `== "import-bia"` silently does nothing for the new name.

    Each verb gets its own home: importing registers the project in `builder.yaml`, so a second
    import into the same home is not the same operation.
    """
    isanna = _load_isanna()

    for verb in ("import-legacy", "import-bia"):
        base = tmp_path / verb
        base.mkdir()
        source_root = _seed_legacy_bia(base)
        assert isanna.main(
            ["home", "init", "--projects-root", str(base), "--home-id", "sol", "--confirm"]
        ) == 0
        home_root = base / ".builder-home"
        project_manifest = home_root / "projects" / "bia" / "product.yaml"
        command = ["home", verb, "--home", str(home_root), "--source-root", str(source_root)]

        assert isanna.main(command) == 0, f"{verb}: preview did not succeed"
        assert not project_manifest.exists(), f"{verb}: preview wrote without --confirm"

        assert isanna.main([*command, "--confirm"]) == 0, f"{verb}: confirmed import failed"
        assert project_manifest.is_file(), f"{verb}: confirmed import wrote nothing"


def test_isanna_home_init_requires_confirm_to_write(tmp_path):
    isanna = _load_isanna()

    preview_code = isanna.main(["home", "init", "--projects-root", str(tmp_path), "--home-id", "sol"])
    assert preview_code == 0
    assert not (tmp_path / ".builder-home").exists()

    apply_code = isanna.main(["home", "init", "--projects-root", str(tmp_path), "--home-id", "sol", "--confirm"])
    assert apply_code == 0
    assert (tmp_path / ".builder-home" / "builder.yaml").is_file()
