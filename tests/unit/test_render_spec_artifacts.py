from __future__ import annotations

import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def run_render(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "scripts/render-spec-artifacts.py", *args], cwd=ROOT, text=True, capture_output=True)


def write_minimal_spec(tmp_path: Path) -> Path:
    spec_dir = tmp_path / ".builder" / "specs" / "demo"
    spec_dir.mkdir(parents=True)
    (spec_dir / "spec.yaml").write_text("name: demo\ntarget_model_profile: tiny_local\n", encoding="utf-8")
    tasks = spec_dir / "tasks.yaml"
    tasks.write_text(
        "\n".join(
            [
                "artifact: tasks",
                "title: Demo",
                "spec: demo",
                "tasks:",
                "  - id: T1",
                "    title: Demo task",
                "    repo: builder/",
                "    files: [README.md]",
                "    tdd:",
                "      mode: exempt",
                "      reason: config-only",
                "    steps:",
                "      - text: Do it",
                "    verify:",
                "      - command: test -f README.md",
                "    done_when: Done",
                "    depends_on: []",
                "    parallel_with: []",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return tasks


def test_help_lists_force_render() -> None:
    result = run_render("--help")
    assert result.returncode == 0
    assert "--force-render" in result.stdout


def test_tiny_local_skips_markdown_without_force(tmp_path: Path) -> None:
    tasks = write_minimal_spec(tmp_path)
    output = tasks.with_suffix(".md")
    result = run_render("tasks", str(tasks), "--output", str(output), "--root", str(ROOT))
    assert result.returncode == 0
    assert not output.exists()
    assert "Skipping markdown render" in result.stderr


def test_force_render_writes_markdown(tmp_path: Path) -> None:
    tasks = write_minimal_spec(tmp_path)
    output = tasks.with_suffix(".md")
    result = run_render("tasks", str(tasks), "--output", str(output), "--root", str(ROOT), "--force-render")
    assert result.returncode == 0, result.stderr
    assert output.is_file()
