"""Cross-repo dependency gating (BUILDER_CROSS_REPO_DEPS). The dispatcher gates a
`<alias>/<spec-id>` dep on the readiness ladder (default rung `merged`) instead of the same-repo
`verified`, because a spec verified in ITS OWN tree is invisible to a consumer's build until it
lands on the upstream default branch.

Two invariants matter most: OFF by default is byte-identical (a cross-repo ref is skipped, as before
this feature), and a cross-repo ref is NEVER turned into a path by string concatenation — a
malformed ref or unknown alias is refused, never a traversal. Git is injected as a fake; the
registry is pre-seeded so the projects root is deterministic regardless of any ambient
projects-root environment variable.

No pytest fixtures beyond tmp_path (vendored shim); env is saved/restored by hand.
"""

from __future__ import annotations

import os
import sys
import types
from pathlib import Path

from _dispatch_runtime.config import DispatchConfig, LaneConfig
from _dispatch_runtime.queue_store import QueueStore
from _dispatch_runtime.scheduler import DispatchScheduler

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))
import planning  # noqa: E402


def _config(tmp_path) -> DispatchConfig:
    return DispatchConfig(
        queue_store_path=tmp_path,
        lanes={"codex-cli": LaneConfig(name="codex-cli", provider="codex-cli", max_concurrency=1)},
        routing_policy={"default": "ordered", "tie_break": "lane_order"},
        cooldown_policy={"default_seconds": 60},
        retry_policy={"max_attempts": 3, "initial_seconds": 5, "max_seconds": 30, "jitter_seconds": 0},
    )


def _scheduler(projects_root: Path, consumer_repo: Path) -> DispatchScheduler:
    store = QueueStore(consumer_repo / ".builder" / "dispatch-queue")
    sched = DispatchScheduler(store, _config(consumer_repo), executor=None,
                              owner_id="s", project_dir=consumer_repo)
    # Pre-seed the registry so the projects root is deterministic (not the container's env default).
    sched._xrepo_registry_cache = planning.Registry(projects_root, consumer_repo)
    return sched


def _consumer(tmp_path: Path, dep_line: str) -> tuple[Path, Path]:
    """A consumer repo `appco` with a spec `payments` that depends cross-repo, plus a product.yaml
    declaring the `sharedlib` alias. Returns (projects_root, consumer_repo)."""
    appco = tmp_path / "appco"
    spec = appco / ".builder" / "specs" / "payments"
    spec.mkdir(parents=True)
    (spec / "dependencies.yaml").write_text(f"dependencies:\n{dep_line}\n", encoding="utf-8")
    (appco / ".builder" / "product.yaml").write_text(
        "product: appco\nrepos:\n  - alias: appco\n  - alias: sharedlib\n", encoding="utf-8")
    return tmp_path, appco


def _sharedlib_spec(tmp_path: Path, spec_id: str, status="verified", delivery: str | None = None) -> None:
    d = tmp_path / "sharedlib" / ".builder" / "specs" / spec_id
    d.mkdir(parents=True)
    (d / "spec.yaml").write_text(f"status: {status}\n", encoding="utf-8")
    if delivery:
        (d / "delivery.yaml").write_text(delivery, encoding="utf-8")


def _with_flag(value, fn):
    key = "BUILDER_CROSS_REPO_DEPS"
    saved = os.environ.get(key)
    if value is None:
        os.environ.pop(key, None)
    else:
        os.environ[key] = value
    try:
        return fn()
    finally:
        if saved is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = saved


def _git(is_ancestor_rc=1):
    def run(args, cwd):
        c = args[0]
        if c == "ls-remote":
            return types.SimpleNamespace(returncode=0, stdout="x\trefs/heads/y\n")
        if c == "symbolic-ref":
            return types.SimpleNamespace(returncode=0, stdout="refs/remotes/origin/main\n")
        if c == "merge-base":
            return types.SimpleNamespace(returncode=is_ancestor_rc, stdout="")
        return types.SimpleNamespace(returncode=0, stdout="")
    return run


# ---------------------------------------------------------------- flag OFF = byte-identical

def test_off_by_default_a_cross_repo_dep_is_ignored(tmp_path):
    pr, appco = _consumer(tmp_path, "  - spec: sharedlib/node-entrypoint\n    kind: required")
    _sharedlib_spec(tmp_path, "node-entrypoint", status="implementing")  # NOT ready
    sched = _scheduler(pr, appco)
    # OFF (default): the cross-repo ref is informational -> no unmet dep -> dispatchable.
    unmet, stalled = _with_flag(None, lambda: sched._unmet_dependencies("payments"))
    assert unmet == [] and stalled == []


