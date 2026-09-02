#!/usr/bin/env python3
"""isanna -- one binary over the three layers.

    isanna verify [PATH]          the host gate, standalone and CI-friendly
    isanna init                   safely wire a repo for dispatch and The Record
    isanna dispatch ...           drive specs autonomously (spec -> plan -> implement -> verify)
    isanna record build|export    the flight recorder: what was run, and what is actually proven
    isanna model build|verify|drift|stale   the living SSOT: what this system still does
    isanna sync                   refresh the SSOT after a spec (model + behavioral drift check)
    isanna release create|status|lint|ship   Product -> Release -> Spec planning
    isanna capture                propose a new intent from a distilled why + success criteria (non-interactive)
    isanna intent accept|reject|supersede     human-only intent lifecycle transitions
    isanna backlog list|rank|promote|retire|garden-review   tend the intent backlog
    isanna coverage               audit the gate record itself
    isanna lint                   validate canonical .builder-home declarations
    isanna demo                   watch a lying agent get caught, in under a second

The load-bearing verb is `verify`. It exists because the agent that writes the code must not be
the thing that decides the code works: the HOST runs the commands and reads the exit codes.

`isanna verify` therefore does NOT re-implement the gate -- it calls the dispatcher's own
`_collect_verify_commands` and `_run_verify_commands_detailed`. A standalone checker that merely
resembled the gate would be worse than none: it could pass what the gate fails, and the whole
claim of this product is that there is exactly ONE answer to "did it pass".
"""

from __future__ import annotations

import argparse
import importlib.util
import hashlib
import sys
import tempfile
import time
from pathlib import Path

from _intent_model import atomic_write_bytes, load_intent_object, project_visible_state, validate_intent_payload
from _dispatch_runtime.paths import runtime_dir
from _dispatch_runtime.phase_runtime import SYNC_RESULT_LOCKED_PATHS, write_sync_result
from typing import Any

sys.dont_write_bytecode = True

SCRIPTS = Path(__file__).resolve().parent
ROOT = SCRIPTS.parent

VERSION = "0.3.1"


def _load(script: str, name: str) -> Any:
    """Import a sibling script whose filename is not a valid module name (gate-coverage.py)."""
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / script)
    if spec is None or spec.loader is None:
        raise SystemExit(f"isanna: cannot load {script}")
    module = importlib.util.module_from_spec(spec)
    # Register BEFORE exec: a @dataclass in the target looks itself up via sys.modules[__module__]
    # during class creation (CPython 3.12+), and a module absent from sys.modules crashes with
    # `NoneType has no attribute __dict__`. This is why `isanna model` broke while `python3
    # scripts/model.py` (run as __main__, already registered) did not.
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------- verify


class _StandaloneWork:
    """The four attributes `_collect_verify_commands` reads. Duck-typed on purpose: we want the
    gate's real collection logic (packet verify_commands + per-spec and project setup-decisions,
    deduped) without standing up a queue, a runner, or a dispatch record."""

    def __init__(self, project_dir: Path, spec_id: str, runner_task_ref: str | None = None):
        self.project_dir = project_dir
        self.specs_dir = runtime_dir(project_dir) / "specs"
        self.spec_id = spec_id
        self.runner_task_ref = runner_task_ref


def _specs_in(project_dir: Path) -> list[str]:
    specs = runtime_dir(project_dir) / "specs"
    if not specs.is_dir():
        return []
    return sorted(p.name for p in specs.iterdir() if p.is_dir())


# Statuses at which NO task has been implemented yet. A spec sitting at one of these whose own
# acceptance commands already pass is describing something that already exists -- the deliverable
# was built by another spec, by hand, or before the spec was written. Past `implementing` a green
# is simply the expected outcome and says nothing.
_PRE_IMPLEMENTATION_STATUSES = frozenset({
    "specifying", "specified", "spec-reviewed", "designed", "reviewed", "planned",
})


def _spec_status(project_dir: Path, spec_id: str) -> str:
    """The spec's DECLARED status, read only to decide whether a green deserves the
    already-shipped advisory. Never treated as evidence of what exists -- that is the entire
    point of running the commands."""
    from _dispatch_runtime.phase_runtime import _safe_yaml

    data = _safe_yaml(runtime_dir(project_dir) / "specs" / spec_id / "spec.yaml") or {}
    if not isinstance(data, dict):
        return ""
    return str(data.get("status", "") or "").strip().lower()


def _print_shipped_advisory(project_dir: Path, spec_id: str, own_passed: int, own_total: int) -> None:
    """Say out loud when a spec nobody has implemented is already passing its own acceptance
    commands. Output only -- never an exit code: exit 0 means every collected command passed,
    and the one verb this project trusts for "did it pass" must keep answering that question
    and no other.

    The ratio counts ONLY the spec's own commands. Folding in the project defaults would inflate
    every spec's score by the repo's generic suite -- the same confusion between project evidence
    and spec evidence that `--spec` exists to refuse.

    Two thresholds, because a clean sweep is not the only informative shape and was not the
    expensive one. Measured on a real planned spec: 15 of its 20 own commands passed, the 5
    failures were environmental (a repo that no longer existed, a container that wasn't
    running), and a clean-sweep-only rule printed nothing at all.
    A three-quarters-built spec handed to an implementer is the costliest case there is.

    A simple majority is the bar. Below it the signal is noise: spec tasks routinely carry generic
    checks (`deno fmt --check`, typecheck) that pass in ANY repo, shipped or not, so a low ratio is
    not evidence and an advisory that fired on it would be trained away within a week."""
    if own_total <= 0 or own_passed <= 0:
        return
    status = _spec_status(project_dir, spec_id)
    if status not in _PRE_IMPLEMENTATION_STATUSES:
        # At or past `implementing`, passing commands are the EXPECTED outcome and carry no
        # information. Printing the advisory on every green would train the reader to skip it.
        return
    if own_passed == own_total:
        print(f"\nALREADY-SHIPPED?  {spec_id} is status `{status}` -- no task has been implemented "
              f"under it, yet all {own_total} of its own acceptance commands already pass "
              f"host-side.\n                  The deliverable most likely exists already. Confirm "
              f"against the code before implementing it a second time.")
    elif own_passed * 2 >= own_total:
        print(f"\nPARTIALLY-SHIPPED?  {spec_id} is status `{status}`, yet {own_passed} of "
              f"{own_total} of its own acceptance commands already pass host-side.\n"
              f"                    Some of it exists. Check whether the remaining failures are "
              f"real gaps or just environment (a missing repo, an offline container, an absent "
              f"credential)\n                    before implementing any of it -- read the FAIL "
              f"lines above rather than trusting the count.")


