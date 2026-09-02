"""The manifest defines the complete local and remote installer asset surface."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _manifest() -> dict[str, set[str]]:
    assets: dict[str, set[str]] = {}
    for line in (ROOT / "asset-manifest.txt").read_text(encoding="utf-8").splitlines():
        kind, rel = line.split(" ", 1)
        assets.setdefault(kind, set()).add(rel)
    return assets


def test_remote_manifest_matches_full_installed_surface(tmp_path):
    assets = _manifest()
    assert assets["prompt"] == {path.name for path in (ROOT / "prompts").glob("isanna-*.prompt.md")}
    # No `test` assets: this project's shell tests exercise the SOURCE repo and cannot pass in
    # an installed project. They used to be installed anyway -- 13 of 20 failed on arrival.
    assert "test" not in assets, "development shell tests must not be shipped into user projects"
    assert assets["schema"] == {path.name for path in (ROOT / "schemas").glob("*.schema.yaml")}
    # Every standard the prompts LOAD, not just the ones the installer happened to copy. This set
    # used to be the four below without the guardrails, which is precisely how three prompts came
    # to declare `standards/builder-guardrails-*.md` in every model tier while no install has ever
    # contained them. A hardcoded expectation is only as good as the day it was written, so
    # tests/unit/test_prompt_load_sets_resolve.py now derives the requirement from the prompts
    # themselves; this stays as the explicit inventory both halves must agree on.
    assert assets["standard"] == {
        "builder-standards.md", "builder-tdd.md", "builder-workflow.md", "builder-contract.md",
        "builder-guardrails-implement.md", "builder-guardrails-review.md",
        "builder-guardrails-verify.md",
    }
    assert assets["template"] == {
        "builder-handoff-template.prompt.md", "constitution.md", "spec.yaml", "intent.yaml", "intent-object.yaml",
        "requirements.yaml", "design.yaml", "gate-lane-policy.yaml", "tasks.yaml", "handoff.yaml",
        "setup-decisions.yaml",
    }
    assert assets["skill"] == {
        path.relative_to(ROOT / "skills").as_posix()
        for path in (ROOT / "skills").glob("**/*") if path.is_file()
    }

    scripts = assets["script"]
    assert {path for path in scripts if "/" not in path} == {
        "validate-spec.py", "validate-constitution.py", "render-spec-artifacts.py",
        "record-workflow-event.py", "analyze-workflow-telemetry.py", "lint-builder-assets.py",
        "check-mirror-drift.py", "list-specs.py", "_yaml.py", "_yaml_compat.py",
    }
    assert {path.removeprefix("_dispatch_runtime/") for path in scripts if path.startswith("_dispatch_runtime/")} == {
        "__init__.py", "gate_evidence.py", "paths.py",
    }
    assert {path.removeprefix("_validators/") for path in scripts if path.startswith("_validators/")} == {
        path.name for path in (ROOT / "scripts" / "_validators").glob("*.py")
    }
    # Everything in _telemetry/ EXCEPT its own test modules. Those used to be shipped, and
    # they landed in `.builder/scripts/_telemetry/` where 8 of their cases failed -- they
    # resolve `ab-memory-gain.py`, which is not installed. Same defect class as the shell
    # tests: a verification tool must not ship tests that cannot pass where it ships them.
    assert {path.removeprefix("_telemetry/") for path in scripts if path.startswith("_telemetry/")} == {
        path.name for path in (ROOT / "scripts" / "_telemetry").glob("*.py")
        if not path.name.startswith("test_")
    }
    assert not any(path.startswith("_telemetry/test_") for path in scripts), (
        "test modules must not be shipped into user projects"
    )
    assert {path.removeprefix("_constitution/") for path in scripts if path.startswith("_constitution/")} == {
        path.name for path in (ROOT / "scripts" / "_constitution").glob("*.py")
    }
    assert {path.removeprefix("_sync/") for path in scripts if path.startswith("_sync/")} == {
        path.name for path in (ROOT / "scripts" / "_sync").glob("*.py")
    }

    # Simulate the remote fetch destination mapping strictly from the manifest.
    stage = tmp_path / "stage"
    for rel in scripts:
        source = ROOT / "scripts" / rel
        destination = stage / "scripts" / rel
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    # -I -S: isolated mode (ignores PYTHONPATH/site) so this cannot pass by
    # accident via a PyYAML `_yaml` C-extension shim shadowing our bundled
    # scripts/_yaml.py in site-packages. Pass the staged scripts dir via argv
    # since -I strips environment variables, including PYTHONPATH.
    result = subprocess.run(
        [
            sys.executable, "-I", "-S", "-c",
            "import sys; sys.path.insert(0, sys.argv[1]); "
            "import _validators, _sync.evidence, _sync.readmit, _yaml, _dispatch_runtime.paths; "
            "assert _validators.CHECKS",
            str(stage / "scripts"),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr

    installer = (ROOT / "install.sh").read_text(encoding="utf-8")
    assert 'curl -fsSL "$RAW_BASE/asset-manifest.txt"' in installer
    assert 'MANIFEST_PATH="$STAGING_DIR/asset-manifest.txt"' in installer
    assert '"$STAGING_DIR"/prompts/isanna-*.prompt.md' in installer
    assert '"$ASSET_SOURCE_DIR"/prompts/isanna-*.prompt.md' in installer
    assert 'test) source="tests/$rel_path"; dest="tests/$rel_path" ;;' in installer
    assert 'for sync_src in "$STAGING_DIR"/scripts/_sync/*.py; do' in installer
    assert 'for dispatch_src in "$STAGING_DIR"/scripts/_dispatch_runtime/*.py; do' in installer


def test_manifest_prompt_count_is_not_hardcoded():
    installer = (ROOT / "install.sh").read_text(encoding="utf-8")
    assert 'EXPECTED_PROMPT_COUNT="12"' not in installer
    assert "grep -c '^prompt ' \"$MANIFEST_PATH\"" in installer
