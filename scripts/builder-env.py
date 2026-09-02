#!/usr/bin/env python3
"""builder-env — per-project environment up/test/down for the dispatcher + humans.

Project-agnostic by construction: the tool knows nothing about any particular repo, and each
project's env is brought up on demand only when a spec targets it. Source-library dependencies
consumed by relative path (e.g. `../shared-lib`) ride along as files on the shared tree — they
are never a separate env.

Resolution order for a project's profile:
  1. <project>/.builder/env.yaml      (env-as-code in the repo, if present)
  2. a built-in profile for known repos  (verified by the env inventory)
  3. auto-detect from the manifest       (deno.json tasks / package.json / pytest)

Strategy per repo: most run directly in the current environment, where the toolchain is already
present and nothing needs starting. A repo whose verify task needs services declares that itself
in `.builder/env.yaml`; env-up then only ensures its prerequisites (siblings present, deps
installed, container runtime reachable) and lets the repo's own task bring the services up.

Usage:
  builder-env.py up   <project> [--projects-dir DIR] [--target-dir DIR]
  builder-env.py test <project> [--full] [--projects-dir DIR]
  builder-env.py down <project> [--projects-dir DIR]

`up --target-dir DIR` (Builder R5/Model A): select the project's profile
(env.yaml / built-in / autodetect) and its sibling-source-dep check by the
usual `<projects-dir>/<project>` path, but run the actual prereqs (npm ci,
etc.) IN `DIR` instead — e.g. a per-spec git worktree that needs the main
repo's profile applied to ITS OWN checkout. Omit `--target-dir` and behavior
is byte-identical to before this flag existed (target = `<projects-dir>/
<project>`).
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

from _dispatch_runtime.paths import runtime_dir

# Built-in profiles, keyed by DIRECTORY NAME.
#
# Deliberately empty. This used to hold a hardcoded table of the maintainer's own repositories
# and their build quirks, which meant any project that happened to share one of those directory
# names silently inherited someone else's commands -- and `auto_env_up` is on by default, so it
# ran before implement/verify without anyone asking for it. A name collision is not consent.
#
# Declare your project's environment in its own repo instead, at `.builder/env.yaml`; that is
# resolution step 1 below and it is the supported path. Failing that, step 3 auto-detects from
# the manifest (deno.json tasks / package.json / pytest), which is what an unknown repo gets.
PROFILES: dict[str, dict] = {}


def _projects_dir(args) -> Path:
    """The directory holding sibling project checkouts. There is no useful default -- a wrong
    guess here points env-up at somebody else's tree -- so this must be passed explicitly."""
    configured = args.projects_dir or os.environ.get("BUILDER_PROJECTS_DIR")
    if not configured:
        raise SystemExit(
            "builder-env: no projects directory. Pass --projects-dir <path>, or set "
            "BUILDER_PROJECTS_DIR, or declare the environment in <project>/.builder/env.yaml."
        )
    return Path(configured)


def _load_env_yaml(proj_dir: Path) -> dict | None:
    p = runtime_dir(proj_dir) / "env.yaml"
    if not p.exists():
        return None
    try:
        from _yaml import yaml  # type: ignore
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _autodetect(proj_dir: Path) -> dict:
    if (proj_dir / "deno.json").exists() or (proj_dir / "deno.jsonc").exists():
        tasks = {}
        try:
            import json
            tasks = (json.loads((proj_dir / "deno.json").read_text(encoding="utf-8")) or {}).get("tasks", {})
        except Exception:
            pass
        subset = []
        if "check" in tasks:
            subset.append("deno task check")
        subset.append("deno task test:unit" if "test:unit" in tasks else ("deno task test" if "test" in tasks else "deno test -A --no-check"))
        return {"toolchain": "deno", "subset": subset}
    if (proj_dir / "package.json").exists():
        return {"toolchain": "node", "subset": ["npm test"]}
    if (proj_dir / "pyproject.toml").exists() or (proj_dir / "requirements.txt").exists():
        return {"toolchain": "python", "subset": ["python3 -m pytest -q"]}
    return {"toolchain": "unknown", "subset": []}


def _profile(project: str, proj_dir: Path) -> dict:
    return _load_env_yaml(proj_dir) or PROFILES.get(project) or _autodetect(proj_dir)


def _run(cmd: str, cwd: Path) -> int:
    print(f"  $ {cmd}", flush=True)
    return subprocess.run(["bash", "-lc", cmd], cwd=str(cwd)).returncode


