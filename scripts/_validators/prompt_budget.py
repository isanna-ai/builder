from __future__ import annotations

import argparse
import sys
from pathlib import Path

from _validators.runtime import runtime_dir

try:
    from .runtime import RUNTIME_DIR_NAMES
except ImportError:  # Direct script invocation outside the package.
    from runtime import RUNTIME_DIR_NAMES
from typing import Any

try:
    from .common import CheckResult, ValidationContext, _parse_entry
except ImportError:  # pragma: no cover - direct script execution
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from _validators.common import CheckResult, ValidationContext, _parse_entry


PROFILES = {"tiny_local", "small_commercial", "flagship_commercial"}
RUNNER_PROMPTS = {"isanna-5-implement.prompt.md", "isanna-6-verify.prompt.md"}
BANNED = ("switch model", "Approve / Another pass", "Quick Paths")
TINY_EXCLUDED = {"skills/planning/SKILL.md", "standards/builder-contract.md", "prompts/isanna-help.prompt.md"}


def _root_from_spec(spec_dir: Path) -> Path:
    parts = spec_dir.parts
    if len(parts) >= 3 and parts[-3] in RUNTIME_DIR_NAMES and parts[-2] == "specs":
        return spec_dir.parents[2]
    return spec_dir.parents[0]


def _frontmatter(path: Path) -> tuple[dict[str, Any], list[str]]:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        return {}, [f"{path.name}: missing load_set frontmatter"]
    end = text.find("\n---", 4)
    if end < 0:
        return {}, [f"{path.name}: malformed frontmatter"]
    data = _parse_entry(text[4:end].splitlines()) or {}
    if not isinstance(data, dict):
        return {}, [f"{path.name}: malformed frontmatter"]
    return data, []


def run(context: ValidationContext, *, project_root: Path | None = None) -> CheckResult:
    root = project_root or _root_from_spec(context.spec_dir)
    errors: list[str] = []

    # The canonical repo keeps prompts at <root>/prompts; an INSTALLED repo keeps them at
    # <root>/.github/prompts. Looking only at the first location made this check a silent
    # no-op in every install: <root>/prompts exists there (a .gitkeep placeholder), so the
    # is_dir() guard passed, the glob matched nothing, and the empty loop still reported OK.
    candidates = [root / "prompts", root / ".github" / "prompts"]
    prompt_paths: list[Path] = []
    prompts_dir: Path | None = None
    for candidate in candidates:
        if not candidate.is_dir():
            continue
        found = sorted(candidate.glob("isanna-*.prompt.md"))
        if found:
            prompts_dir, prompt_paths = candidate, found
            break

    if prompts_dir is None:
        # Never report OK on an empty scan. Declare plainly that nothing was checked --
        # validate-spec.py prints SKIP and excludes skipped checks from total_checks.
        searched = ", ".join(str(c) for c in candidates)
        return CheckResult(
            "prompt_budget",
            [],
            total_checks=0,
            skipped=True,
            summary=f"prompt_budget skipped: no isanna-*.prompt.md found in {searched}",
        )
    for prompt in prompt_paths:
        lines = prompt.read_text(encoding="utf-8").splitlines()
        budget = 120 if prompt.name in RUNNER_PROMPTS else 200
        if len(lines) > budget:
            errors.append(f"{prompt.name} is {len(lines)} lines (budget {budget})")

        if prompt.name in RUNNER_PROMPTS:
            text = "\n".join(lines)
            for phrase in BANNED:
                if phrase in text:
                    errors.append(f"{prompt.name}: banned phrase `{phrase}` present")

        frontmatter, fm_errors = _frontmatter(prompt)
        errors.extend(fm_errors)
        load_set = frontmatter.get("load_set") if isinstance(frontmatter, dict) else None
        if not isinstance(load_set, dict):
            errors.append(f"{prompt.name}: missing load_set frontmatter")
            continue
        missing_profiles = sorted(PROFILES - set(str(key) for key in load_set))
        if missing_profiles:
            errors.append(f"{prompt.name}: load_set missing profiles {missing_profiles}")
        for profile in sorted(PROFILES):
            files = load_set.get(profile)
            if not isinstance(files, list):
                errors.append(f"{prompt.name}: load_set.{profile} must be a list")
                continue
            for item in files:
                rel = str(item)
                # A load_set names a spec ARTIFACT the phase will read -- not a file that must already
                # exist somewhere in the repo. Resolving only against the repo root and ONE reference
                # spec dir made the check depend on whichever historical spec happened to be picked:
                # `flagship-to-runner-workflow` predates intent.yaml, so every prompt declaring the
                # (perfectly legitimate) intent.yaml artifact failed the selftest. An artifact is valid
                # if the project defines it -- i.e. it ships a template or a schema for it.
                # Same split as the prompt lookup above: the canonical repo keeps these at
                # <root>/, an INSTALLED project keeps them under <root>/.builder/. Both must be
                # searched. Checking only <root>/ reports every load_set asset as missing in
                # every install, and a non-zero validator exit halts the verify phase.
                # A load_set entry with a DIRECTORY component (standards/, skills/, prompts/) is an
                # installed asset and must exist. A BARE filename (decisions.yaml,
                # system-model.yaml) is a per-spec artifact the phase reads if the spec has one --
                # `validate-spec.py` itself reports `SKIP decisions.yaml not found` for exactly that
                # state. Erroring on it made a legal spec fail validation, and a non-zero exit halts
                # the verify phase, naming a PROMPT file rather than the spec: near-undiagnosable.
                if "/" not in rel and rel.endswith(".yaml"):
                    continue
                asset_roots = [root, runtime_dir(root)]
                known_artifact = any(
                    (base / "templates" / rel).exists()
                    or (base / "schemas" / f"{Path(rel).stem}.schema.yaml").exists()
                    for base in asset_roots
                )
                found = any((base / rel).exists() for base in asset_roots)
                if not found and not (context.spec_dir / rel).exists() and not known_artifact:
                    errors.append(f"{prompt.name}: load_set.{profile} missing file {rel}")
            if prompt.name in RUNNER_PROMPTS:
                bad = sorted(TINY_EXCLUDED.intersection(str(item) for item in load_set.get("tiny_local", []) or []))
                for rel in bad:
                    errors.append(f"{prompt.name}: tiny_local load_set must not include {rel}")

    return CheckResult(
        "prompt_budget",
        errors,
        total_checks=len(prompt_paths),
        summary=None if errors else f"prompt_budget: valid ({len(prompt_paths)} prompts)",
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args()
    if not args.selftest:
        parser.print_help()
        return 2
    root = Path(__file__).resolve().parents[2]
    # This public, tracked fixture keeps the self-test independent of private
    # runtime state while still supplying a real spec artifact directory.
    fixture = root / "tests" / "fixtures" / "prompt-budget-selftest"
    result = run(ValidationContext(spec_dir=fixture), project_root=root)
    for error in result.errors:
        print(error, file=sys.stderr)
    return 1 if result.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