def cmd_verify(args: argparse.Namespace) -> int:
    project_dir = Path(args.path).resolve()
    if not project_dir.is_dir():
        print(f"isanna verify: not a directory: {project_dir}", file=sys.stderr)
        return 2

    sys.path.insert(0, str(SCRIPTS))
    from _dispatch_runtime.gate_evidence import classify_failure
    from _dispatch_runtime.lane_common import (
        _collect_verify_commands,
        _run_verify_commands_detailed,
        _spec_scoped_verify_commands,
    )

    if args.spec is not None and not args.spec.strip():
        # `--spec ""` used to be scoped BOTH ways at once: falsy for spec selection (so every
        # spec's commands were collected) but not-None for the scope flag (so the output claimed
        # to be about one spec, printed `spec=`, and read the status of a spec named ""). Three
        # sites disagreed about what counts as "a spec was named"; rejecting it here is the one
        # answer all three can share. Found by independent review (N1).
        print("UNVERIFIABLE  --spec was given an empty name. Name a spec, or omit --spec to "
              "verify the whole project.", file=sys.stderr)
        return 2

    if args.spec and not (runtime_dir(project_dir) / "specs" / args.spec).is_dir():
        # Distinguish "no such spec" from "this spec declares nothing". A typo used to be
        # reported as `declares no verify commands of its own`, which sends the reader to
        # audit a tasks.yaml that does not exist. Found by independent review.
        known = _specs_in(project_dir)
        near = [s for s in known if args.spec.lower() in s.lower() or s.lower() in args.spec.lower()]
        hint = f"  Did you mean: {', '.join(near[:5])}" if near else f"  {len(known)} spec(s) known."
        print(f"UNVERIFIABLE  no spec `{args.spec}` under "
              f"{runtime_dir(project_dir) / 'specs'}.\n{hint}", file=sys.stderr)
        return 2

    spec_ids = [args.spec] if args.spec else _specs_in(project_dir)
    if not spec_ids:
        print(f"isanna verify: no specs under {runtime_dir(project_dir) / 'specs'}", file=sys.stderr)
        return 2

    # `--spec X` makes every line below a claim about X, so X's own acceptance commands are read
    # from `tasks.yaml` when no runner packet is bound -- otherwise a spec that was planned but
    # never dispatched has nothing of its own to run. Whole-project mode is left exactly as it
    # was: it means "run this repo's gate", and pulling every spec's environment-bound acceptance
    # commands into every invocation would redefine the verb for everyone.
    spec_scoped = args.spec is not None
    commands: list[str] = []
    own_commands: list[str] = []
    for spec_id in spec_ids:
        work = _StandaloneWork(project_dir, spec_id)
        if spec_scoped:
            for cmd in _spec_scoped_verify_commands(work):
                if cmd not in own_commands:
                    own_commands.append(cmd)
        for cmd in _collect_verify_commands(work, include_spec_tasks=spec_scoped):
            if cmd not in commands:
                commands.append(cmd)

    if spec_scoped and not own_commands:
        # The project default commands may well pass -- but they are the repo's generic suite,
        # identical for every spec, so a green built on them says nothing about THIS one. A
        # verdict printed under `--spec` that cannot name spec-specific evidence is unearned.
        print(f"UNVERIFIABLE  {args.spec} declares no verify commands of its own -- the project "
              f"default commands are identical for every spec and prove nothing about this one.")
        return 1

    if not commands:
        # Blindness is never success. A gate with nothing to run has proven NOTHING, and saying
        # "ok" here is precisely the unearned green this tool exists to refuse.
        scope = args.spec or f"{len(spec_ids)} spec(s)"
        print(f"UNVERIFIABLE  {scope} declares no verify commands -- nothing was checked.")
        return 1

    if spec_scoped:
        print(f"HOST VERIFY  {project_dir.name}  spec={args.spec}  ({len(commands)} command(s), "
              f"{len(own_commands)} from the spec itself)\n")
    else:
        print(f"HOST VERIFY  {project_dir.name}  ({len(commands)} command(s))\n")
    results = _run_verify_commands_detailed(commands, str(project_dir))
    failed = 0
    own_set = set(own_commands)
    own_passed = 0
    for r in results:
        if r.ok:
            print(f"  PASS  {r.command}")
            if r.command in own_set:
                own_passed += 1
            continue
        failed += 1
        cls = classify_failure(r)
        print(f"  FAIL  {r.command}   (exit {r.exit_code} -- {cls})")
        tail = (r.stderr_tail or r.stdout_tail or "").strip().splitlines()
        for line in tail[-args.tail:]:
            print(f"        {line}")

    print()
    if failed:
        print(f"REJECTED  {failed}/{len(results)} command(s) failed. The host ran them; this is not an opinion.")
        verdict = 1
    else:
        print(f"VERIFIED  {len(results)}/{len(results)} command(s) passed, host-executed.")
        verdict = 0

    if spec_scoped:
        _print_shipped_advisory(project_dir, args.spec, own_passed, len(own_commands))
    return verdict


# ---------------------------------------------------------------------------- delegates