# ---------------------------------------------------------------- flag ON gates on the ladder

def test_enforce_blocks_when_upstream_is_only_verified_not_merged(tmp_path):
    pr, appco = _consumer(tmp_path, "  - spec: sharedlib/node-entrypoint\n    kind: required")
    _sharedlib_spec(tmp_path, "node-entrypoint", status="verified")  # verified in its OWN tree only
    sched = _scheduler(pr, appco)
    # verified != consumable across repos: readiness tops out at `verified` (no delivery record),
    # which does not satisfy the default `merged` -> the consumer is held.
    unmet, stalled = _with_flag("enforce", lambda: sched._unmet_dependencies("payments"))
    assert unmet == ["sharedlib/node-entrypoint"] and stalled == []


def test_enforce_satisfied_when_merged(tmp_path):
    pr, appco = _consumer(tmp_path, "  - spec: sharedlib/node-entrypoint\n    kind: required")
    _sharedlib_spec(tmp_path, "node-entrypoint", status="verified",
                   delivery="recorded_by: builder-delivery\nbranch: builder/node-entrypoint\ncommit: deadbeef\n")
    sched = _scheduler(pr, appco)
    dep = {"spec": "sharedlib/node-entrypoint", "kind": "required"}
    # inject a fake git that reports is-ancestor -> merged -> satisfied
    satisfied, stalled = sched._cross_repo_dep_state(dep, "sharedlib/node-entrypoint",
                                                     git_runner=_git(is_ancestor_rc=0))
    assert satisfied is True and stalled is False


# ---------------------------------------------------------------- path safety / bad refs

def test_enforce_malformed_ref_is_unmet_and_stalled_never_a_path(tmp_path):
    # `a/b/c` (too many segments), a traversal, and a backslash must all be refused as bad refs
    # (unmet + stalled = needs a human), never turned into a filesystem path.
    pr, appco = _consumer(tmp_path, "  - spec: sharedlib/x\n    kind: required")
    sched = _scheduler(pr, appco)
    for bad in ["a/b/c", "../../etc/passwd", "sharedlib/..", "back\\slash"]:
        satisfied, stalled = sched._cross_repo_dep_state({"spec": bad}, bad)
        assert satisfied is False and stalled is True, f"{bad!r} must be refused"


def test_enforce_bogus_ready_at_is_stalled_not_silently_downgraded(tmp_path):
    # A typo'd ready_at (`availabl` meant `available`, or junk) must NOT silently gate on the weaker
    # `merged`. Even with the upstream fully merged, a misconfigured ready_at is unmet + stalled ->
    # a human fixes the ref.
    pr, appco = _consumer(tmp_path, "  - spec: sharedlib/node-entrypoint\n    ready_at: availabl\n    kind: required")
    _sharedlib_spec(tmp_path, "node-entrypoint", status="verified",
                   delivery="recorded_by: builder-delivery\nbranch: b\ncommit: deadbeef\n")
    sched = _scheduler(pr, appco)
    dep = {"spec": "sharedlib/node-entrypoint", "ready_at": "availabl"}
    satisfied, stalled = sched._cross_repo_dep_state(dep, "sharedlib/node-entrypoint",
                                                     git_runner=_git(is_ancestor_rc=0))  # merged
    assert satisfied is False and stalled is True


def test_enforce_unknown_alias_is_unmet_and_stalled(tmp_path):
    pr, appco = _consumer(tmp_path, "  - spec: nope/spec\n    kind: required")
    sched = _scheduler(pr, appco)
    satisfied, stalled = sched._cross_repo_dep_state({"spec": "nope/spec"}, "nope/spec")
    assert satisfied is False and stalled is True


def test_enforce_dangling_cross_repo_spec_is_unmet(tmp_path):
    # alias resolves, but the spec dir does not exist -> unmet + stalled.
    pr, appco = _consumer(tmp_path, "  - spec: sharedlib/ghost\n    kind: required")
    (tmp_path / "sharedlib" / ".builder" / "specs").mkdir(parents=True)
    sched = _scheduler(pr, appco)
    satisfied, stalled = sched._cross_repo_dep_state({"spec": "sharedlib/ghost"}, "sharedlib/ghost")
    assert satisfied is False and stalled is True
