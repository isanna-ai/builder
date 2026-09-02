#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from _dispatch_runtime.paths import runtime_dir
from _validators.common import parse_yaml_like_file
from _validators.renderers import render_artifact


def _load_runner_schema(root: Path):
    for candidate in (root / "schemas" / "runner.schema.yaml", runtime_dir(root) / "schemas" / "runner.schema.yaml"):
        if candidate.exists():
            return parse_yaml_like_file(candidate)
    return {}, ["runner.schema.yaml not found"]


def _profile_allows_render(root: Path, profile_name: str) -> bool:
    runner_data, runner_errors = _load_runner_schema(root)
    if runner_errors or not isinstance(runner_data, dict):
        return profile_name != "tiny_local"
    properties = runner_data.get("properties") if isinstance(runner_data.get("properties"), dict) else {}
    model_profiles = properties.get("model_profiles") if isinstance(properties.get("model_profiles"), dict) else runner_data.get("model_profiles")
    if not isinstance(model_profiles, dict):
        return profile_name != "tiny_local"
    profile_source = model_profiles.get("properties") if isinstance(model_profiles.get("properties"), dict) else model_profiles
    profile_def = profile_source.get(profile_name) if isinstance(profile_source, dict) else None
    if isinstance(profile_def, dict) and profile_def.get("allow_rendered_markdown") is False:
        return False
    return True

def main() -> int:
    parser = argparse.ArgumentParser(description="Render Builder canonical YAML artifacts into derived markdown or handoff text exports.")
    parser.add_argument("artifact", nargs="?", choices=["tasks", "review-log", "constitution-review", "handoff", "requirements", "design"], help="Artifact family to render")
    parser.add_argument("input", nargs="?", help="Path to the canonical YAML file")
    parser.add_argument("--output", help="Write the rendered output to this path instead of stdout")
    parser.add_argument("--force-render", action="store_true", help="Force render even if schema says no")
    parser.add_argument("--root", default=".", help="Workspace root to resolve spec.yaml")
    args = parser.parse_args()

    if not args.artifact or not args.input:
        parser.print_help()
        return 2

    input_path = Path(args.input).resolve()
    spec_dir = input_path.parent
    spec_yaml_path = spec_dir / "spec.yaml"

    if spec_yaml_path.exists() and not args.force_render:
        spec_data, spec_errors = parse_yaml_like_file(spec_yaml_path)
        if not spec_errors and isinstance(spec_data, dict):
            if spec_data.get("artifact_mode") == "ai_native":
                print("Skipping markdown render: artifact_mode=ai_native. Pass --force-render for an explicit derived export.", file=sys.stderr)
                return 0
            profile_name = spec_data.get("target_model_profile")
            if isinstance(profile_name, str) and profile_name:
                if not _profile_allows_render(Path(args.root), profile_name):
                    print(f"Skipping markdown render: allow_rendered_markdown=false for profile {profile_name}. Pass --force-render to override the derived export.", file=sys.stderr)
                    return 0

    data, errors = parse_yaml_like_file(input_path)
    if errors:
        for error in errors:
            print(f"ERROR  {error}", file=sys.stderr)
        return 1

    rendered = render_artifact(args.artifact, data)
    if args.output:
        Path(args.output).write_text(rendered, encoding="utf-8")
    else:
        sys.stdout.write(rendered)
    return 0

if __name__ == "__main__":
    sys.exit(main())
