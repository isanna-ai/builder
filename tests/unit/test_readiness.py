"""The readiness ladder is about ORDERING across repos, and its one job is to never report a rung
higher than reality. Every failure to observe must leave the rung LOWER, never optimistically
higher — a false `merged` would let a downstream repo build against a change that isn't on main.
Git is injected as a fake so each rung is deterministic without a real remote.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

_spec = importlib.util.spec_from_file_location("readiness_under_test", SCRIPTS / "readiness.py")
readiness = importlib.util.module_from_spec(_spec)
sys.modules["readiness_under_test"] = readiness
_spec.loader.exec_module(readiness)


def _spec_dir(tmp_path: Path, status="verified", delivery=None) -> Path:
    d = tmp_path / "specs" / "node-entrypoint"
    d.mkdir(parents=True)
    (d / "spec.yaml").write_text(f"status: {status}\n", encoding="utf-8")
    if delivery:
        (d / "delivery.yaml").write_text(delivery, encoding="utf-8")
    return d


def _safe_load(path: Path):
    if not path.exists():
        return None
    from _yaml import yaml
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _git(*, branch_exists=False, is_ancestor_rc=1, default="origin/main"):
    def run(args, cwd):
        cmd = args[0]
        if cmd == "ls-remote":
            return types.SimpleNamespace(returncode=0 if branch_exists else 0,
                                         stdout=("abc123\trefs/heads/x\n" if branch_exists else ""))
        if cmd == "symbolic-ref":
            return types.SimpleNamespace(returncode=0, stdout=f"refs/remotes/{default}\n")
        if cmd == "merge-base":
            return types.SimpleNamespace(returncode=is_ancestor_rc, stdout="")
        return types.SimpleNamespace(returncode=0, stdout="")
    return run


def _eval(spec_dir, **kw):
    kw.setdefault("safe_load", _safe_load)
    return readiness.evaluate("sharedlib/node-entrypoint", spec_dir, spec_dir.parents[1], **kw)


# ---------------------------------------------------------------- the rungs, bottom to top

def test_unverified_is_rung_none(tmp_path):
    d = _spec_dir(tmp_path, status="implementing")
    r = _eval(d, git_runner=_git())
    assert r.rung == "none" and not r.satisfies("merged")


def test_verified_only_stops_at_verified(tmp_path):
    d = _spec_dir(tmp_path, status="verified")  # no delivery.yaml
    r = _eval(d, git_runner=_git())
    assert r.rung == "verified"
    assert not r.satisfies("merged"), "verified in its own tree is NOT consumable across repos"


def test_delivered_when_branch_exists_but_not_merged(tmp_path):
    d = _spec_dir(tmp_path, delivery="recorded_by: builder-delivery\nbranch: builder/node-entrypoint\ncommit: deadbeef\n")
    r = _eval(d, git_runner=_git(branch_exists=True, is_ancestor_rc=1))
    assert r.rung == "delivered" and not r.satisfies("merged")


def test_merged_only_on_is_ancestor_exit_zero(tmp_path):
    d = _spec_dir(tmp_path, delivery="recorded_by: builder-delivery\nbranch: builder/node-entrypoint\ncommit: deadbeef\n")
    r = _eval(d, git_runner=_git(branch_exists=True, is_ancestor_rc=0))
    assert r.rung == "merged" and r.satisfies("merged")


def test_unknown_ref_128_is_never_merged(tmp_path):
    # git exit 128 = it couldn't resolve the ref. That is UNKNOWN, not merged.
    d = _spec_dir(tmp_path, delivery="recorded_by: builder-delivery\nbranch: builder/node-entrypoint\ncommit: deadbeef\n")
    r = _eval(d, git_runner=_git(branch_exists=True, is_ancestor_rc=128))
    assert r.rung == "delivered" and not r.satisfies("merged")


def test_non_sha_commit_cannot_reach_merged(tmp_path):
    # A delivery record whose commit isn't a sha must not be probed as an ancestor.
    d = _spec_dir(tmp_path, delivery="recorded_by: builder-delivery\nbranch: x\ncommit: not-a-sha; rm -rf /\n")
    r = _eval(d, git_runner=_git(branch_exists=True, is_ancestor_rc=0))
    assert r.rung == "delivered" and not r.satisfies("merged")


def test_unmarked_delivery_record_is_untrusted(tmp_path):
    # THE forgery the review found: an agent writes delivery.yaml (its own spec dir) pointing at
    # ANY commit already on origin/main, and is-ancestor would say "merged" for work that never
    # landed. Without the host provenance marker the record is IGNORED -> the ladder stays at
    # `verified`. (The residual same-user marker forgery is the documented tamper-evidence caveat;
    # the real unforgeable tie needs delivery.py to record the actual work-commit.)
    d = _spec_dir(tmp_path, delivery="branch: builder/node-entrypoint\ncommit: deadbeef\n")
    r = _eval(d, git_runner=_git(branch_exists=True, is_ancestor_rc=0))  # git WOULD say ancestor
    assert r.rung == "verified", "an agent-written (unmarked) delivery record must not reach merged"
    assert not r.satisfies("merged")


# ---------------------------------------------------------------- available (package-mediated)

def test_available_requires_a_published_version(tmp_path):
    d = _spec_dir(tmp_path, delivery="recorded_by: builder-delivery\nbranch: x\ncommit: deadbeef\n")
    pkg = {"registry": "npm", "name": "@sharedlib/core", "min_version": "0.9.0"}
    # merged but not published -> stays below available, observation shows the publish wait
    r = _eval(d, git_runner=_git(branch_exists=True, is_ancestor_rc=0), package=pkg,
              registry_query=lambda p: (False, "0.8.1"))
    assert r.rung == "merged" and "waiting: publish" in r.observation
    assert not r.satisfies("available")
    # published >= constraint -> available
    r2 = _eval(d, git_runner=_git(branch_exists=True, is_ancestor_rc=0), package=pkg,
               registry_query=lambda p: (True, "0.9.0"))
    assert r2.rung == "available" and r2.satisfies("available")


def test_no_registry_query_abstains_never_fabricates_available(tmp_path):
    d = _spec_dir(tmp_path, delivery="recorded_by: builder-delivery\nbranch: x\ncommit: deadbeef\n")
    pkg = {"registry": "npm", "name": "@sharedlib/core", "min_version": "0.9.0"}
    r = _eval(d, git_runner=_git(branch_exists=True, is_ancestor_rc=0), package=pkg)  # no query
    assert not r.satisfies("available") and "waiting" in r.observation


def test_missing_spec_dir_is_unresolved(tmp_path):
    r = readiness.evaluate("sharedlib/ghost", tmp_path / "nope", tmp_path, safe_load=_safe_load,
                           git_runner=_git())
    assert r.rung == "none" and "unresolved" in r.observation


def test_satisfies_ladder_ordering():
    r = readiness.Readiness("x", "merged", "", {})
    assert r.satisfies("verified") and r.satisfies("delivered") and r.satisfies("merged")
    assert not r.satisfies("available")


def test_unknown_required_rung_resolves_to_strictest_never_weaker():
    # A typo'd required rung (e.g. `availabl` meant `available`) must NOT silently downgrade to
    # `merged` and let a merged-but-unpublished dep through. Unknown -> strictest.
    merged = readiness.Readiness("x", "merged", "", {})
    assert not merged.satisfies("availabl"), "unknown required must not be satisfied by merged"
    assert not merged.satisfies("bogus")
    available = readiness.Readiness("y", "available", "", {})
    assert available.satisfies("bogus"), "only the top rung satisfies an unknown (strictest) required"