def cmd_ssot_adapter_coverage(args: argparse.Namespace) -> int:
    """Prove a repo's adapter maps its REAL change surface, before enabling sync there.

    Real paths from git, not invented ones: an adapter that covers a hand-picked list and misses
    the repo's actual chrome would pass a synthetic check and then block every sync."""
    import subprocess

    from _ssot_audit import adapter_coverage

    repo = Path(args.root).resolve()
    if not repo.is_dir():
        print(f"isanna ssot adapter-coverage: not a directory: {repo}", file=sys.stderr)
        return 2
    proc = subprocess.run(["git", "-C", str(repo), "diff", "--name-only", args.since, "HEAD"],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        print(f"isanna ssot adapter-coverage: cannot read changed paths from git "
              f"({args.since}..HEAD): {proc.stderr.strip()}", file=sys.stderr)
        return 2
    paths = [p for p in proc.stdout.splitlines() if p.strip()]
    result = adapter_coverage(repo, paths)

    if not result.adapter_present:
        print(f"NO ADAPTER  {repo.name} has no valid .builder/sync-adapter.yaml "
              f"(needs `artifact: sync-adapter` and a `mappings` list). Sync fails closed with "
              f"bootstrap_required.")
        return 1
    print(f"ADAPTER COVERAGE  {repo.name}  ({result.mapping_count} mapping(s) vs "
          f"{result.checked_paths} real changed path(s) since {args.since})")
    if result.vacuous:
        print("  UNVERIFIABLE  no changed paths in that range -- nothing was checked. "
              "Widen --since.")
        return 1
    if result.unmapped:
        print(f"  {len(result.unmapped)} UNMAPPED path(s). Each becomes an undeclared "
              f"`unmapped:<path>` tuple, so every sync in this repo will diverge:")
        for path in result.unmapped[:25]:
            print(f"    {path}")
        if len(result.unmapped) > 25:
            print(f"    ... and {len(result.unmapped) - 25} more")
        print("  Map them. Use `tuples: []` to cover a path without asserting an SSOT change.")
        return 1
    print(f"  COVERED  0 unmapped; {len(result.observed_targets)} declared target(s) observed: "
          f"{', '.join(result.observed_targets) or '(none)'}")
    return 0


def cmd_ssot_archive_check(args: argparse.Namespace) -> int:
    """Gate one spec's archive on whether it ever synced. Archiving past sync loses the SSOT
    update permanently, so this refuses under `enforce` and reports under `warn` (default)."""
    from _ssot_audit import archive_sync_gate

    if not args.spec:
        print("isanna ssot archive-check: --spec is required", file=sys.stderr)
        return 2
    repo = Path(args.root).resolve()
    if not repo.is_dir():
        print(f"isanna ssot archive-check: not a directory: {repo}", file=sys.stderr)
        return 2
    verdict = archive_sync_gate(repo, args.spec)
    if not verdict.reason:
        print(f"OK  {args.spec} has synced; safe to archive.")
        return 0
    if verdict.allowed:
        print(f"WARN  {verdict.reason}\n      (BUILDER_ARCHIVE_REQUIRE_SYNC=warn -- allowed for now; "
              f"set enforce to refuse)")
        return 0
    print(f"REFUSED  {verdict.reason}")
    return 1


def cmd_ssot(args: argparse.Namespace) -> int:
    """Report SSOT readiness per repo. Read-only: it writes nothing and touches no repo.

    `sync_blocked` is the headline because it is the precondition everything else waits on --
    a repo missing its adapter or its curated behavioral SSOT fails `isanna sync` closed with
    `bootstrap_required`, per spec, silently, forever. Nothing else reported that."""
    from _ssot_audit import SkippedWorktree, audit_repo, enumerate_fleet

    roots: list[Path] = []
    skipped_worktrees: list[SkippedWorktree] = []
    if args.projects_root:
        base = Path(args.projects_root).resolve()
        if not base.is_dir():
            print(f"isanna ssot: not a directory: {base}", file=sys.stderr)
            return 2
        roots, skipped_worktrees = enumerate_fleet(base)
        if not roots and not skipped_worktrees:
            print(f"isanna ssot: no builder-wired repos under {base}", file=sys.stderr)
            return 2
    else:
        roots = [Path(args.root).resolve()]

    audits = [audit_repo(r) for r in roots]

    if args.json:
        import json

        print(json.dumps([
            {
                "repo": a.repo, "wired": a.wired, "has_adapter": a.has_adapter,
                "has_behaviors": a.has_behaviors, "behavior_count": a.behavior_count,
                "behaviors_empty": a.behaviors_empty, "has_model": a.has_model,
                "sync_blocked": a.sync_blocked, "spec_count": a.spec_count,
                "synced_count": a.synced_count,
                "finished_never_synced": a.finished_never_synced,
                "unsynced_actionable": a.unsynced_actionable,
                "historical_no_provenance": a.historical_no_provenance,
                "awaiting_sync": a.awaiting_sync,
                "blind": a.blind,
            } for a in audits
        ], indent=2))
    else:
        print(f"{'REPO':<14} {'ADAPTER':<8} {'BEHAVIORS':<10} {'MODEL':<6} {'SPECS':<6} "
              f"{'SYNCED':<7} {'ACTIONABLE':<11} {'HISTORICAL':<11} {'AWAITING':<9} "
              f"{'BLIND':<6} STATE")
        for a in audits:
            # A COUNT, not True/False. The boolean read as "this repo's SSOT is real" and could
            # not tell a finished curation from a first slice -- one repo showed `True` on five
            # entries against 78k lines. `--` is absent, `0!` is present-but-declaring-nothing,
            # `?` is present-but-unparseable.
            if not a.has_behaviors:
                behaviors = "--"
            elif a.behaviors_empty:
                behaviors = "0!"
            elif a.behavior_count == 0:
                behaviors = "?"
            else:
                behaviors = str(a.behavior_count)
            # BLIND outranks BLOCKED in the state column: a repo the audit cannot fully read
            # has not been shown to be merely blocked, and reporting the weaker, more
            # actionable-sounding word would send someone to fix the wrong thing.
            state = "BLIND" if a.blind else ("BLOCKED" if a.sync_blocked else "ok")
            print(f"{a.repo:<14} {str(a.has_adapter):<8} {behaviors:<10} "
                  f"{str(a.has_model):<6} {a.spec_count:<6} {a.synced_count:<7} "
                  f"{len(a.unsynced_actionable):<11} {len(a.historical_no_provenance):<11} "
                  f"{len(a.awaiting_sync):<9} {len(a.blind):<6} {state}")
        blocked = [a for a in audits if a.sync_blocked]
        actionable = sum(len(a.unsynced_actionable) for a in audits)
        historical = sum(len(a.historical_no_provenance) for a in audits)
        print(f"\n{len(blocked)}/{len(audits)} repo(s) cannot sync at all; "
              f"{actionable} finished spec(s) could still sync; {historical} are historical.")
        if historical:
            print("  HISTORICAL means the spec has no gate-evidence/, so readmission refuses it\n"
                  "  (`unsafe-evidence-directory`) and it can NEVER sync at spec level. Sync goes\n"
                  "  forward only; reconcile these at release level via owner adoption.")
        if skipped_worktrees:
            # Excluding a worktree removes a DUPLICATE, so say what was excluded and on whose
            # behalf -- an unexplained shrink in the fleet total is indistinguishable from the
            # census bug this replaced.
            print(f"\n  {len(skipped_worktrees)} git worktree(s) excluded from the census, to "
                  "avoid counting\n  the same specs twice:")
            for wt in skipped_worktrees:
                if wt.parent_audited:
                    print(f"    {wt.name}  (worktree of {wt.parent}, which IS audited above)")
                else:
                    print(f"    {wt.name}  (worktree of {wt.parent} -- NOT audited: its specs "
                          f"are in NO row above)")
        blind_total = sum(len(a.blind) for a in audits)
        if blind_total:
            print(f"\n  {blind_total} SPEC(S) COULD NOT BE READ. Every number above is a floor,\n"
                  "  not a count: a spec whose status is unknown is in no bucket, because\n"
                  "  guessing one would invent evidence. Fix the read, then re-run -- do not\n"
                  "  treat these totals as complete.")
            for a in audits:
                for spec_id in a.blind:
                    print(f"    {a.repo}/{spec_id}")
        stubs = [a for a in audits if a.behaviors_empty]
        unreadable = [a for a in audits if a.has_behaviors and not a.behaviors_empty
                      and a.behavior_count == 0]
        if stubs:
            print(f"\n  {len(stubs)} repo(s) carry a behaviors file that declares NOTHING (`0!`).\n"
                  "  That satisfies sync's presence check and unblocks the repo while asserting\n"
                  "  nothing about the system: " + ", ".join(a.repo for a in stubs))
        if unreadable:
            print(f"\n  {len(unreadable)} repo(s) have a behaviors file that could not be parsed "
                  "(`?`).\n  Its coverage is unknown -- not zero: "
                  + ", ".join(a.repo for a in unreadable))
        if blocked:
            print("  BLOCKED means `isanna sync` fails closed with `bootstrap_required`: the repo\n"
                  "  is missing .builder/sync-adapter.yaml and/or docs/system-behaviors.yaml.")
        print("\n  BEHAVIORS is a COUNT, not a yes/no: presence is not coverage, and `isanna sync`\n"
              "  only ever checks presence. A low number is a repo whose SSOT was started and not\n"
              "  finished -- it will still report `ok`, because that is what sync itself does.")
        for a in audits:
            if a.awaiting_sync:
                print(f"  {a.repo}: {len(a.awaiting_sync)} spec(s) already carry an ssot-delta and "
                      f"are ready to sync once bootstrapped.")

    # Blindness fails --strict as hard as a blocked repo. `--strict` means "exit non-zero if
    # this repo is not ready", and an audit that could not read every spec has not shown the
    # repo is ready -- it has shown it does not know. Exiting 0 there is the unearned green.
    # A worktree whose main checkout is NOT audited fails --strict for the same reason a blind
    # spec does: its specs are represented in no row, so the census is a floor, not a count.
    # An excluded worktree whose parent IS audited is a clean dedupe and changes nothing.
    orphan_worktrees = [w for w in skipped_worktrees if not w.parent_audited]
    if args.strict and (any(a.sync_blocked or a.blind for a in audits) or orphan_worktrees):
        return 1
    return 0


def cmd_record(args: argparse.Namespace, rest: list[str]) -> int:
    return int(_load("record.py", "isanna_record").main([args.record_verb, *rest]) or 0)


def cmd_model(args: argparse.Namespace, rest: list[str]) -> int:
    return int(_load("model.py", "isanna_model").main([args.model_verb, *rest]) or 0)


def _cmd_sync_unlocked(args: argparse.Namespace) -> int:
    """Refresh the SSOT after a spec finishes: regenerate the spec-derived capability model, then
    validate the curated behavioral SSOT so it can never claim a behavior the host cannot verify.
    `sync` is the one command that keeps `what the system does` honest."""
    root = Path(args.root or ".").resolve()
    if not root.is_dir():
        print(f"isanna sync: not a directory: {root}", file=sys.stderr)
        return 2
    print(f"isanna sync  {root.name}\n")
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    from _sync.adapter import adapter_for_repo
    from _sync.evidence import (
        atomic_write_yaml,
        repair_legacy_sync_transaction,
        sha256_bytes,
        sync_result_payload_digest,
        validate_scope_evidence,
    )
    from _sync.publish import atomic_publish
    from _validators.behaviors import check_behavior_drift
    from _validators.common import parse_yaml_like_file

    specs_root = runtime_dir(root) / "specs"
    spec_id = args.spec
    if not spec_id:
        syncable = sorted(p.name for p in specs_root.iterdir() if (p / "ssot-delta.yaml").is_file()) if specs_root.is_dir() else []
        spec_id = syncable[0] if len(syncable) == 1 else None
    spec_dir = specs_root / spec_id if spec_id else None
    if spec_dir is None or not spec_dir.is_dir():
        print("bootstrap_required: syncable spec is missing.", file=sys.stderr)
        return 2
    delta_data, errors = parse_yaml_like_file(spec_dir / "ssot-delta.yaml")
    if errors:
        print(f"hook_failed: invalid ssot-delta.yaml ({'; '.join(errors)})", file=sys.stderr)
        return 1
    evidence_path = Path(args.scope_evidence).resolve() if args.scope_evidence else None
    repair_legacy_sync_transaction(root, spec_dir, evidence_path)
    scope, scope_errors = validate_scope_evidence(root, spec_dir, evidence_path)
    if scope_errors or scope is None:
        print(f"hook_failed: host scope evidence is unavailable ({'; '.join(scope_errors)})", file=sys.stderr)
        return 1

    adapter = adapter_for_repo(root)
    ssot = root / "docs" / "system-behaviors.yaml"
    result = "synced"
    hook_exit = 0
    observed: list[dict[str, Any]] = []
    undeclared: list[dict[str, Any]] = []
    publish_state = "staged-only"
    candidate_files: dict[Path, bytes] = {}
    drift: list[str] = []
    if adapter is None or not ssot.is_file():
        result = "bootstrap_required"
        hook_exit = 2
    else:
        observed = adapter.observed_tuples(scope["changed_paths"])
    declared = {
        (category, str(item.get("target", "")).strip(), str(item.get("change", "")).strip())
        for category in ("capabilities", "behaviors", "journeys")
        for item in (delta_data.get(category) or [])
        if isinstance(item, dict)
    }
    if result != "bootstrap_required":
        observed_set = {(row["category"], row["target"], row["change"]) for row in observed}
        undeclared = [
            {"category": c, "target": t, "change": ch}
            for (c, t, ch) in sorted(observed_set - declared)
        ]
        if undeclared:
            result, hook_exit = "divergence", 2

        if result == "synced":
            spec_status_now = ""
            _sd, _sd_errors = parse_yaml_like_file(spec_dir / "spec.yaml")
            if isinstance(_sd, dict):
                spec_status_now = str(_sd.get("status", "")).strip()
            if spec_status_now != "synced":  # grandfather already-synced specs: enforce forward-only
                ssot_ids = {
                    str(b.get("id"))
                    for b in ((parse_yaml_like_file(ssot)[0] or {}).get("behaviors") or [])
                    if isinstance(b, dict)
                }
                unlanded = sorted(
                    t for (c, t, ch) in declared
                    if c == "behaviors" and ch == "create" and t not in ssot_ids
                )
                if unlanded:
                    result, hook_exit = "hook_failed", 1
                    print(
                        "hook_failed: declared behavior create(s) not landed in "
                        "docs/system-behaviors.yaml: " + ", ".join(unlanded),
                        file=sys.stderr,
                    )

    preimage_parts: list[bytes] = []
    if result == "synced":
        try:
            with tempfile.TemporaryDirectory(prefix="isanna-sync-") as staging:
                stage_root = Path(staging)
                model = _load("model.py", "isanna_model_sync")
                model.build_model(root, out=stage_root / "model")
                generated = stage_root / "model" / "system-model.yaml"
                candidate_files[runtime_dir(root) / "model" / "system-model.yaml"] = generated.read_bytes()
                for dest in sorted(candidate_files, key=lambda item: str(item)):
                    preimage_parts.append(str(dest.relative_to(root)).encode("utf-8") + b"\0")
                    preimage_parts.append(dest.read_bytes() if dest.exists() else b"<missing>")
                    preimage_parts.append(b"\0")
                drift = check_behavior_drift(root)
                if drift:
                    result, hook_exit = "hook_failed", 1
                else:
                    try:
                        atomic_publish(candidate_files)
                        publish_state = "published"
                    except Exception as exc:
                        result, hook_exit = "hook_failed", 1
                        print(f"hook_failed: atomic publish failed ({exc})", file=sys.stderr)
        except Exception as exc:
            result, hook_exit = "hook_failed", 1
            print(f"hook_failed: candidate model build failed ({exc})", file=sys.stderr)

    payload = {
        "spec": spec_dir.name,
        "worktree_root": str(root),
        "verify_gate_id": scope["verify_gate_id"],
        "verify_gate_sha256": scope["verify_gate_sha256"],
        "verified_tree": scope["verified_tree"],
        "changed_paths_digest": scope["changed_paths_digest"],
        "declared_delta_digest": scope["declared_delta_digest"],
        "preimage_manifest_digest": sha256_bytes(b"".join(preimage_parts)),
        "observed_tuples": observed,
        "undeclared_tuples": undeclared,
        "hook_exit_code": hook_exit,
        "publish_state": publish_state,
        "result": result,
        "resolution_paths": list(SYNC_RESULT_LOCKED_PATHS),
        "transaction_id": scope["transaction_id"],
    }
    if scope.get("provenance") == "bootstrap-exception":
        expected = {
            "provenance": "bootstrap-exception",
            "owner_authorization": scope.get("owner_authorization"),
            "derived_baseline": scope.get("derived_baseline"),
        }
        if not expected["owner_authorization"] or not expected["derived_baseline"]:
            print("hook_failed: bootstrap-exception disclosure is incomplete", file=sys.stderr)
            return 1
        payload.update(expected)
    from _dispatch_runtime import gate_evidence

    gate_body = {
        "schema": gate_evidence.SCHEMA,
        "gate_id": "",
        "seq": 0,
        "gate": "host_sync",
        "polarity": "green" if result == "synced" else "red",
        "spec_id": spec_dir.name,
        "phase": "sync",
        "verdict": "pass" if result == "synced" and hook_exit == 0 else "fail",
        "hook_exit_code": hook_exit,
        "result": result,
        "verify_gate_id": payload["verify_gate_id"],
        "verified_tree": payload["verified_tree"],
        "changed_paths_digest": payload["changed_paths_digest"],
        "declared_delta_digest": payload["declared_delta_digest"],
        "transaction_id": payload["transaction_id"],
        "sync_result_payload_sha256": sync_result_payload_digest(payload),
        "prev_bundle_sha256": "",
        "bundle_sha256": "",
    }
    bundle = gate_evidence.write_bundle(spec_dir / gate_evidence.EVIDENCE_DIRNAME, gate_body)
    if bundle is None:
        print("hook_failed: host sync gate evidence could not be persisted", file=sys.stderr)
        return 1
    payload["sync_gate_id"] = gate_body["gate_id"]
    payload["sync_gate_bundle"] = f"{gate_evidence.EVIDENCE_DIRNAME}/{bundle.name}"
    payload["sync_gate_sha256"] = gate_body["bundle_sha256"]
    write_sync_result(spec_dir, payload)
    spec_state, spec_errors = parse_yaml_like_file(spec_dir / "spec.yaml")
    if not spec_errors:
        if result == "synced" and hook_exit == 0:
            spec_state["status"] = "synced"
        elif result == "divergence":
            spec_state["status"] = "verified"
        else:
            spec_state["status"] = "syncing"
        spec_state["current_phase"] = "sync"
        atomic_write_yaml(spec_dir / "spec.yaml", spec_state)
    if result == "bootstrap_required":
        print("bootstrap_required: curated behavioral SSOT or repo semantic adapter is missing.", file=sys.stderr)
        return 2
    if result == "divergence":
        print("divergence: observed tuples exceed the declared delta.")
        return hook_exit
    if result == "hook_failed":
        if drift:
            print(f"behavioral SSOT: {len(drift)} DRIFT finding(s) — a documented behavior has no live, gated test:")
            for finding in drift:
                print(f"  - {finding}")
        else:
            print("hook_failed: candidate validation or publication failed.", file=sys.stderr)
        return 1
    print("behavioral SSOT: clean — every documented behavior has a live, gated test.")
    return 0


def cmd_sync(args: argparse.Namespace) -> int:
    """Serialize ordinary sync with dispatch and readmission for the selected spec."""
    root = Path(args.root or ".").resolve()
    spec_id = args.spec
    if not spec_id:
        specs_root = runtime_dir(root) / "specs"
        syncable = sorted(
            path.name for path in specs_root.iterdir() if (path / "ssot-delta.yaml").is_file()
        ) if specs_root.is_dir() else []
        if len(syncable) == 1:
            spec_id = syncable[0]
            args.spec = spec_id
    if not spec_id:
        return _cmd_sync_unlocked(args)
    from _sync.locking import SpecMutationBusy, spec_mutation_lock

    try:
        with spec_mutation_lock(root, spec_id, blocking=False, owner="sync"):
            return _cmd_sync_unlocked(args)
    except SpecMutationBusy as exc:
        print(f"hook_failed: {exc}", file=sys.stderr)
        return 1


def cmd_coverage(args: argparse.Namespace, rest: list[str]) -> int:
    return int(_load("gate-coverage.py", "isanna_coverage").main(rest) or 0)


def cmd_lint(args: argparse.Namespace) -> int:
    from _builder_project_model.lint import lint_home_from_args

    return int(lint_home_from_args([args.home]) or 0)


def cmd_home(args: argparse.Namespace) -> int:
    from _builder_project_model import (
        apply_mutation_preview,
        apply_import_preview,
        build_dispatch_plan,
        CutoverOrchestrator,
        CutoverError,
        SyntheticCutoverOperator,
        LiveCutoverOperator,
        apply_plan,
        emit_declaration_patch_handoff,
        verify_bia_import,
        load_builder_home,
        plan_backlog_edit,
        plan_project_edit,
        plan_release_edit,
        plan_repo_register,
        plan_repo_unregister,
        preview_bia_import,
        render_dispatch_plan,
        render_cutover_results,
        render_home_status,
        render_import_preview,
        render_migration_verification,
        render_mutation_preview,
        render_plan,
        resolve_admission_repo,
        scaffold_home,
    )

    if args.home_verb == "init":
        projects_root = Path(args.projects_root).resolve()
        plan = scaffold_home(projects_root=projects_root, home_id=args.home_id)
        print(render_plan(projects_root, plan), end="")
        if args.confirm:
            apply_plan(plan)
            print(f"builder-home initialized at {projects_root / '.builder-home'}")
        else:
            print("dry-run only; re-run with --confirm to write.")
        return 0

    if args.home_verb in ("import-legacy", "import-bia"):
        home = load_builder_home(Path(args.home).resolve())
        preview = preview_bia_import(home=home, source_root=Path(args.source_root).resolve())
        print(render_import_preview(home, preview), end="")
        if args.confirm:
            apply_import_preview(preview)
            print(f"imported {preview.subject} into {home.root}")
            if args.verify_record:
                report = verify_bia_import(home_dir=Path(args.home).resolve(), source_root=Path(args.source_root).resolve())
                print(render_migration_verification(report), end="")
                return 0 if report.success else 1
        else:
            print("dry-run only; re-run with --confirm to write.")
            if args.verify_record:
                print("post-write verification skipped in dry-run mode.", file=sys.stderr)
        return 0
    if args.home_verb == "status":
        home = load_builder_home(Path(args.home).resolve())
        print(render_home_status(home), end="")
        return 0
    if args.home_verb == "lint":
        from _builder_project_model.lint import lint_home_from_args

        return int(lint_home_from_args([args.home]) or 0)
    if args.home_verb == "repo-register":
        home = load_builder_home(Path(args.home).resolve())
        preview = plan_repo_register(home=home, repo_id=args.repo_id, repo_path=args.path)
        print(render_mutation_preview(home, preview), end="")
        if args.confirm:
            apply_mutation_preview(preview)
            print(f"registered repo {args.repo_id} in {home.root}")
        else:
            print("dry-run only; re-run with --confirm to write.")
        return 0
    if args.home_verb == "repo-unregister":
        home = load_builder_home(Path(args.home).resolve())
        preview = plan_repo_unregister(home=home, repo_id=args.repo_id)
        print(render_mutation_preview(home, preview), end="")
        if args.confirm:
            apply_mutation_preview(preview)
            print(f"unregistered repo {args.repo_id} from {home.root}")
        else:
            print("dry-run only; re-run with --confirm to write.")
        return 0
    if args.home_verb == "project-edit":
        home = load_builder_home(Path(args.home).resolve())
        repo_pairs = None if not args.repo else [tuple(item.split("=", 1)) for item in args.repo]
        preview = plan_project_edit(
            home=home,
            project_id=args.project,
            title=args.title,
            description=args.description,
            default_repo=args.default_repo,
            repos=repo_pairs,
        )
        print(render_mutation_preview(home, preview), end="")
        if args.confirm:
            apply_mutation_preview(preview)
            print(f"updated project {args.project} in {home.root}")
        else:
            print("dry-run only; re-run with --confirm to write.")
        return 0
    if args.home_verb == "backlog-edit":
        home = load_builder_home(Path(args.home).resolve())
        backlog = [item.strip() for item in args.backlog.split(",") if item.strip()]
        preview = plan_backlog_edit(home=home, project_id=args.project, backlog=backlog)
        print(render_mutation_preview(home, preview), end="")
        if args.confirm:
            apply_mutation_preview(preview)
            print(f"updated backlog for {args.project} in {home.root}")
        else:
            print("dry-run only; re-run with --confirm to write.")
        return 0
    if args.home_verb in {"release-edit", "release-lifecycle"}:
        home = load_builder_home(Path(args.home).resolve())
        specs = None if args.specs is None else [item.strip() for item in args.specs.split(",") if item.strip()]
        intents = None if getattr(args, "intents", None) is None else [
            item.strip() for item in args.intents.split(",") if item.strip()
        ]
        status = args.status if args.home_verb == "release-lifecycle" else args.release_status
        preview = plan_release_edit(
            home=home,
            project_id=args.project,
            release_name=args.release,
            description=args.description,
            specs=specs,
            intents=intents,
            status=status,
        )
        print(render_mutation_preview(home, preview), end="")
        if args.confirm:
            apply_mutation_preview(preview)
            print(f"updated release {args.project}/{args.release} in {home.root}")
        else:
            print("dry-run only; re-run with --confirm to write.")
        return 0
    if args.home_verb == "dispatch-plan":
        home = load_builder_home(Path(args.home).resolve())
        actions = build_dispatch_plan(home=home, project_id=args.project, release_name=args.release)
        print(render_dispatch_plan(home=home, project_id=args.project, release_name=args.release, actions=actions), end="")
        return 0
    if args.home_verb == "cutover":
        try:
            if args.operator == "synthetic" and not args.state_file:
                raise CutoverError("synthetic operator requires --state-file")
            operator = (
                LiveCutoverOperator(Path(args.home))
                if args.operator == "live"
                else SyntheticCutoverOperator(Path(args.state_file))
            )
            orchestrator = CutoverOrchestrator(
                operator,
                dry_run=not args.apply,
                confirmations=set(args.confirm_step or []),
            )
            results = orchestrator.run_forward(steps=tuple(args.step) if args.step else None)
        except CutoverError as exc:
            print(f"cutover refused: {exc}", file=sys.stderr)
            return 1
        print(render_cutover_results(results), end="")
        return 0
    if args.home_verb == "rollback-cutover":
        try:
            if args.operator == "synthetic" and not args.state_file:
                raise CutoverError("synthetic operator requires --state-file")
            operator = (
                LiveCutoverOperator(Path(args.home))
                if args.operator == "live"
                else SyntheticCutoverOperator(Path(args.state_file))
            )
            orchestrator = CutoverOrchestrator(
                operator,
                dry_run=not args.apply,
                confirmations=set(args.confirm_step or []),
            )
            results = orchestrator.run_rollback(steps=tuple(args.step) if args.step else None)
        except CutoverError as exc:
            print(f"rollback refused: {exc}", file=sys.stderr)
            return 1
        print(render_cutover_results(results), end="")
        return 0
    if args.home_verb == "handoff-patch":
        home = load_builder_home(Path(args.home).resolve())
        preview = plan_backlog_edit(
            home=home,
            project_id=args.project,
            backlog=[item.strip() for item in args.backlog.split(",") if item.strip()],
        )
        print(emit_declaration_patch_handoff(preview=preview), end="")
        return 0
    if args.home_verb == "resolve-admission":
        home = load_builder_home(Path(args.home).resolve())
        print(f"Selected home: {home.root}")
        print(f"admission {args.admission_id} -> repo {resolve_admission_repo(home.root, admission_id=args.admission_id)}")
        return 0
    return 2


def cmd_release(args: argparse.Namespace, rest: list[str]) -> int:
    # Product -> Release -> Spec. The % done is computed only from host-observed events.
    return int(_load("planning.py", "isanna_planning").main([args.release_verb, *rest]) or 0)


def _intent_path(root: Path, intent_id: str) -> Path:
    return runtime_dir(root) / "intents" / intent_id / "intent.yaml"


def _load_intent_for_cli(root: Path, intent_id: str):
    planning = _load("planning.py", "isanna_planning_for_intent")
    path = _intent_path(root, intent_id)
    return path, load_intent_object(path, root, planning.parse_spec_ref)


def _intent_members(root: Path, intent_obj):
    planning = _load("planning.py", "isanna_planning_for_intent_inventory")
    registry = planning._registry(root, projects_root=None)
    members = []
    for canonical_ref in intent_obj.specs:
        ref, err = planning.parse_spec_ref(canonical_ref)
        if err or ref is None:
            continue
        spec_dir, resolve_error = registry.spec_dir(ref)
        if resolve_error or spec_dir is None or not spec_dir.is_dir():
            members.append(type("M", (), {"finding": resolve_error or "dangling"}))
            continue
        data = planning._safe_load(spec_dir / "spec.yaml")
        status = str(data.get("status", "")).strip() if isinstance(data, dict) else ""
        repo_root, _ = registry.resolve(ref)
        verification = planning._spec_verification(repo_root, ref.spec_id)
        members.append(type("M", (), {"finding": None, "status": status, "verification": verification, "canonical_ref": canonical_ref}))
    return members


def _open_controlling_tty():
    """Open the process controlling terminal, not merely a TTY-looking stdio stream."""
    return open("/dev/tty", "r+", encoding="utf-8", buffering=1)


def cmd_intent(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    target_state = args.intent_verb
    if target_state == "accept":
        target_state = "accepted"
    elif target_state == "reject":
        target_state = "rejected"
    elif target_state == "supersede":
        target_state = "superseded"
    if (
        not isinstance(args.intent_id, str)
        or not args.intent_id.strip()
        or args.intent_id in {".", ".."}
        or "/" in args.intent_id
        or "\\" in args.intent_id
    ):
        print(f"intent refused: unsafe intent id {args.intent_id!r}", file=sys.stderr)
        return 2
    path = _intent_path(root, args.intent_id)
    try:
        _, intent_obj = _load_intent_for_cli(root, args.intent_id)
        original = path.read_bytes()
    except Exception as exc:
        print(f"intent refused: {exc}", file=sys.stderr)
        return 2
    controlling_tty = None
    try:
        controlling_tty = _open_controlling_tty()
        is_controlling_tty = controlling_tty.isatty()
    except OSError as exc:
        if controlling_tty is not None:
            controlling_tty.close()
        print(f"intent refused: controlling TTY required ({exc})", file=sys.stderr)
        return 2
    if not is_controlling_tty:
        controlling_tty.close()
        print("intent refused: controlling TTY required", file=sys.stderr)
        return 2
    try:
        members = _intent_members(root, intent_obj)
        visible = project_visible_state(intent_obj, members)
        if target_state == "accepted" and intent_obj.status != "proposed":
            print("intent refused: accept only allows proposed -> accepted", file=sys.stderr)
            return 2
        if target_state in {"rejected", "superseded"}:
            if intent_obj.status not in {"proposed", "accepted"}:
                print("intent refused: terminal transitions require declared proposed or accepted", file=sys.stderr)
                return 2
            if visible.visible_state == "fulfilled":
                print("intent refused: fulfilled intents are immutable", file=sys.stderr)
                return 2
            if not isinstance(args.reason, str) or not args.reason.strip():
                print("intent refused: --reason is required", file=sys.stderr)
                return 2
        if target_state == "superseded" and args.superseded_by is not None:
            if not isinstance(args.superseded_by, str) or not args.superseded_by.strip():
                print("intent refused: --superseded-by must be non-empty when provided", file=sys.stderr)
                return 2
        controlling_tty.write("Type '<intent-id> <target-state>' to confirm: ")
        controlling_tty.flush()
        confirm = controlling_tty.readline().strip()
    except OSError as exc:
        print(f"intent refused: controlling TTY read failed ({exc})", file=sys.stderr)
        return 2
    finally:
        controlling_tty.close()
    expected = f"{args.intent_id} {target_state}"
    if confirm != expected:
        print("intent refused: confirmation mismatch", file=sys.stderr)
        if path.read_bytes() != original:
            raise SystemExit("intent invariant violated: file changed on refusal")
        return 2
    data = _load("planning.py", "isanna_planning_for_write")._safe_load(path)
    data["status"] = target_state
    if target_state in {"rejected", "superseded"}:
        data["reason"] = args.reason.strip()
    else:
        data.pop("reason", None)
    if target_state == "superseded":
        if args.superseded_by:
            data["superseded_by"] = args.superseded_by.strip()
    else:
        data.pop("superseded_by", None)
    payload = _load("planning.py", "isanna_planning_for_yaml")._yaml().safe_dump(data, sort_keys=False).encode("utf-8")
    try:
        parse_ref = _load("planning.py", "isanna_planning_for_reparse").parse_spec_ref
        validate_intent_payload(payload, path, root, parse_ref)
        atomic_write_bytes(path, payload)
    except Exception as exc:
        print(f"intent refused: {exc}", file=sys.stderr)
        return 2
    print(f"intent updated: {args.intent_id} -> {target_state}")
    return 0


def cmd_capture(args: argparse.Namespace) -> int:
    """Non-interactive capture: distilled why + success criteria -> a `proposed` intent, with no
    controlling TTY required so no future harness turn depends on this conversation staying open."""
    root = Path(args.root).resolve()
    capture = _load("isanna_capture.py", "isanna_capture_for_cli")
    try:
        capture.capture_intent(
            root,
            args.id,
            args.title,
            args.problem,
            args.why,
            args.success,
            non_goals=args.non_goal,
        )
    except Exception as exc:
        print(f"capture refused: {exc}", file=sys.stderr)
        return 2
    print(f"intent captured: {args.id} -> proposed")
    return 0
def cmd_backlog(args: argparse.Namespace) -> int:
    backlog = _load("isanna_backlog.py", "isanna_backlog_cli")
    root = Path(args.root).resolve()
    verb = args.backlog_verb
    if verb == "list":
        try:
            rows = backlog.visible_backlog(root)
        except backlog.BacklogError as exc:
            print(f"backlog refused: {exc}", file=sys.stderr)
            return 2
        if args.state:
            rows = [row for row in rows if row.visible_state == args.state]
        for row in rows:
            print(f"{row.intent_id} {row.visible_state} rank={row.rank}")
        return 0
    if verb == "rank":
        try:
            backlog.rank_intent(root, args.id, args.position)
        except (backlog.BacklogRefusal, backlog.BacklogError) as exc:
            print(f"backlog refused: {exc}", file=sys.stderr)
            return 2
        print(f"backlog rank updated: {args.id} -> position {args.position}")
        return 0
    if verb == "promote":
        try:
            collisions = backlog.promotion_collisions(root, args.id)
        except (ValueError, backlog.BacklogError) as exc:
            print(f"backlog refused: {exc}", file=sys.stderr)
            return 2
        if collisions:
            print(
                f"backlog refused: {args.id} collides with {', '.join(collisions)} "
                "on a declared capability target",
                file=sys.stderr,
            )
            return 2
        intent_args = argparse.Namespace(
            intent_verb="accept", intent_id=args.id, root=str(root), reason=None, superseded_by=None
        )
        return cmd_intent(intent_args)
    if verb == "retire":
        intent_args = argparse.Namespace(
            intent_verb="reject", intent_id=args.id, root=str(root), reason=args.reason, superseded_by=None
        )
        return cmd_intent(intent_args)
    if verb == "garden-review":
        try:
            report = backlog.garden_review(root, stale_days=args.stale_days, now_ts=time.time())
        except backlog.BacklogError as exc:
            print(f"backlog refused: {exc}", file=sys.stderr)
            return 2
        if not report:
            print("garden review: no stale proposed intents")
            return 0
        for row in report:
            collision_note = f" collision={','.join(row.collisions)}" if row.collisions else ""
            print(f"{row.intent_id} rank={row.rank} age_days={row.age_days}{collision_note}")
            for command in row.commands:
                print(f"  {command}")
        return 0
    return 2


def cmd_central(args: argparse.Namespace) -> int:
    from _builder_project_model.central_daemon import CentralDaemon
    from _dispatch_runtime.scheduler import SchedulerBusyError

    try:
        CentralDaemon(Path(args.home), poll_seconds=args.interval).run(once=args.once)
    except (RuntimeError, ValueError, SchedulerBusyError) as exc:
        print(f"central daemon refused: {exc}", file=sys.stderr)
        return 2
    return 0


def cmd_dispatch(args: argparse.Namespace, rest: list[str]) -> int:
    if args.attempt:
        from _dispatch_runtime.attempt_runner import AttemptRefused, run_reserved_attempt

        try:
            run_reserved_attempt(
                args.attempt,
                config_path=Path(args.config or runtime_dir(Path.cwd()) / "dispatch.yaml"),
                expected_attempt_id=args.attempt_id,
                home_path=Path(args.home).resolve() if args.home else None,
            )
        except (AttemptRefused, RuntimeError, ValueError) as exc:
            print(f"attempt refused: {exc}", file=sys.stderr)
            return 2
        return 0
    import runpy

    target = "builder-dispatch.py"
    config_argv = ["--config", args.config] if args.config else []
    # Management subcommands (cancel/pause/continue/gc/status/...) pass straight through to
    # builder-dispatch; only the bare `isanna dispatch [--once]` form defaults to `run`.
    _MGMT_VERBS = {
        "enqueue", "status", "lanes", "cancel", "pause", "continue",
        "gc", "drain", "approve", "hold", "draft",
    }
    if rest and rest[0] in _MGMT_VERBS:
        argv = [str(SCRIPTS / target), *config_argv, *rest]
    else:
        argv = [str(SCRIPTS / target), *config_argv, *rest, "run"]
        if args.once and "--once" not in argv:
            argv.append("--once")
    saved = sys.argv
    sys.argv = argv
    try:
        runpy.run_path(str(SCRIPTS / target), run_name="__main__")
    except SystemExit as exc:
        return int(exc.code or 0)
    finally:
        sys.argv = saved
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    return int(_load("demo.py", "isanna_demo").main([]) or 0)


def cmd_init(args: argparse.Namespace) -> int:
    argv = ["--target", args.target]
    if args.dry_run:
        argv.append("--dry-run")
    if args.force:
        argv.append("--force")
    if args.no_reviews:
        argv.append("--no-reviews")
    return int(_load("init.py", "isanna_init").main(argv) or 0)


def cmd_migrate(args: argparse.Namespace) -> int:
    argv = ["--dir", "--target", args.target]
    if args.dry_run:
        argv.append("--dry-run")
    if args.force:
        argv.append("--force")
    return int(_load("migrate.py", "isanna_migrate").main(argv) or 0)


# ---------------------------------------------------------------------------- cli


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="isanna",
        description="The host runs the tests. The agent does not get a vote.",
    )
    p.add_argument("--version", action="version", version=f"isanna {VERSION}")
    sub = p.add_subparsers(dest="verb", required=True)

    v = sub.add_parser("verify", help="run this project's verify commands HOST-SIDE and gate on exit 0")
    v.add_argument("path", nargs="?", default=".")
    v.add_argument("--spec", default=None, help="verify one spec's commands (default: every spec)")
    v.add_argument("--tail", type=int, default=8, help="lines of failing output to show (default 8)")

    d = sub.add_parser("dispatch", help="drive specs autonomously")
    d.add_argument("--once", action="store_true", help="run exactly one cycle, then exit")
    d.add_argument("--attempt", metavar="WORK_ID", help="run one central-owned leased attempt")
    d.add_argument("--attempt-id", default=None, help=argparse.SUPPRESS)
    d.add_argument("--config", default=None)
    d.add_argument("--home", default=None, help=argparse.SUPPRESS)

    central = sub.add_parser("central", help="run the opt-in central Builder Home daemon")
    central_sub = central.add_subparsers(dest="central_verb", required=True)
    central_run = central_sub.add_parser("run")
    central_run.add_argument("--home", default=".builder-home")
    central_run.add_argument("--once", action="store_true")
    central_run.add_argument("--interval", type=float, default=None)

    i = sub.add_parser("init", help="safely wire a repo for dispatch and The Record")
    i.add_argument("--target", default=".")
    i.add_argument("--dry-run", action="store_true")
    i.add_argument("--force", action="store_true")
    i.add_argument("--no-reviews", action="store_true",
                   help="generate dispatch.yaml with pipeline.reviews.enabled: false -- the "
                        "independent review phases are OMITTED, not downgraded. Nothing reviews "
                        "the work but the host gate.")

    mig = sub.add_parser("migrate", help="atomically move one stopped legacy runtime to .builder")
    mig.add_argument("--dir", action="store_true", required=True, help="move this repository's runtime directory")
    mig.add_argument("--target", default=".")
    mig.add_argument("--dry-run", action="store_true")
    mig.add_argument("--force", action="store_true")

    r = sub.add_parser("record", help="the flight recorder")
    r.add_argument("record_verb", choices=["build", "export"])

    ss = sub.add_parser("ssot", help="SSOT readiness per repo -- what can sync, and what never did")
    ss.add_argument("ssot_verb", choices=["audit", "archive-check", "adapter-coverage"])
    ss.add_argument("--since", default="HEAD~15",
                    help="adapter-coverage: git rev to diff from for real changed paths (default HEAD~15)")
    ss.add_argument("--spec", default=None, help="archive-check: the spec id to gate")
    ss.add_argument("--root", default=".", help="Audit one repo (default: .)")
    ss.add_argument("--projects-root", default=None,
                    help="Audit every builder-wired repo directly under this directory")
    ss.add_argument("--json", action="store_true", help="Emit JSON instead of a table")
    ss.add_argument("--strict", action="store_true",
                    help="Exit non-zero when any audited repo cannot sync")

    m = sub.add_parser("model", help="the living SSOT -- what this system still does")
    m.add_argument("model_verb", choices=["build", "verify", "drift", "stale"])

    syncp = sub.add_parser("sync", help="refresh the SSOT after a spec finishes (model + behavioral drift)")
    syncp.add_argument("--root", default=".")
    syncp.add_argument("--spec", default=None)
    syncp.add_argument("--scope-evidence", default=None)

    readmit = sub.add_parser("sync-readmit", help="human-only readmission of one named spec into lawful sync scope")
    readmit.add_argument("--root", default=".")
    readmit.add_argument("--spec", required=True)

    lintp = sub.add_parser("lint", help="validate canonical .builder-home declarations")
    lintp.add_argument("home", nargs="?", default=".builder-home")

    home = sub.add_parser("home", help="canonical Builder Home scaffold/import commands")
    home_sub = home.add_subparsers(dest="home_verb", required=True)
    home_init = home_sub.add_parser("init", help="preview or create a .builder-home scaffold")
    home_init.add_argument("--projects-root", required=True)
    home_init.add_argument("--home-id", default=None)
    home_init.add_argument("--confirm", action="store_true")
    home_status = home_sub.add_parser("status", help="show the selected home and lint summary")
    home_status.add_argument("--home", default=".builder-home")
    home_lint = home_sub.add_parser("lint", help="lint canonical .builder-home declarations")
    home_lint.add_argument("--home", default=".builder-home")
    # The user-facing verb names what it does. `import-bia` stays as an alias so any existing
    # invocation keeps working -- the old name is a product this repo never defines, so it is a
    # poor thing to meet in `isanna home --help` on day one.
    home_import = home_sub.add_parser(
        "import-legacy", aliases=["import-bia"],
        help="preview or import a legacy declaration tree into a home (the source product must be named 'bia')")
    home_import.add_argument("--home", default=".builder-home")
    home_import.add_argument("--source-root", required=True)
    home_import.add_argument("--confirm", action="store_true")
    home_import.add_argument("--verify-record", action="store_true")
    home_repo_register = home_sub.add_parser("repo-register", help="preview or register one repo in repositories.yaml")
    home_repo_register.add_argument("--home", default=".builder-home")
    home_repo_register.add_argument("--repo-id", required=True)
    home_repo_register.add_argument("--path", required=True)
    home_repo_register.add_argument("--confirm", action="store_true")
    home_repo_unregister = home_sub.add_parser("repo-unregister", help="preview or unregister one repo from repositories.yaml")
    home_repo_unregister.add_argument("--home", default=".builder-home")
    home_repo_unregister.add_argument("--repo-id", required=True)
    home_repo_unregister.add_argument("--confirm", action="store_true")
    home_project_edit = home_sub.add_parser("project-edit", help="preview or edit one canonical project declaration")
    home_project_edit.add_argument("--home", default=".builder-home")
    home_project_edit.add_argument("--project", required=True)
    home_project_edit.add_argument("--title", default=None)
    home_project_edit.add_argument("--description", default=None)
    home_project_edit.add_argument("--default-repo", default=None)
    home_project_edit.add_argument("--repo", action="append", default=[])
    home_project_edit.add_argument("--confirm", action="store_true")
    home_backlog_edit = home_sub.add_parser("backlog-edit", help="preview or replace one project's backlog")
    home_backlog_edit.add_argument("--home", default=".builder-home")
    home_backlog_edit.add_argument("--project", required=True)
    home_backlog_edit.add_argument("--backlog", required=True)
    home_backlog_edit.add_argument("--confirm", action="store_true")
    home_release_edit = home_sub.add_parser("release-edit", help="preview or edit one canonical release declaration")
    home_release_edit.add_argument("--home", default=".builder-home")
    home_release_edit.add_argument("--project", required=True)
    home_release_edit.add_argument("--release", required=True)
    home_release_edit.add_argument("--description", default=None)
    home_release_edit.add_argument("--specs", default=None)
    home_release_edit.add_argument("--intents", default=None)
    home_release_edit.add_argument("--release-status", default=None)
    home_release_edit.add_argument("--confirm", action="store_true")
    home_release_lifecycle = home_sub.add_parser("release-lifecycle", help="preview or change one release status")
    home_release_lifecycle.add_argument("--home", default=".builder-home")
    home_release_lifecycle.add_argument("--project", required=True)
    home_release_lifecycle.add_argument("--release", required=True)
    home_release_lifecycle.add_argument("--status", required=True)
    home_release_lifecycle.add_argument("--confirm", action="store_true")
    home_dispatch_plan = home_sub.add_parser("dispatch-plan", help="resolve a release roadmap into per-repo actions without enqueueing")
    home_dispatch_plan.add_argument("--home", default=".builder-home")
    home_dispatch_plan.add_argument("--project", required=True)
    home_dispatch_plan.add_argument("--release", required=True)
    home_cutover = home_sub.add_parser("cutover", help="run cutover; defaults to synthetic dry-run")
    home_cutover.add_argument("--operator", choices=("synthetic", "live"), default="synthetic")
    home_cutover.add_argument("--state-file")
    home_cutover.add_argument("--home", default=".builder-home")
    home_cutover.add_argument("--step", action="append", choices=[
        "stop_legacy",
        "prove_legacy_gone",
        "reconcile_legacy_pgids",
        "start_central",
        "acquire_repo_locks",
        "reconcile_repo_runtime",
        "replace_watchdogs",
    ])
    home_cutover.add_argument("--apply", action="store_true")
    home_cutover.add_argument("--confirm-step", action="append", default=[])
    home_rollback = home_sub.add_parser("rollback-cutover", help="run the cutover rollback package; defaults to synthetic dry-run")
    home_rollback.add_argument("--operator", choices=("synthetic", "live"), default="synthetic")
    home_rollback.add_argument("--state-file")
    home_rollback.add_argument("--home", default=".builder-home")
    home_rollback.add_argument("--step", action="append", choices=[
        "stop_new_central_launches",
        "reconcile_central_groups",
        "stop_central",
        "release_repo_locks",
        "restore_legacy_watchdogs",
        "restore_legacy_daemons",
        "select_legacy_discovery",
    ])
    home_rollback.add_argument("--apply", action="store_true")
    home_rollback.add_argument("--confirm-step", action="append", default=[])
    home_handoff_patch = home_sub.add_parser("handoff-patch", help="emit a declaration patch as handoff text only")
    home_handoff_patch.add_argument("--home", default=".builder-home")
    home_handoff_patch.add_argument("--project", required=True)
    home_handoff_patch.add_argument("--backlog", required=True)
    home_resolve_admission = home_sub.add_parser("resolve-admission", help="resolve one admission id to exactly one repo")
    home_resolve_admission.add_argument("--home", default=".builder-home")
    home_resolve_admission.add_argument("--admission-id", required=True)

    rel = sub.add_parser("release", help="Product -> Release -> Spec: completeness agents can't inflate")
    rel.add_argument("release_verb", choices=["create", "status", "lint", "ship", "capability-owners", "backlog-summary"])

    intent = sub.add_parser("intent", help="human-only intent lifecycle verbs")
    intent.add_argument("intent_verb", choices=["accept", "reject", "supersede"])
    intent.add_argument("intent_id")
    intent.add_argument("--root", default=".")
    intent.add_argument("--reason", default=None)
    intent.add_argument("--superseded-by", default=None)

    capture = sub.add_parser("capture", help="propose a new intent from a distilled why + success criteria (non-interactive)")
    capture.add_argument("--root", default=".")
    capture.add_argument("--id", required=True, dest="id")
    capture.add_argument("--title", required=True)
    capture.add_argument("--problem", required=True)
    capture.add_argument("--why", required=True)
    capture.add_argument("--success", action="append", required=True, help="a success criterion statement (repeatable)")
    capture.add_argument("--non-goal", action="append", default=[], help="a non-goal statement (repeatable)")
    bl = sub.add_parser("backlog", help="tend the intent backlog: list, rank, promote, retire, garden-review")
    bl_sub = bl.add_subparsers(dest="backlog_verb", required=True)

    bl_list = bl_sub.add_parser("list", help="list intents by computed visible state and owner-curated rank")
    bl_list.add_argument("--root", default=".")
    bl_list.add_argument("--state", default=None)

    bl_rank = bl_sub.add_parser("rank", help="curate the owner rank sidecar; never edits an intent object")
    bl_rank.add_argument("--root", default=".")
    bl_rank.add_argument("--id", dest="id", required=True)
    bl_rank.add_argument("--position", type=int, required=True)

    bl_promote = bl_sub.add_parser("promote", help="collision-checked, human-gated proposed -> accepted")
    bl_promote.add_argument("--root", default=".")
    bl_promote.add_argument("--id", dest="id", required=True)

    bl_retire = bl_sub.add_parser("retire", help="human-gated rejection with a required reason")
    bl_retire.add_argument("--root", default=".")
    bl_retire.add_argument("--id", dest="id", required=True)
    bl_retire.add_argument("--reason", default=None)

    bl_garden = bl_sub.add_parser("garden-review", help="read-only report of stale proposed intents")
    bl_garden.add_argument("--root", default=".")
    bl_garden.add_argument("--stale-days", type=int, default=30)

    sub.add_parser("coverage", help="audit the gate record itself")
    sub.add_parser("demo", help="watch a lying agent get caught")
    return p


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    args, rest = build_parser().parse_known_args(argv)
    if args.verb == "verify":
        return cmd_verify(args)
    if args.verb == "record":
        return cmd_record(args, rest)
    if args.verb == "ssot":
        if args.ssot_verb == "archive-check":
            return cmd_ssot_archive_check(args)
        if args.ssot_verb == "adapter-coverage":
            return cmd_ssot_adapter_coverage(args)
        return cmd_ssot(args)
    if args.verb == "model":
        return cmd_model(args, rest)
    if args.verb == "sync":
        return cmd_sync(args)
    if args.verb == "sync-readmit":
        from _sync.readmit import ReadmitFailure, readmit_spec

        try:
            code, _report = readmit_spec(Path(args.root).resolve(), args.spec)
            return code
        except ReadmitFailure as exc:
            print(f"sync-readmit refused: {exc.code}: {exc.detail}", file=sys.stderr)
            return 2
    if args.verb == "coverage":
        return cmd_coverage(args, rest)
    if args.verb == "lint":
        return cmd_lint(args)
    if args.verb == "home":
        return cmd_home(args)
    if args.verb == "release":
        return cmd_release(args, rest)
    if args.verb == "intent":
        return cmd_intent(args)
    if args.verb == "capture":
        return cmd_capture(args)
    if args.verb == "backlog":
        return cmd_backlog(args)
    if args.verb == "dispatch":
        return cmd_dispatch(args, rest)
    if args.verb == "central":
        return cmd_central(args)
    if args.verb == "demo":
        return cmd_demo(args)
    if args.verb == "init":
        return cmd_init(args)
    if args.verb == "migrate":
        return cmd_migrate(args)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
