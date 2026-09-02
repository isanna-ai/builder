from __future__ import annotations

from pathlib import Path

from scripts._validators.common import ValidationContext
from scripts._validators.prompt_budget import run


def make_root(tmp_path: Path) -> Path:
    root = tmp_path
    (root / ".builder" / "specs" / "demo").mkdir(parents=True)
    for rel in ["standards/builder-workflow.md", "standards/builder-tdd.md", "standards/builder-contract.md", "skills/planning/SKILL.md"]:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x\n", encoding="utf-8")
    (root / "prompts").mkdir()
    return root


def prompt(lines: int = 10, load: str | None = None, body: str = "") -> str:
    load = load or "standards/builder-workflow.md"
    fm = f"---\nload_set:\n  tiny_local:\n    - {load}\n  small_commercial:\n    - {load}\n  flagship_commercial:\n    - {load}\n---\n"
    return fm + (body + "\n" if body else "") + "\n".join("line" for _ in range(lines)) + "\n"


def ctx(root: Path) -> ValidationContext:
    return ValidationContext(spec_dir=root / ".builder" / "specs" / "demo")


def test_runner_prompt_over_budget_errors(tmp_path: Path) -> None:
    root = make_root(tmp_path)
    (root / "prompts" / "isanna-5-implement.prompt.md").write_text(prompt(121), encoding="utf-8")
    assert "isanna-5-implement.prompt.md" in "\n".join(run(ctx(root)).errors)


def test_sp6_over_budget_errors(tmp_path: Path) -> None:
    root = make_root(tmp_path)
    (root / "prompts" / "isanna-6-verify.prompt.md").write_text(prompt(121), encoding="utf-8")
    assert "isanna-6-verify.prompt.md" in "\n".join(run(ctx(root)).errors)


def test_phase_prompt_over_200_errors(tmp_path: Path) -> None:
    root = make_root(tmp_path)
    (root / "prompts" / "isanna-1-specify.prompt.md").write_text(prompt(201), encoding="utf-8")
    assert "budget 200" in "\n".join(run(ctx(root)).errors)


def test_missing_load_set_errors(tmp_path: Path) -> None:
    root = make_root(tmp_path)
    (root / "prompts" / "isanna-1-specify.prompt.md").write_text("# no frontmatter\n", encoding="utf-8")
    assert "missing load_set" in "\n".join(run(ctx(root)).errors)


def test_missing_load_set_path_errors(tmp_path: Path) -> None:
    root = make_root(tmp_path)
    (root / "prompts" / "isanna-1-specify.prompt.md").write_text(prompt(1, "missing.md"), encoding="utf-8")
    assert "missing.md" in "\n".join(run(ctx(root)).errors)


def test_a_bare_spec_artifact_name_is_not_required_to_exist(tmp_path: Path) -> None:
    """A load_set entry with a directory (standards/, skills/, prompts/) is an INSTALLED asset and
    must exist. A bare `*.yaml` is a per-spec artifact the phase reads only if the spec has one --
    `validate-spec.py` itself reports `SKIP decisions.yaml not found` for that exact state.
    Erroring on it failed a legal spec, and a non-zero validator exit halts the verify phase."""
    root = make_root(tmp_path)
    (root / "prompts" / "isanna-1-specify.prompt.md").write_text(prompt(1, "decisions.yaml"), encoding="utf-8")
    assert "decisions.yaml" not in "\n".join(run(ctx(root)).errors)


def test_banned_phrase_errors(tmp_path: Path) -> None:
    root = make_root(tmp_path)
    (root / "prompts" / "isanna-5-implement.prompt.md").write_text(prompt(1, body="switch model"), encoding="utf-8")
    assert "switch model" in "\n".join(run(ctx(root)).errors)


def test_tiny_local_exclusion_errors(tmp_path: Path) -> None:
    root = make_root(tmp_path)
    (root / "prompts" / "isanna-5-implement.prompt.md").write_text(prompt(1, "skills/planning/SKILL.md"), encoding="utf-8")
    assert "tiny_local" in "\n".join(run(ctx(root)).errors)


def test_valid_prompts_pass(tmp_path: Path) -> None:
    root = make_root(tmp_path)
    (root / "prompts" / "isanna-5-implement.prompt.md").write_text(prompt(1), encoding="utf-8")
    assert run(ctx(root)).errors == []


def test_selftest_passes_on_this_repo():
    """REGRESSION: the selftest was FAILING on this very repo, and nothing noticed.

    It used to resolve against one hardcoded private runtime spec. The public self-test now uses a
    tracked minimal fixture, so it works in a publishable-only export as well as this working tree.

    A load_set names a spec ARTIFACT the phase will read -- an artifact is valid when the project
    DEFINES it (ships a template or a schema), not when some old spec happens to contain it.

    Found by `isanna model verify` re-running every check every spec ever wrote. Nothing else was
    running this selftest at all.
    """
    import subprocess, sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    proc = subprocess.run(
        [sys.executable, str(root / "scripts" / "_validators" / "prompt_budget.py"), "--selftest"],
        capture_output=True, text=True, cwd=str(root),
    )
    assert proc.returncode == 0, f"prompt_budget --selftest is failing:\n{proc.stdout}{proc.stderr}"


def test_finds_prompts_in_installed_github_layout(tmp_path: Path) -> None:
    """An INSTALLED repo keeps prompts at .github/prompts, not <root>/prompts.

    Regression guard: prompt_budget used to look only at <root>/prompts. In an install that
    directory exists but holds a .gitkeep placeholder, so the is_dir() guard passed, the glob
    matched nothing, the loop never ran, and the check reported OK -- a silent no-op in every
    installed repo while remaining green in the canonical one.
    """
    root = make_root(tmp_path)
    (root / "prompts" / ".gitkeep").write_text("", encoding="utf-8")
    github_prompts = root / ".github" / "prompts"
    github_prompts.mkdir(parents=True)
    (github_prompts / "isanna-6-verify.prompt.md").write_text(prompt(lines=500), encoding="utf-8")

    result = run(ctx(root), project_root=root)

    assert result.skipped is False
    assert result.total_checks == 1
    assert any("budget 120" in e for e in result.errors), result.errors


def test_empty_scan_skips_instead_of_reporting_ok(tmp_path: Path) -> None:
    """Zero prompts found must SKIP, never report a pass.

    max(1, len(prompt_paths)) previously turned an empty scan into total_checks=1 with a
    "prompt_budget: valid" summary -- a printed OK for a check that inspected nothing.
    """
    root = make_root(tmp_path)
    (root / "prompts" / ".gitkeep").write_text("", encoding="utf-8")

    result = run(ctx(root), project_root=root)

    assert result.skipped is True
    assert result.total_checks == 0
    assert not result.errors
    assert "skipped" in (result.summary or "")


def test_root_prompts_wins_when_both_layouts_are_populated(tmp_path: Path) -> None:
    """Canonical layout takes precedence, so builder keeps checking its own prompts."""
    root = make_root(tmp_path)
    (root / "prompts" / "isanna-6-verify.prompt.md").write_text(prompt(lines=10), encoding="utf-8")
    github_prompts = root / ".github" / "prompts"
    github_prompts.mkdir(parents=True)
    (github_prompts / "isanna-6-verify.prompt.md").write_text(prompt(lines=500), encoding="utf-8")

    result = run(ctx(root), project_root=root)

    assert result.total_checks == 1
    assert not result.errors, "should have read <root>/prompts, not the over-budget .github copy"
