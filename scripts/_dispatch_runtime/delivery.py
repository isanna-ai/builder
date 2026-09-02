"""Delivery: verified spec -> branch -> PR -> CI-green auto-merge.

After 6-verify succeeds, deliver the spec's implementation as a pull request and
arm GitHub's native auto-merge so the repo's own CI is the real acceptance gate
(Decision 4d). A protected `production` GitHub environment (configured in the
repo, with required reviewers) gates the prod deploy AFTER merge — that manual
approval is intentional and lives in the repo's deploy workflow, not here.

Off by default (`pipeline.deliver.enabled: false`) so it never fires until a
project is explicitly opted in. Pure command construction + an injectable runner
so it is fully unit-testable without touching a real repo.
"""

from __future__ import annotations

import re
import subprocess
from datetime import datetime, timezone
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from _dispatch_runtime.phase_runtime import _safe_yaml
from _dispatch_runtime.paths import runtime_dir

# The provenance marker the readiness ladder (scripts/readiness.py) requires before it will trust a
# delivery record for the `merged` rung. Keep in sync with readiness.DELIVERY_PROVENANCE.
DELIVERY_PROVENANCE = "builder-delivery"
_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")


class _DefaultRunner:
    def run(self, argv: list[str], cwd: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(argv, cwd=cwd, text=True, capture_output=True, check=False)


def write_delivery_record(project_dir: Path, spec_id: str, branch: str, commit: str, *,
                          base: str = "", pr_url: str = "", recorded_at: str | None = None) -> Path | None:
    """Write the HOST-authored `<spec>/delivery.yaml` the readiness ladder's `merged` rung reads.

    This is the piece that makes `merged` trustworthy. `deliver()` runs host-side and AUTHORED the
    commit itself (from the spec's scoped, verified work), so the `commit` recorded here is the real
    work-commit — the agent never supplies it. The record carries the provenance marker; an
    agent-written delivery.yaml (no marker) is ignored by readiness. The residual — an agent forging
    the marker as the same OS user — is the project's standard tamper-EVIDENT caveat: the load-bearing
    guarantee is that this commit genuinely delivered the spec and that reaching origin/<default>
    required branch protection, a gate outside every agent lane."""
    if not spec_id or spec_id in (".", "..") or "/" in spec_id or "\\" in spec_id:
        return None
    if not _SHA_RE.match(commit or ""):
        return None
    spec_dir = runtime_dir(Path(project_dir)) / "specs" / spec_id
    if not spec_dir.is_dir():
        return None
    recorded_at = recorded_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    lines = [
        "# Host-authored by delivery.py. The readiness ladder's `merged` rung reads `commit`;",
        "# an agent-written record without `recorded_by: builder-delivery` is ignored.",
        f"recorded_by: {DELIVERY_PROVENANCE}",
        f"spec: {spec_id}",
        f"branch: {branch}",
        f"commit: {commit}",
    ]
    if base:
        lines.append(f"base: {base}")
    if pr_url:
        lines.append(f"pr_url: {pr_url}")
    lines.append(f"recorded_at: {recorded_at}")
    path = spec_dir / "delivery.yaml"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def branch_name_for(spec_id: str, deliver_cfg: dict[str, Any] | None = None) -> str:
    """The delivery branch name for a spec. Shared by `deliver()` and the R5 per-spec
    worktree (scheduler.py `_ensure_worktree`) so an isolated worktree is checked out
    on the SAME branch delivery will later push — making delivery's `checkout -B` a
    provable no-op rather than a coincidence."""
    cfg = deliver_cfg or {}
    return (cfg.get("branch_prefix") or "builder/") + spec_id


def _scoped_add_paths(project_dir: Path, spec_id: str) -> tuple[list[str], bool]:
    """R5 scoped delivery: the file paths this spec actually touched, sourced from
    traceability.yaml (`task_links[].files[].path`) plus handoff.yaml
    (`files_written`), deduped and order-preserving. Returns (paths, ok) where
    ok=False means NEITHER source yielded a single path (missing/empty/malformed
    control files) — the caller falls back to `git add -A` rather than silently
    delivering nothing. Never raises: a malformed traceability/handoff file is
    treated the same as an absent one (via `_safe_yaml`'s own except-all).

    Model A: `.builder/specs/<spec_id>` is redirected to a symlink into the
    shared MAIN control dir inside an isolated worktree (scheduler
    `_ensure_worktree`/`_redirect_spec_control_dir`). Control state is canonical
    in main regardless of what the source PR ships, so any path under that
    control dir is defensively dropped here — scoped delivery never explicitly
    stages it, even if a traceability/handoff record were ever to list it."""
    spec_dir = runtime_dir(Path(project_dir)) / "specs" / spec_id
    control_prefix = f"{runtime_dir(Path(project_dir)).name}/specs/{spec_id}"
    paths: list[str] = []
    seen: set[str] = set()

    def _add(value: Any) -> None:
        text = str(value).strip()
        if not text or text in seen:
            return
        # L-A: normalize BEFORE the control-dir filter, so a `./`-prefixed,
        # absolute, or `..`-traversal path can never slip past it (or reach
        # `git add` as a bogus/misleading path). Strips backslashes -> `/` and
        # any number of leading `./`; an absolute path or one containing a `..`
        # segment is dropped outright as out-of-scope for a delivery PR either
        # way (never staged, scoped or not).
        normalized = text.replace("\\", "/")
        while normalized.startswith("./"):
            normalized = normalized[2:]
        if not normalized or normalized.startswith("/") or ".." in normalized.split("/"):
            return
        if normalized == control_prefix or normalized.startswith(control_prefix + "/"):
            return  # Model A: never stage the spec's own (symlinked) control dir
        seen.add(text)
        paths.append(normalized)

    trace = _safe_yaml(spec_dir / "traceability.yaml")
    if isinstance(trace, dict):
        for link in trace.get("task_links") or []:
            if not isinstance(link, dict):
                continue
            for file_entry in link.get("files") or []:
                if isinstance(file_entry, dict):
                    _add(file_entry.get("path", ""))

    handoff = _safe_yaml(spec_dir / "handoff.yaml")
    if isinstance(handoff, dict):
        for value in handoff.get("files_written") or []:
            _add(value)

    return paths, bool(paths)


@dataclass
class DeliveryResult:
    ok: bool
    branch: str
    pr_url: str = ""
    reason: str = ""
    steps: list[str] = field(default_factory=list)


def _default_base(runner, cwd: str) -> str:
    """The repo's default branch (origin/HEAD), falling back to main."""
    p = runner.run(["git", "rev-parse", "--abbrev-ref", "origin/HEAD"], cwd)
    out = (p.stdout or "").strip()
    if p.returncode == 0 and "/" in out:
        return out.split("/", 1)[1]
    return "main"


def deliver(project_dir: Path, spec_id: str, deliver_cfg: dict[str, Any],
            *, summary: str = "", runner: Any | None = None, scoped: bool = False) -> DeliveryResult:
    """`scoped` (R5, default False): when True, `git add` only the paths
    traceability.yaml/handoff.yaml say this spec touched (falling back to `git add
    -A` if neither yields a path), and skip `checkout -B` when the tree already
    reports being on the delivery branch (the per-spec isolated worktree case).
    False (the default) is byte-identical to the pre-R5 behavior: `checkout -B` +
    `git add -A` unconditionally.

    H4/L1 (Model A fail-closed): each scoped `git add -- <path>` is run
    individually (the `--` separator guards a path that could otherwise be
    misread as an option) and its returncode is checked; if ANY of them fails,
    the scoped add is ABORTED and the commit falls back to `git add -A`
    (H-2: scoped to `-- ':(exclude).builder'`, never plain `-A`) instead of
    proceeding with a partial stage — a spec's isolated worktree is often the
    only copy of its uncommitted work, so shipping (and then discarding, via
    `_cleanup_worktree`) a partial commit would silently lose the rest of it.

    Model A note: under `pipeline.worktree_isolation`, `.builder/specs/<id>`
    inside the worktree is a symlink to the shared MAIN control dir (control
    state lives in main regardless of delivery). `_scoped_add_paths` defensively
    drops any path under that control dir, so the scoped list never explicitly
    stages it. H-2 (destructive, empirically verified): a `git add -A` fallback
    inside the worktree WOULD also stage that symlink itself (mode 120000) plus
    a `D` for every tracked control file under it — and after the delivery PR's
    auto-merge, main's OWN `.builder/specs/<id>` becomes a symlink pointing at
    itself (ELOOP), destroying the real control files. Both `add -A` fallbacks
    below therefore use the `:(exclude).builder` pathspec magic so the WHOLE
    `.builder` subtree (not just the spec's own control dir) is excluded, no
    matter what a traceability/handoff record could ever list. This is a
    deliberate change from the pre-R5 behavior, where the unconditional
    `add -A` swept control files (spec state, traceability, handoff) into the
    delivery PR too — the non-scoped (`scoped=False`, flag-off) path below is
    untouched and still runs a plain `add -A`."""
    runner = runner or _DefaultRunner()
    cwd = str(project_dir)
    branch = branch_name_for(spec_id, deliver_cfg)
    base = deliver_cfg.get("base") or _default_base(runner, cwd)
    squash = deliver_cfg.get("squash", True)
    auto_merge = deliver_cfg.get("auto_merge", True)
    steps: list[str] = []

    def run(argv: list[str], *, allow_fail: bool = False) -> subprocess.CompletedProcess[str]:
        steps.append(" ".join(argv))
        p = runner.run(argv, cwd)
        if p.returncode != 0 and not allow_fail:
            raise RuntimeError(f"`{' '.join(argv)}` failed (rc={p.returncode}): {(p.stderr or p.stdout or '').strip()[:300]}")
        return p

    title = f"{spec_id}: {summary}" if summary else f"{spec_id}: autonomous Builder delivery"
    body = (
        f"Autonomous Builder delivery of `{spec_id}` (specify→design→review→plan→implement→verify).\n\n"
        f"CI-green is the acceptance gate; auto-merge {'armed' if auto_merge else 'not armed'}. "
        f"Prod deploy remains gated by the protected `production` environment."
    )
    try:
        if scoped:
            # R5: a per-spec isolated worktree is typically ALREADY checked out on
            # `branch` (scheduler._ensure_worktree used the same branch_name_for) —
            # re-branching here would be redundant at best; only checkout if the
            # tree is NOT already on it (defensive: covers a worktree-provisioning
            # fallback to the shared project_dir).
            current = run(["git", "rev-parse", "--abbrev-ref", "HEAD"], allow_fail=True)
            on_branch = current.returncode == 0 and (current.stdout or "").strip() == branch
            if not on_branch:
                run(["git", "checkout", "-B", branch])
            paths, ok = _scoped_add_paths(project_dir, spec_id)
            if ok:
                add_failed = False
                for path in paths:
                    added = run(["git", "add", "--", path], allow_fail=True)
                    if added.returncode != 0:
                        add_failed = True
                        break  # H4: abort the scoped path on the first failure
                if add_failed:
                    steps.append(
                        "(scoped add: a path failed to stage -> aborting scoped add, "
                        "falling back to git add -A)"
                    )
                    # H-2: NEVER a plain `git add -A` here — the worktree's
                    # `.builder/specs/<id>` is a symlink into main (Model A);
                    # `-A` would stage the symlink itself (120000) plus a `D`
                    # for every tracked control file under it, and after
                    # auto-merge main's OWN control dir becomes a symlink to
                    # itself (ELOOP), destroying the real spec.yaml/etc.
                    run(["git", "add", "-A", "--", f":(exclude){runtime_dir(Path(project_dir)).name}"])
                else:
                    steps.append(f"(scoped add: {len(paths)} path(s) from traceability/handoff)")
            else:
                steps.append("(scoped delivery: no traceability/handoff paths found -> falling back to git add -A)")
                run(["git", "add", "-A", "--", f":(exclude){runtime_dir(Path(project_dir)).name}"])  # H-2: see above
        else:
            run(["git", "checkout", "-B", branch])
            run(["git", "add", "-A"])
        # H-1: determine "nothing to commit" by STAGED state, not commit
        # stderr — a scoped worktree is permanently dirty (the untracked
        # `.builder` control symlink), so when scoped adds stage nothing NEW,
        # real `git commit` returns rc=1 with "no changes added to commit"/
        # "nothing added to commit but untracked files present", which the
        # string tolerance below does NOT recognize, raising and permanently
        # blocking this spec's delivery. `git diff --cached --quiet` (rc=0 =>
        # nothing staged) catches this BEFORE calling commit at all and treats
        # it as the same idempotent no-op the string tolerance already handles
        # — skip the commit and proceed straight to push/PR. Gated to `scoped`
        # only so the non-scoped (flag-off) step log is untouched.
        skip_commit = False
        if scoped:
            staged = run(["git", "diff", "--cached", "--quiet"], allow_fail=True)
            if staged.returncode == 0:
                skip_commit = True
                steps.append("(scoped delivery: nothing staged -> skipping commit, idempotent no-op)")
        if not skip_commit:
            # Commit; tolerate "nothing to commit" so re-delivery is idempotent
            # (belt-and-suspenders alongside the pre-check above).
            commit = run(["git", "commit", "-m", title], allow_fail=True)
            if commit.returncode != 0 and "nothing to commit" not in (commit.stdout or "" ) + (commit.stderr or ""):
                raise RuntimeError(f"git commit failed: {(commit.stderr or commit.stdout or '').strip()[:300]}")
        run(["git", "push", "-u", "origin", branch])
        pr = run(["gh", "pr", "create", "--title", title, "--body", body, "--base", base, "--head", branch],
                 allow_fail=True)
        pr_url = (pr.stdout or "").strip().splitlines()[-1] if pr.returncode == 0 and pr.stdout else ""
        if pr.returncode != 0:
            # A PR may already exist for this branch — look it up rather than fail.
            existing = run(["gh", "pr", "view", branch, "--json", "url", "-q", ".url"], allow_fail=True)
            pr_url = (existing.stdout or "").strip() if existing.returncode == 0 else ""
            if not pr_url:
                raise RuntimeError(f"gh pr create failed: {(pr.stderr or '').strip()[:300]}")
        if auto_merge:
            merge_argv = ["gh", "pr", "merge", branch, "--auto"]
            merge_argv.append("--squash" if squash else "--merge")
            run(merge_argv)
        # Record the HOST-authored delivery commit so the readiness ladder can observe `merged`.
        # The commit is read back from git (rev-parse HEAD after our own push), never from the
        # agent. Best-effort: a runner that cannot report a sha simply leaves no record (the ladder
        # then tops out at `verified`), so this never fails an otherwise-successful delivery.
        head = run(["git", "rev-parse", "HEAD"], allow_fail=True)
        sha = (head.stdout or "").strip()
        if _SHA_RE.match(sha):
            # Only record a GENUINELY PENDING delivery: a commit that is NEW work, not yet on the
            # DEFAULT branch. deliver() tolerates a no-op commit (a re-delivery, or an existing
            # branch), after which HEAD could be an already-merged / unrelated tip; recording that
            # would let readiness observe `merged` for work this run never delivered.
            #
            # Check against `origin/<default>` -- the SAME ref readiness's `merged` rung checks --
            # NOT the config `base`: a manipulated/stale `base` (behind the default) would make an
            # already-merged unrelated commit look "ahead", then readiness would still see it on the
            # default and report `merged` (adversarial review, final round). is-ancestor rc: 1 = not
            # yet on the default (genuine pending delivery, record it); 0 = already on the default
            # (skip); 128/other = unknown ref -> FAIL CLOSED, skip. A missed record only
            # under-reports (tops out at `delivered`); it never forges `merged`.
            merged_ref = "origin/" + _default_base(runner, cwd)
            ahead = run(["git", "merge-base", "--is-ancestor", sha, merged_ref], allow_fail=True)
            if ahead.returncode == 1:
                write_delivery_record(project_dir, spec_id, branch, sha, base=base, pr_url=pr_url)
                steps.append(f"(delivery record written: {sha[:12]})")
            else:
                steps.append("(delivery record skipped: HEAD is not a new commit ahead of the base)")
        return DeliveryResult(ok=True, branch=branch, pr_url=pr_url, steps=steps)
    except RuntimeError as exc:
        return DeliveryResult(ok=False, branch=branch, reason=str(exc), steps=steps)
