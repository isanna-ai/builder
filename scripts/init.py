#!/usr/bin/env python3
"""Create the additive, safe dispatch and Record wiring for one repository.

``isanna init`` deliberately does not install the slash-command workflow and
does not start a dispatcher.  Those are separate operations with different
authority and safety consequences.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from _dispatch_runtime.paths import runtime_dir


INSTALL_SH = Path(__file__).resolve().parent.parent / "install.sh"
GATE_POLICY_TEMPLATE = Path(__file__).resolve().parent.parent / "templates" / "gate-lane-policy.yaml"


def _dispatch_yaml(target: Path, reviews_enabled: bool) -> str:
    """Return the safe default dispatcher configuration."""
    reviews = "true" if reviews_enabled else "false"
    return f'''# Builder dispatcher for {target.name}.
# Codex authors, plans, IMPLEMENTS and verifies (pipeline.default_lane). Claude tokens
# are the scarce resource; codex-cli quota is not. The review phases route to the
# independent codex reviewer regardless.
#
# IMPORTANT: init creates configuration only. It never starts a daemon, and it
# never commits, pushes, or merges verified work.

queue_store:
  # Relative so this config works in dual mounts: host and container paths differ.
  # The dispatcher resolves it relative to this config file's directory.
  path: "dispatch-queue"

lanes:
  - name: "claude"
    provider: "claude-code-cli"
  # Codex is required while pipeline.reviews is enabled: independent review
  # phases need a separate lane to land on.
  - name: "codex"
    provider: "codex-cli"

routing_policy:
  default: ordered

pipeline:
  # Lane selection is LOCKED to default_lane (see _dispatch_runtime/phase_routing.py);
  # the runtime default is "claude". We override to codex to conserve Claude tokens.
  #
  # Trade-off, stated plainly: spec authoring moves OFF the deep_reasoner (Fable 5 /
  # Opus 4.8) onto gpt-5.4. Spec defects cascade through plan -> implement -> verify.
  # If a spec comes back thin, author THAT one on claude explicitly:
  #   builder-dispatch --config <this> draft --lane claude "<intent>"
  default_lane: codex
  reviews:
    enabled: {reviews}
  plan_gate: false
  deliver:
    # SAFE DEFAULT: verified work stays in the working tree for human review.
    # Never auto commit, push, or merge from this generated configuration.
    enabled: false

retry_policy:
  max_attempts: 4
  initial_seconds: 30
  max_seconds: 900

cooldown_policy:
  default_seconds: 600
'''


DEPENDENCIES_YAML = """# The Record uses this file to draw the roadmap DAG layer.\n# Without it, the roadmap remains a clean kanban. Add dependencies as specs need them.\n# dependencies: []\n"""

# The dispatch queue is STATE, not source. Without these, a freshly wired repo will
# happily commit live runner state on the first `git add -A`.
BUILDER_GITIGNORE = """\
# Ephemeral dispatcher runner state — not part of the durable spec→code record.
# The DURABLE artifacts (requirements/design/plan/traceability/system-model/spec/intent)
# ARE tracked: they are the record of how the code came to exist.
telemetry/

specs/*/phase-log.yaml
specs/*/handoff.yaml
specs/*/review-log.yaml
specs/*/review-log.md
specs/*/human-notes.yaml

specs/archive/*/phase-log.yaml
specs/archive/*/handoff.yaml
specs/archive/*/review-log.yaml
specs/archive/*/review-log.md
specs/archive/*/human-notes.yaml
"""

QUEUE_GITIGNORE = """\
# Runtime dispatch queue state — keep the directory, ignore its contents.
*
!.gitignore
"""


def _has_slash_command_workflow(target: Path) -> bool:
    """Detect the project-local install surfaces without attempting to install them."""
    templates = runtime_dir(target) / "templates"
    prompt_dirs = (target / ".github" / "prompts", target / ".claude" / "commands")
    prompts = any(directory.glob("sp-*") for directory in prompt_dirs if directory.is_dir())
    # Codex installs prompts globally, so the absence of project-local prompts is not alone
    # enough to call a repository half-installed. A completely absent local workflow is.
    return templates.is_dir() or prompts


def _print_plan(actions: list[tuple[str, Path]]) -> None:
    for action, path in actions:
        print(f"{action:<8} {path}")


def _next_steps(target: Path) -> None:
    print("\nNext steps:")
    print("  isanna dispatch --once              # run one included dispatcher cycle")
    print("  isanna record build                  # the flight recorder + the spec roadmap + releases")
    print("\ninit alone makes the repo VISIBLE and DRIVABLE, but NOT autonomous; run dispatch explicitly when requested.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="isanna init", description="Safely wire one repo for dispatch and The Record.")
    parser.add_argument("--target", default=".", help="repository to initialize (default: current directory)")
    parser.add_argument("--dry-run", action="store_true", help="print planned changes without writing")
    parser.add_argument("--force", action="store_true", help="replace existing generated files (never queue state)")
    parser.add_argument("--no-reviews", action="store_true", help="disable independent Codex review phases")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    target = Path(args.target).expanduser().resolve()
    if not target.is_dir():
        print(f"isanna init: not a directory: {target}")
        return 2

    builder = runtime_dir(target)
    dispatch = builder / "dispatch.yaml"
    queue = builder / "dispatch-queue"
    specs = builder / "specs"
    dependencies = builder / "dependencies.yaml"
    gate_policy = builder / "gate-lane-policy.yaml"
    files = (
        (dispatch, _dispatch_yaml(target, not args.no_reviews)),
        (dependencies, DEPENDENCIES_YAML),
        (gate_policy, GATE_POLICY_TEMPLATE.read_text(encoding="utf-8")),
        (builder / ".gitignore", BUILDER_GITIGNORE),
        # Lives INSIDE the queue dir, but only ever adds this one file — the queue's
        # contents are live state and are never touched (see the PRESERVE rule below).
        (queue / ".gitignore", QUEUE_GITIGNORE),
    )

    actions: list[tuple[str, Path]] = []
    if not builder.exists():
        actions.append(("CREATE", builder))
    for path, _ in files:
        if not path.exists():
            actions.append(("CREATE", path))
        elif args.force:
            actions.append(("REPLACE", path))
        else:
            actions.append(("PRESERVE", path))
    for directory in (queue, specs):
        if not directory.exists():
            actions.append(("CREATE", directory))
        else:
            # dispatch-queue is live state. It is never modified, including with --force.
            actions.append(("PRESERVE", directory))

    if not (target / ".git").exists():
        print(f"WARNING: {target} is not a git repository; proceeding anyway.")
    if not _has_slash_command_workflow(target):
        print("WARNING: the slash-command workflow is not installed here.")
        print(f"         Install it separately: bash {INSTALL_SH} --target {target}")

    if args.dry_run:
        _print_plan(actions)
        return 0

    changed = False
    for path, content in files:
        if path.exists() and not args.force:
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        changed = True
    for directory in (queue, specs):
        if not directory.exists():
            directory.mkdir(parents=True)
            changed = True

    if changed:
        _print_plan([action for action in actions if action[0] != "PRESERVE"])
        print(f"\nInitialized {target}")
        _next_steps(target)
    else:
        print(f"No changes: {target} is already initialized.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