def cmd_up(args) -> int:
    proj_dir = _projects_dir(args) / args.project
    if not proj_dir.exists():
        print(f"error: project dir not found: {proj_dir}", file=sys.stderr)
        return 1
    # R5/Model A: an optional --target-dir redirects where prereqs actually run
    # (e.g. a per-spec worktree) while the profile is still selected + siblings
    # still checked against the CANONICAL project dir above. Absent -> target
    # is proj_dir itself, so behavior (incl. the printed `dir=`) is unchanged.
    target_dir = Path(args.target_dir).expanduser() if getattr(args, "target_dir", None) else proj_dir
    prof = _profile(args.project, proj_dir)
    print(f"env-up {args.project}  (toolchain={prof.get('toolchain')}, dir={target_dir})")

    # 1. sibling source deps must exist on the shared tree (in-process libs, not services)
    missing = [s for s in prof.get("siblings", []) if not (proj_dir.parent / s).exists()]
    if missing:
        print(f"  ⚠️  missing sibling source deps (relative imports will fail): {missing}", flush=True)
    elif prof.get("siblings"):
        print(f"  ✓ siblings present: {prof['siblings']}")

    # 2. toolchain prereqs (e.g. npm ci for native node deps) — run IN target_dir
    if not args.no_prereqs:
        for pre in prof.get("prereqs", []):
            if pre.startswith("npm") and not (target_dir / "package.json").exists():
                continue
            if pre.strip() == "npm ci" and (target_dir / "node_modules").exists():
                print("  ✓ node_modules present; skipping npm ci")
                continue
            _run(pre, target_dir)

    # 3. docker reachable if the project needs services
    if prof.get("services_compose"):
        rc = subprocess.run(["bash", "-lc", "docker info >/dev/null 2>&1"]).returncode
        print(f"  {'✓' if rc == 0 else '⚠️ '} docker {'reachable' if rc == 0 else 'NOT reachable (services tier will fail)'} "
              f"(compose: {prof['services_compose']}; brought up by the repo's verify task)")
    print("  ready.")
    return 0


def cmd_test(args) -> int:
    proj_dir = _projects_dir(args) / args.project
    if not proj_dir.exists():
        print(f"error: project dir not found: {proj_dir}", file=sys.stderr)
        return 1
    prof = _profile(args.project, proj_dir)
    cmds = (prof.get("full") or prof.get("subset")) if args.full else prof.get("subset")
    cmds = cmds or prof.get("subset") or []
    if not cmds:
        print(f"error: no verify commands for {args.project} (add .builder/env.yaml)", file=sys.stderr)
        return 1
    print(f"env-test {args.project}  ({'full' if args.full else 'subset'}, {len(cmds)} command(s))")
    failed = []
    for cmd in cmds:
        if _run(cmd, proj_dir) != 0:
            failed.append(cmd)
            print(f"  ✗ FAILED: {cmd}", flush=True)
        else:
            print(f"  ✓ {cmd}", flush=True)
    if failed:
        print(f"env-test {args.project}: {len(failed)}/{len(cmds)} FAILED")
        return 1
    print(f"env-test {args.project}: all {len(cmds)} passed")
    return 0


def cmd_down(args) -> int:
    proj_dir = _projects_dir(args) / args.project
    prof = _profile(args.project, proj_dir)
    compose = prof.get("services_compose")
    if not compose or not (proj_dir / compose).exists():
        print(f"env-down {args.project}: no services to stop")
        return 0
    return _run(f"docker compose -f {compose} down -v", proj_dir)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="builder-env")
    parser.add_argument("--projects-dir", help="root holding sibling project checkouts (required; or set BUILDER_PROJECTS_DIR)")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("up", "test", "down"):
        p = sub.add_parser(name)
        p.add_argument("project")
        p.add_argument("--projects-dir", dest="projects_dir")
        if name == "up":
            p.add_argument("--no-prereqs", action="store_true")
            p.add_argument("--target-dir", dest="target_dir",
                            help="run prereqs in this dir instead of <projects-dir>/<project> "
                                 "(profile selection + sibling checks still use the latter)")
        if name == "test":
            p.add_argument("--full", action="store_true", help="run the full suite (may start services)")
    args = parser.parse_args(argv)
    return {"up": cmd_up, "test": cmd_test, "down": cmd_down}[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
