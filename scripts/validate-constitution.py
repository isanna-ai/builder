#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

from _dispatch_runtime.paths import runtime_dir
from typing import Any

from _constitution.deterministic import evaluate
from _constitution.discovery import discover, load_machine_constitution
from _constitution.packet import build_spec_text
from _validators.common import parse_yaml_like_file
from _validators.renderers import render_constitution_review


def resolve_spec_dir(root: Path, spec_arg: str) -> Path:
    candidate = Path(spec_arg)
    if candidate.is_absolute() and candidate.is_dir():
        return candidate
    if candidate.is_dir():
        return candidate.resolve()
    return (runtime_dir(root) / "specs" / spec_arg).resolve()


def _changed_files_from_args(args: argparse.Namespace) -> list[str]:
    changed: list[str] = []
    for item in args.changed_files or []:
        changed.extend(part.strip() for part in item.split(",") if part.strip())
    if args.diff:
        diff_path = Path(args.diff)
        if diff_path.is_file():
            for line in diff_path.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.startswith("+++ b/") or line.startswith("--- a/"):
                    changed.append(line[6:].strip())
    return sorted(set(changed))


def _load_constitutions(paths: list[Path]) -> tuple[list[dict[str, Any]], list[str]]:
    constitutions: list[dict[str, Any]] = []
    errors: list[str] = []
    yaml_paths = [path for path in paths if path.suffix == ".yaml"]
    if yaml_paths:
        for path in yaml_paths:
            data, load_errors = load_machine_constitution(path)
            errors.extend(str(error) for error in load_errors)
            if data:
                data["_path"] = str(path)
                constitutions.append(data)
        return constitutions, errors

    md_paths = [path for path in paths if path.suffix == ".md"]
    for path in md_paths:
        constitutions.append(
            {
                "artifact": "constitution",
                "project": path.parent.parent.name if path.parent.name == ".builder" else path.parent.name,
                "source": str(path),
                "_path": str(path),
                "principles": [],
            }
        )
    return constitutions, errors


def _verdict(results: list[dict[str, Any]], skipped: bool) -> str:
    if skipped:
        return "skipped"
    statuses = {str(item.get("status", "")).strip() for item in results}
    if "block" in statuses:
        return "block"
    if "requires-human-decision" in statuses:
        return "requires-human-decision"
    if "warn" in statuses:
        return "warn"
    return "pass"


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate a Builder spec against project constitution principles.")
    parser.add_argument("spec", help="Feature name or direct path to spec directory")
    parser.add_argument("--root", default=".", help="Builder workspace root")
    parser.add_argument("--project-root", help="Project root that owns the constitution; defaults to --root")
    parser.add_argument("--strict", action="store_true", help="Fail when no constitution exists")
    parser.add_argument("--changed-files", nargs="*", help="Changed files relative to project root")
    parser.add_argument("--diff", help="Optional unified diff path")
    parser.add_argument("--phase", help="Optional Builder phase id used to honor constitution applies_to filters")
    parser.add_argument("--output", help="Output review YAML path")
    parser.add_argument("--no-model", action="store_true", help="Run deterministic checks only")
    args = parser.parse_args()

    root = Path(args.root).resolve()
    project_root = Path(args.project_root).resolve() if args.project_root else root
    spec_dir = resolve_spec_dir(root, args.spec)
    if not spec_dir.is_dir():
        print(f"ERROR  spec directory not found: {spec_dir}", file=sys.stderr)
        return 2

    paths = discover(project_root)
    constitutions, errors = _load_constitutions(paths)
    skipped = not constitutions
    changed_files = _changed_files_from_args(args)
    spec_text = build_spec_text(spec_dir)
    results: list[dict[str, Any]] = []

    for constitution in constitutions:
        results.extend(evaluate(constitution, project_root, changed_files, spec_text, args.phase or ""))

    verdict = _verdict(results, skipped)
    if skipped and args.strict:
        verdict = "block"
        errors.append(f"no constitution found under {project_root}")

    if errors and verdict == "pass":
        verdict = "warn"

    review = {
        "artifact": "constitution-review",
        "spec": spec_dir.name,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "verdict": verdict,
        "summary": "No constitution found." if skipped else f"Checked {len(constitutions)} constitution file(s).",
        "checked_constitutions": [str(path) for path in paths],
        "principle_results": results,
        "required_decisions": [
            str(item.get("summary", ""))
            for item in results
            if str(item.get("status", "")) == "requires-human-decision"
        ],
        "follow_up_actions": errors,
        "model_assisted": False,
    }

    output_path = Path(args.output).resolve() if args.output else spec_dir / "constitution-review.yaml"
    output_path.write_text(_to_yaml(review), encoding="utf-8")
    md_path = output_path.with_suffix(".md")
    md_path.write_text(render_constitution_review(review), encoding="utf-8")

    print(f"Constitution verdict: {verdict}")
    print(output_path)
    return 1 if verdict == "block" else 0


def _to_yaml(value: Any, indent: int = 0) -> str:
    pad = " " * indent
    if isinstance(value, dict):
        lines: list[str] = []
        for key, item in value.items():
            if item == []:
                lines.append(f"{pad}{key}: []")
                continue
            if isinstance(item, (dict, list)):
                lines.append(f"{pad}{key}:")
                lines.append(_to_yaml(item, indent + 2).rstrip())
            else:
                lines.append(f"{pad}{key}: {_scalar(item)}")
        return "\n".join(lines) + "\n"
    if isinstance(value, list):
        if not value:
            return f"{pad}[]\n"
        lines = []
        for item in value:
            if isinstance(item, dict):
                entries = list(item.items())
                if not entries:
                    lines.append(f"{pad}- {{}}")
                    continue
                first_key, first_value = entries[0]
                if first_value == []:
                    lines.append(f"{pad}- {first_key}: []")
                elif isinstance(first_value, (dict, list)):
                    lines.append(f"{pad}- {first_key}:")
                    lines.append(_to_yaml(first_value, indent + 2).rstrip())
                else:
                    lines.append(f"{pad}- {first_key}: {_scalar(first_value)}")
                for key, child in entries[1:]:
                    if child == []:
                        lines.append(f"{pad}  {key}: []")
                    elif isinstance(child, (dict, list)):
                        lines.append(f"{pad}  {key}:")
                        lines.append(_to_yaml(child, indent + 4).rstrip())
                    else:
                        lines.append(f"{pad}  {key}: {_scalar(child)}")
            else:
                lines.append(f"{pad}- {_scalar(item)}")
        return "\n".join(lines) + "\n"
    return f"{pad}{_scalar(value)}\n"


def _scalar(value: Any) -> str:
    if value is True:
        return "true"
    if value is False:
        return "false"
    if value is None:
        return "null"
    text = str(value)
    if text == "" or any(char in text for char in ":#[]{}&*!\n'\""):
        return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return text


if __name__ == "__main__":
    sys.exit(main())
