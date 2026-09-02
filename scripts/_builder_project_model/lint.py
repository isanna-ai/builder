from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .common import ValidationError
from .home import lint_loaded_home, load_builder_home


def lint_home(home_dir: Path) -> list[str]:
    findings: list[str] = []
    try:
        home = load_builder_home(home_dir)
    except ValidationError as exc:
        findings.extend(issue.render() for issue in exc.issues)
        return findings
    return lint_loaded_home(home)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="builder-project-model-lint", description="Lint canonical .builder-home declarations.")
    parser.add_argument("home", nargs="?", default=".builder-home")
    return parser


def lint_home_from_args(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    home = Path(args.home).resolve()
    findings = lint_home(home)
    for finding in findings:
        print(finding, file=sys.stderr)
    if findings:
        print(f"\n{len(findings)} finding(s).", file=sys.stderr)
        return 1
    print(f"builder-home lint: {home} clean.")
    return 0


if __name__ == "__main__":
    raise SystemExit(lint_home_from_args(sys.argv[1:]))
