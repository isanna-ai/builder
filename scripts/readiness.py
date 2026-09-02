#!/usr/bin/env python3
"""The cross-repo readiness ladder — when is an upstream spec actually consumable downstream?

Same-repo, a required dependency is satisfied at local `verified`: the change is in the same working
tree. ACROSS repos that is wrong — repo B being verified in ITS OWN tree is invisible to repo A's
build. So a cross-repo edge climbs a ladder, and every rung is observed from a durable fact, ordered
by who wrote the evidence:

    1. verified   host gates passed in B's tree        spec.yaml + phase log      host
    2. delivered  a PR/branch exists on origin         git ls-remote              host
    3. merged     the delivery commit is an ancestor   git merge-base --is-...     GIT — unforgeable
                  of origin/<default>                                             by any lane
    4. available  a consumable artifact is published   registry query            the registry

The dispatcher gates a cross-repo dep on `merged` by default (or `available` when consumption is
package-mediated). Rationale: `verified` is one host-gate from the agent's own tree and consumable by
nobody; `delivered` (PR open) can be red/stale; **`merged` is the first rung at a durable, shared,
non-forgeable ref** — reaching it means passing whatever CI and branch policy guards the default
branch, a gate outside every agent lane. It is observable with plain git (no `gh`, absent in the
container) and works offline against the last fetch.

What is NOT observable, said plainly: semantic compatibility. `merged` proves B's change exists on
main, not that A can build against it — that is exactly what A's own host gates catch when A runs.
The ladder de-risks ORDERING; correctness stays with the gates.

This module is pure observation: it reads git and (optionally) a registry, and writes a
`dep-resolutions.yaml` audit. It never mutates a foreign repo and never coordinates.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

sys.dont_write_bytecode = True

RUNGS = ("none", "verified", "delivered", "merged", "available")
_RUNG_INDEX = {r: i for i, r in enumerate(RUNGS)}

GIT_TIMEOUT = 10
_VERIFIED_STATUSES = ("verified", "archived")


# ------------------------------------------------------------------ git (injectable for tests)

def _default_git_runner(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True,
                          timeout=GIT_TIMEOUT)


@dataclass
class Readiness:
    ref: str
    rung: str                 # highest rung reached
    observation: str          # human-readable ("merged ✓" / "waiting: publish @scope/pkg >=0.9.0")
    detail: dict

    def satisfies(self, required: str) -> bool:
        # An UNRECOGNIZED `required` resolves to the STRICTEST rung, never a weaker one: a typo like
        # `availabl` (meant `available`) must not silently downgrade the gate to `merged` and let an
        # unpublished dep through. A typo can only make the gate harder to satisfy, never easier.
        req_idx = _RUNG_INDEX.get(required)
        if req_idx is None:
            req_idx = len(RUNGS) - 1  # strictest known rung
        return _RUNG_INDEX.get(self.rung, 0) >= req_idx


# ------------------------------------------------------------------ the rungs

def _rung_verified(spec_dir: Path, safe_load) -> tuple[bool, str]:
    spec = safe_load(spec_dir / "spec.yaml")
    status = str(spec.get("status", "")).strip().lower() if isinstance(spec, dict) else ""
    return (status in _VERIFIED_STATUSES), status


# delivery.yaml is HOST-written (by the not-yet-built delivery.py) and carries this marker. It
# records the commit the host actually delivered the spec's work as -- so the `merged` probe checks
# a commit tied to real work, not one the agent picked. WITHOUT this marker the record is untrusted
# and the ladder tops out at `verified`.
#
# Why the marker matters (adversarial review, 2026-07-14): delivery.yaml lives in the spec dir the
# agent controls. An agent could point `commit:` at ANY SHA already on origin/<default> (e.g. main's
# HEAD) and `is-ancestor` would say "merged" for work that never landed. The unforgeable half of
# `merged` is git's is-ancestor answer; the forgeable half is WHICH commit, which is why the record
# must be host-authored. This marker closes the naive forge; the residual (an agent forging the
# marker as the same OS user) is the project's standard tamper-EVIDENT caveat -- the load-bearing
# guarantee is that delivery.py records the real work-commit, and getting THAT onto main requires
# passing branch protection, a gate outside every agent lane.
DELIVERY_PROVENANCE = "builder-delivery"


def _delivery_record(spec_dir: Path, safe_load) -> dict | None:
    rec = safe_load(spec_dir / "delivery.yaml")
    if not isinstance(rec, dict):
        return None
    if str(rec.get("recorded_by", "")).strip() != DELIVERY_PROVENANCE:
        return None  # not host-authored -> untrusted -> ignore (ladder stays at `verified`)
    return rec


def _branch_exists(repo_root: Path, branch: str, git_runner) -> bool:
    try:
        out = git_runner(["ls-remote", "--heads", "origin", branch], repo_root)
    except Exception:
        return False
    return bool(getattr(out, "returncode", 1) == 0 and (out.stdout or "").strip())


def _is_ancestor(repo_root: Path, commit: str, default_ref: str, git_runner) -> bool:
    """merged = the delivery commit is an ancestor of origin/<default>. git exit 0 = ancestor,
    1 = not, 128 = unknown ref (do NOT treat as merged)."""
    if not _SHA_RE.match(commit or ""):
        return False
    try:
        out = git_runner(["merge-base", "--is-ancestor", commit, default_ref], repo_root)
    except Exception:
        return False
    return getattr(out, "returncode", 1) == 0


_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")


def _default_ref(repo_root: Path, git_runner) -> str:
    """origin/HEAD -> the default branch ref. Falls back to origin/main, then origin/master."""
    try:
        out = git_runner(["symbolic-ref", "refs/remotes/origin/HEAD"], repo_root)
        ref = (out.stdout or "").strip()
        if ref.startswith("refs/remotes/"):
            return ref[len("refs/remotes/"):]
    except Exception:
        pass
    for cand in ("origin/main", "origin/master"):
        try:
            out = git_runner(["rev-parse", "--verify", cand], repo_root)
            if getattr(out, "returncode", 1) == 0:
                return cand
        except Exception:
            continue
    return "origin/main"


# ------------------------------------------------------------------ registry (available)

def _available(package: dict, registry_query) -> tuple[bool, str]:
    """available = a published version satisfies the constraint. Network, so it is OFF unless a
    registry_query is injected; without one we abstain (never fabricate `available`)."""
    if not isinstance(package, dict) or registry_query is None:
        name = package.get("name", "?") if isinstance(package, dict) else "?"
        return False, f"waiting: publish {name} (registry check not run)"
    name = str(package.get("name", ""))
    min_version = str(package.get("min_version", ""))
    try:
        ok, latest = registry_query(package)
    except Exception:
        return False, f"waiting: publish {name} >={min_version} (registry unreachable)"
    return (bool(ok), (f"available: {name} {latest} >= {min_version}" if ok
                       else f"waiting: publish {name} >={min_version} (latest {latest})"))


# ------------------------------------------------------------------ the ladder

def evaluate(ref: str, spec_dir: Path, repo_root: Path, *, required: str = "merged",
             package: dict | None = None, safe_load=None, git_runner=None,
             registry_query=None) -> Readiness:
    """Climb the ladder for one cross-repo ref and return the highest rung reached. Pure
    observation; every failure to observe leaves the rung LOWER (never optimistically higher)."""
    if safe_load is None:
        from planning import _safe_load as safe_load  # reuse the symlink-refusing loader
    git_runner = git_runner or _default_git_runner

    detail: dict = {}
    if not spec_dir.is_dir():
        return Readiness(ref, "none", f"unresolved: no spec dir for {ref}", {"error": "no_spec_dir"})

    verified, status = _rung_verified(spec_dir, safe_load)
    detail["spec_status"] = status
    if not verified:
        return Readiness(ref, "none", f"not verified in its tree (status: {status or 'unknown'})", detail)

    rung, observation = "verified", "verified in its own tree"

    delivery = _delivery_record(spec_dir, safe_load)
    if delivery:
        branch = str(delivery.get("branch", "")) or f"builder/{spec_dir.name}"
        commit = str(delivery.get("commit", ""))
        detail["delivery"] = {"branch": branch, "commit": commit}
        if _branch_exists(repo_root, branch, git_runner):
            rung, observation = "delivered", f"delivered on {branch}"
        default_ref = _default_ref(repo_root, git_runner)
        if commit and _is_ancestor(repo_root, commit, default_ref, git_runner):
            rung, observation = "merged", f"merged into {default_ref} ✓"
            detail["merged_into"] = default_ref

    if package is not None:
        ok, obs = _available(package, registry_query)
        detail["package"] = package
        if ok:
            rung, observation = "available", obs
        elif rung in ("merged", "available"):
            observation = obs  # merged but not yet published: surface the publish wait

    result = Readiness(ref, rung, observation, detail)
    detail["required"] = required
    detail["satisfied"] = result.satisfies(required)
    return result


# ------------------------------------------------------------------ CLI (probe one ref, for ops)

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="probe a cross-repo dependency's readiness rung")
    ap.add_argument("spec_dir", help="path to the upstream spec dir")
    ap.add_argument("--repo-root", default=None, help="upstream repo root (default: 3 levels up)")
    ap.add_argument("--required", default="merged", choices=RUNGS)
    args = ap.parse_args(argv)
    spec_dir = Path(args.spec_dir).resolve()
    repo_root = Path(args.repo_root).resolve() if args.repo_root else spec_dir.parents[2]
    r = evaluate(spec_dir.name, spec_dir, repo_root, required=args.required)
    mark = "✓" if r.satisfies(args.required) else "…"
    print(f"{mark} {r.ref}: rung={r.rung} (need {args.required}) — {r.observation}")
    return 0 if r.satisfies(args.required) else 1


if __name__ == "__main__":
    raise SystemExit(main())
