"""Every manifest-staged script/package must import cleanly on a fresh install root.

Regression guard for the asset-manifest dependency-closure bug: the manifest omitted
scripts/_dispatch_runtime/paths.py, scripts/_yaml.py, and scripts/_yaml_compat.py, so
an installed check-mirror-drift.py (and anything importing gate_evidence.py, which
imports _yaml) crashed with ModuleNotFoundError at every fresh install root — nothing
outside the manifest's own file set is ever staged.

This MUST run under `python3 -I -S` (isolated mode): -S skips site-packages entirely
and -I additionally ignores PYTHONPATH and stops Python from auto-prepending the
script's own directory to sys.path. Without -S, a PyYAML `_yaml` C-extension shim in
site-packages can shadow our bundled scripts/_yaml.py and make an import test pass by
accident even when scripts/_yaml.py itself was never shipped.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _manifest_scripts() -> set[str]:
    scripts: set[str] = set()
    for line in (ROOT / "asset-manifest.txt").read_text(encoding="utf-8").splitlines():
        kind, rel = line.split(" ", 1)
        if kind == "script":
            scripts.add(rel)
    return scripts


def _stage(tmp_path: Path) -> Path:
    """Simulate the remote/local staging destination mapping strictly from the manifest."""
    stage = tmp_path / "scripts"
    for rel in _manifest_scripts():
        source = ROOT / "scripts" / rel
        destination = stage / rel
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    return stage


def _module_name(rel: str) -> str:
    if rel.endswith("/__init__.py"):
        return rel[: -len("/__init__.py")].replace("/", ".")
    return rel[:-3].replace("/", ".")


def test_staged_package_modules_import_cleanly_under_isolated_python(tmp_path):
    stage = _stage(tmp_path)
    scripts = _manifest_scripts()

    # Every *.py that lives inside a staged package (has a "/" in its manifest
    # rel path) must import cleanly with only the staged scripts dir on sys.path.
    package_modules = sorted(_module_name(rel) for rel in scripts if "/" in rel)
    assert package_modules, "expected at least one staged package module"

    for module in package_modules:
        result = subprocess.run(
            [
                sys.executable, "-I", "-S", "-c",
                "import sys; sys.path.insert(0, sys.argv[1]); import " + module,
                str(stage),
            ],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"{module}: {result.stderr}"


def test_staged_top_level_scripts_run_under_isolated_python(tmp_path):
    stage = _stage(tmp_path)
    scripts = _manifest_scripts()

    top_level = sorted(rel for rel in scripts if "/" not in rel and rel.endswith(".py"))
    assert top_level, "expected at least one staged top-level script"

    # -P (implied by -I) stops Python from auto-prepending the script's own
    # directory to sys.path, so drive execution through runpy after inserting
    # the staged dir ourselves. --help is used so argparse-based scripts exit
    # via SystemExit(0) rather than requiring real positional args; a genuine
    # regression (e.g. a missing module) raises before that and shows up as a
    # non-SystemExit traceback / non-zero exit.
    driver = (
        "import runpy, sys\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "script = sys.argv[2]\n"
        "sys.argv = [script, '--help']\n"
        "try:\n"
        "    runpy.run_path(script, run_name='__main__')\n"
        "except SystemExit:\n"
        "    pass\n"
    )
    for rel in top_level:
        script_path = str(stage / rel)
        result = subprocess.run(
            [sys.executable, "-I", "-S", "-c", driver, str(stage), script_path],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"{rel}: {result.stderr}"
