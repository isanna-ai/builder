"""SSOT readiness per repo — the measurement backfill is tracked against.

Builder introduced the SSOT/sync layer after several repos were already brownfield, so they
were never bootstrapped. Nothing reported that: `isanna sync` fails closed per spec with
`bootstrap_required`, which only surfaces if someone happens to run it on a spec that reached
the sync phase. Across a real portfolio that measured as zero of dozens of active specs ever
synced, in repos that carried no adapter, no curated behavioral SSOT and no published model --
and nothing anywhere said so.

This module answers, per repo: can it sync at all, and how much finished work never did.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from _dispatch_runtime.paths import RUNTIME_DIR_NAMES, runtime_dir
from _dispatch_runtime.staged_gate import staged_gate_enforced

# Statuses at which a spec's implementation work is over. Reaching one of these WITHOUT a
# sync-result is the shape this audit exists to surface: finished work that never updated the
# SSOT. `archived` is included deliberately -- archiving past sync is exactly the hole the
# hard-refuse rule closes.
FINISHED_STATUSES = frozenset({
    "verified", "verified_with_tasks", "synced", "syncing", "archived",
})

_STATUS_RE = re.compile(r"^status:\s*[\"']?([A-Za-z0-9_-]+)", re.MULTILINE)


@dataclass
class RepoSsotAudit:
    repo: str
    wired: bool = False
    has_adapter: bool = False
    # PRESENCE of docs/system-behaviors.yaml, and nothing more -- see sync_blocked. Presence is
    # not coverage: one repo carried 5 entries against 78,401 lines while two others carried 78
    # and 106, and all three rendered as `True`. Read
    # `behavior_count` to judge whether a repo's SSOT is real.
    has_behaviors: bool = False
    behavior_count: int = 0
    # Present but declaring NOTHING -- the one objectively-stub case. It satisfies sync's
    # presence check and unblocks the repo while asserting nothing about the system. A file we
    # could not PARSE is a different thing (has_behaviors true, count 0, this false) and is
    # reported separately rather than being called empty: it may well contain plenty.
    behaviors_empty: bool = False
    has_model: bool = False
    spec_count: int = 0
    synced_count: int = 0
    finished_never_synced: list[str] = field(default_factory=list)
    awaiting_sync: list[str] = field(default_factory=list)
    # A PARTITION of finished_never_synced. Anything double-counted or dropped makes the
    # totals lie, and the totals are what the audit is for.
    unsynced_actionable: list[str] = field(default_factory=list)
    historical_no_provenance: list[str] = field(default_factory=list)
    # Specs the audit could not read the identity of. Counted in spec_count and deliberately
    # placed in NO bucket: an unreadable spec has no known status, so calling it finished,
    # historical or actionable would be inventing evidence. Reported loudly instead, because a
    # census that quietly shrinks is worse than one that errors -- nothing prompts anyone to look.
    blind: list[str] = field(default_factory=list)

    @property
    def sync_blocked(self) -> bool:
        """Mirrors builder's OWN precondition rather than restating it: `bootstrap_required`
        fires when the adapter is missing OR `docs/system-behaviors.yaml` is absent. A missing
        published model is a real gap, reported separately, but is NOT what blocks sync --
        conflating the two would send someone to fix the wrong thing.

        Deliberately NOT tightened to require real coverage. `isanna sync` decides
        bootstrap_required on `not ssot.is_file()` -- presence, nothing more. An audit that
        refused a five-entry file would report BLOCKED for a repo where sync actually proceeds:
        the same lie as the one `behavior_count` exists to fix, pointing the other way. Coverage
        is reported as a number beside this flag, never folded into it."""
        return not (self.has_adapter and self.has_behaviors)


# A directory under specs/ is judged a SPEC by whether it carries canonical spec artifacts,
# not by whether spec.yaml happens to be readable at this instant. Without this, a spec whose
# identity file cannot be opened is indistinguishable from a stray directory, and gets skipped.
_SPEC_SHAPE_ARTIFACTS = (
    "spec.yaml", "tasks.yaml", "requirements.yaml", "design.yaml",
    "phase-log.yaml", "handoff.yaml", "traceability.yaml",
)


def _looks_like_a_spec(entry: Path) -> bool:
    try:
        return any((entry / name).is_file() for name in _SPEC_SHAPE_ARTIFACTS)
    except OSError:
        return False


def _declared_status(spec_dir: Path) -> str | None:
    """The spec's declared status, or None when it CANNOT BE DETERMINED.

    None and "" are different answers and the distinction is load-bearing. This used to return
    "" for an unreadable file, a missing file AND a spec.yaml with no status key; "" then simply
    failed the FINISHED_STATUSES test, so the spec fell out of every bucket without a word.
    Observed on a real repo whose filesystem was intermittently failing reads: the
    audit reported 25 specs instead of 26 and dropped the affected spec entirely, while exiting
    0. Callers must be able to tell "this spec is not finished" from "I could not read it"."""
    path = spec_dir / "spec.yaml"
    try:
        if not path.is_file():
            return None
        match = _STATUS_RE.search(path.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return None
    return match.group(1) if match else None


def _behavior_count(behaviors_path: Path) -> int | None:
    """How many behaviours `docs/system-behaviors.yaml` actually declares.

    None means the file is there but could not be parsed -- distinct from 0, which means it
    parsed and declares nothing. Conflating them would report a large, merely-malformed SSOT as
    a stub, sending someone to write behaviours that already exist."""
    try:
        from _yaml import yaml  # type: ignore

        data = yaml.safe_load(behaviors_path.read_text(encoding="utf-8", errors="replace"))
    except Exception:  # noqa: BLE001 - an unparseable SSOT is never a crash in an audit
        return None
    if not isinstance(data, dict):
        return None
    behaviors = data.get("behaviors")
    if behaviors is None:
        return 0
    if not isinstance(behaviors, list):
        return None
    return sum(1 for item in behaviors if isinstance(item, dict))


def worktree_parent(repo_root: Path) -> Path | None:
    """The main checkout a git WORKTREE belongs to, or None when this is not a worktree.

    A worktree's `.git` is a FILE holding `gitdir: <main>/.git/worktrees/<name>`; a normal
    clone's `.git` is a directory. Never raises and never guesses: a `.git` file that cannot be
    read or parsed is reported as an ordinary repo, because silently attaching a directory to
    some other repo's totals is worse than counting it on its own."""
    marker = repo_root / ".git"
    try:
        if not marker.is_file():
            return None
        text = marker.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    if not text.startswith("gitdir:"):
        return None
    gitdir = Path(text[len("gitdir:"):].strip())
    # <main>/.git/worktrees/<name> -> <main>. Anything else is a shape we do not recognise.
    parts = gitdir.parts
    if len(parts) < 4 or parts[-2] != "worktrees" or parts[-3] != ".git":
        return None
    return Path(*parts[:-3])


@dataclass(frozen=True)
class SkippedWorktree:
    name: str
    parent: str
    # False means the main checkout is NOT in the audited set, so skipping this worktree really
    # does drop its specs from the census. Loud, because that is the only case where excluding
    # a worktree loses coverage rather than removing a duplicate.
    parent_audited: bool


def enumerate_fleet(base: Path) -> tuple[list[Path], list[SkippedWorktree]]:
    """Builder-wired repos under `base`, with git worktrees separated out rather than counted.

    A worktree is not a repo, and it carries its main checkout's `.builder/specs` tree -- so
    counting both double-counts every spec in it. That is how this census once overstated a
    fleet total by 20%. A backlog number that overstates itself gets discounted by whoever
    reads it, and then the real number is not believed either.

    Worktrees are RETURNED, not dropped on the floor: silently skipping them would be the same
    silence this module was just fixed for, pointing the other way."""
    base = Path(base)
    try:
        entries = sorted(base.iterdir(), key=lambda p: p.name)
    except OSError:
        return [], []
    wired = [p for p in entries if p.is_dir() and (runtime_dir(p) / "specs").is_dir()]
    parents = {p: worktree_parent(p) for p in wired}
    roots = [p for p in wired if parents[p] is None]
    resolved_roots = {r.resolve() for r in roots}
    skipped = [
        SkippedWorktree(
            name=p.name,
            parent=parents[p].name,
            parent_audited=parents[p].resolve() in resolved_roots,
        )
        for p in wired if parents[p] is not None
    ]
    return roots, skipped


def _has_valid_adapter(repo_root: Path) -> bool:
    """Presence is not enough: `_sync.adapter.adapter_for_repo` requires `artifact:
    sync-adapter` AND a list of `mappings`. Counting a file that merely exists would report
    readiness a real sync would then refuse."""
    try:
        from _sync.adapter import adapter_for_repo
    except ImportError:
        return False
    try:
        return adapter_for_repo(repo_root) is not None
    except Exception:  # noqa: BLE001 - an unreadable adapter is an absent one, never a crash
        return False


@dataclass
class AdapterCoverage:
    adapter_present: bool
    mapping_count: int
    checked_paths: int
    unmapped: list[str]
    observed_targets: list[str]
    vacuous: bool

    @property
    def covered(self) -> bool:
        """Zero unmapped over a NON-EMPTY path set. An empty set trivially has no gaps, and
        calling that covered is the unearned green this project exists to refuse -- nothing was
        checked, so nothing was proven."""
        return self.adapter_present and not self.vacuous and not self.unmapped


def _source_paths_only(changed_paths: list[str]) -> set[str]:
    """The subset sync would actually see, mirroring `_git_source_paths` in lane_common.

    Sync drops every path under a runtime dir, so spec artifacts, intents and the published
    model are never part of a spec's change surface. Feeding them to coverage counts paths the
    adapter is not supposed to map and inflates the unmapped total with pure spec-artifact
    churn -- measured on builder, 242 raw paths against 205 real ones.

    RUNTIME_DIR_NAMES is imported, never restated: a second copy of the list would drift from
    the filter this is meant to mirror. Only a LEADING `<runtime>/` segment counts, so
    `src/.builder-notes.md` is source and stays."""
    prefixes = tuple(f"{name}/" for name in RUNTIME_DIR_NAMES)
    return {
        text for text in (str(p).strip() for p in changed_paths)
        if text and not text.startswith(prefixes)
    }


def adapter_coverage(repo_root: Path, changed_paths: list[str]) -> AdapterCoverage:
    """Whether the repo's adapter maps every one of `changed_paths`.

    Worth proving BEFORE enabling sync, and against REAL paths. `observed_tuples` turns any
    unmatched path into `{capabilities, unmapped:<path>, enrich}` -- a tuple no spec declared --
    so sync flags it as divergence. An incomplete adapter blocks sync instead of weakening it,
    and without this check the fault surfaces at the end of a spec's lifecycle phrased as "this
    spec diverged" when the real defect is a hole in a repo-level file.

    A `tuples: []` mapping counts as covered: that is the documented "recognized, makes no
    claim" pattern for shared entry points. Requiring a claim in order to count as covered is
    exactly how blanket mappings and stale tuples get authored.
    """
    repo_root = Path(repo_root)
    paths = sorted(_source_paths_only(changed_paths or []))
    try:
        from _sync.adapter import adapter_for_repo

        adapter = adapter_for_repo(repo_root)
    except Exception:  # noqa: BLE001 - an unreadable adapter is an absent one
        adapter = None
    if adapter is None:
        return AdapterCoverage(False, 0, len(paths), list(paths), [], not paths)

    rows = adapter.observed_tuples(paths) if paths else []
    unmapped = sorted(
        row["target"][len("unmapped:"):]
        for row in rows if str(row.get("target", "")).startswith("unmapped:")
    )
    targets = sorted({
        row["target"] for row in rows if not str(row.get("target", "")).startswith("unmapped:")
    })
    return AdapterCoverage(
        adapter_present=True, mapping_count=len(adapter.mappings), checked_paths=len(paths),
        unmapped=unmapped, observed_targets=targets, vacuous=not paths,
    )


@dataclass
class ArchiveSyncVerdict:
    allowed: bool
    enforced: bool
    reason: str


def _archive_gate_enforced(repo_root: Path | None = None) -> bool:
    """Resolution is env, then the repo's own `pipeline.archive_require_sync`, then warn --
    delegated to the SHARED resolver so this gate and the ssot-delta advancement gate cannot
    drift apart. See _dispatch_runtime.staged_gate for the ordering rationale.

    Default stays warn: most repos cannot sync at all yet, so defaulting to enforce would
    freeze archiving fleet-wide for a backfill that has not happened."""
    return staged_gate_enforced("BUILDER_ARCHIVE_REQUIRE_SYNC", repo_root, "archive_require_sync")


def archive_sync_gate(repo_root: Path, spec_id: str) -> ArchiveSyncVerdict:
    """Whether `spec_id` may be archived, given that archiving past sync loses the SSOT update
    permanently -- the spec's declared delta is never reconciled and nothing later notices.

    The reason distinguishes three cases, because they need three different responses and
    reporting the wrong one sends the reader to the wrong file -- or, worse, on an errand that
    cannot succeed:

      * the spec can NEVER sync at spec level (no `gate-evidence/`) -- not a task at all
      * the repo cannot sync at all (not bootstrapped) -- fix the repo
      * the spec simply has not synced yet -- run the sync phase

    The first case was missing, and its absence wedged specs. Observed on
    a spec right after promotion: this gate said "run the sync phase" for a
    spec whose sync phase can never succeed, while `isanna ssot audit` said "can never sync at
    spec level" about the same spec. Under `archive_require_sync: enforce` it could then neither
    sync nor be archived. An impossible instruction reads as a task, gets attempted, fails, and
    spends the reader's trust in every other thing this gate says."""
    repo_root = Path(repo_root)
    enforced = _archive_gate_enforced(repo_root)
    spec_dir = runtime_dir(repo_root) / "specs" / spec_id

    if not (spec_dir / "spec.yaml").is_file():
        return ArchiveSyncVerdict(
            allowed=not enforced, enforced=enforced,
            reason=f"no spec at {spec_dir} -- refusing rather than assuming it is fine")
    if (spec_dir / "sync-result.yaml").is_file():
        return ArchiveSyncVerdict(allowed=True, enforced=enforced, reason="")

    audit = audit_repo(repo_root)
    if not (spec_dir / "gate-evidence").is_dir():
        # Checked FIRST and deliberately: this fact does not change. Bootstrapping the repo
        # will not make this spec syncable either, so leading with the repo-level blocker would
        # be impossible advice one level up. `allowed` is untouched -- saying WHY a refusal
        # stands is not the same as deciding it should lift, and whether this class may be
        # archived is an owner policy call.
        reason = (f"{spec_id} never synced and can NEVER sync at spec level: no gate-evidence/, "
                  f"so `isanna sync-readmit` refuses it with unsafe-evidence-directory. Running "
                  f"the sync phase will not help and no command fixes this -- the spec's verify "
                  f"provenance does not exist and cannot be rebuilt. Reconciling it is a "
                  f"release-level decision (owner adoption); see the Sync Preconditions section "
                  f"of standards/builder-contract.md.")
        if audit.sync_blocked:
            # Both facts are true and both matter. Lead with the permanent one -- bootstrapping
            # will not make THIS spec syncable -- but do not hide the repo-level blocker, which
            # still governs every future spec in this repo.
            reason += (f" Separately, {audit.repo} is not bootstrapped at all (missing "
                       f".builder/sync-adapter.yaml and/or docs/system-behaviors.yaml), which "
                       f"blocks its FUTURE specs even though it is not what blocks this one.")
    elif audit.sync_blocked:
        reason = (f"{spec_id} never synced, and {audit.repo} cannot sync at all: missing "
                  f".builder/sync-adapter.yaml and/or docs/system-behaviors.yaml "
                  f"(`isanna sync` fails closed with bootstrap_required). Bootstrap the repo "
                  f"before archiving finished work past its SSOT update.")
    else:
        reason = (f"{spec_id} never synced -- no sync-result.yaml. The repo IS bootstrapped, so "
                  f"run the sync phase; archiving now loses this spec's SSOT update permanently.")
    return ArchiveSyncVerdict(allowed=not enforced, enforced=enforced, reason=reason)


def audit_repo(repo_root: Path) -> RepoSsotAudit:
    """Readiness for one repo. Never raises: a repo that cannot be read is reported as unwired,
    because a crash in an audit is indistinguishable from a repo with nothing to report."""
    repo_root = Path(repo_root)
    result = RepoSsotAudit(repo=repo_root.name)

    specs_root = runtime_dir(repo_root) / "specs"
    result.wired = specs_root.is_dir()
    result.has_adapter = _has_valid_adapter(repo_root)
    behaviors_path = repo_root / "docs" / "system-behaviors.yaml"
    result.has_behaviors = behaviors_path.is_file()
    if result.has_behaviors:
        counted = _behavior_count(behaviors_path)
        result.behavior_count = counted or 0
        result.behaviors_empty = counted == 0  # None (unparseable) is NOT empty
    result.has_model = (runtime_dir(repo_root) / "model" / "system-model.yaml").is_file()
    if not result.wired:
        return result

    try:
        entries = sorted(specs_root.iterdir(), key=lambda p: p.name)
    except OSError:
        return result
    for entry in entries:
        if not entry.is_dir() or entry.name == "archive":
            continue
        if not _looks_like_a_spec(entry):
            # No spec shape at all -- a stray directory, not a spec. Skipped rather than called
            # blind: false blindness is its own unearned alarm, and an audit that cries wolf is
            # ignored exactly when it finally reports something real.
            continue
        result.spec_count += 1
        status = _declared_status(entry)
        if status is None:
            # Counted, never bucketed, always reported. Everything below this line reads files
            # whose meaning depends on a status we do not have.
            result.blind.append(entry.name)
            continue
        synced = (entry / "sync-result.yaml").is_file()
        if synced:
            result.synced_count += 1
        if status in FINISHED_STATUSES and not synced:
            result.finished_never_synced.append(entry.name)
            # Sync goes forward only, and that is forced rather than chosen. Readmission
            # rebuilds provenance from the host-recorded verify-bundle chain, and
            # `readmit.py` raises `unsafe-evidence-directory` when a spec has no
            # `gate-evidence/` directory -- so a spec without one can NEVER be synced at
            # spec level, however well its repo's adapter is curated. Those are reconciled
            # at release level (see the owner-adoption mechanism), not chased.
            #
            # Reachability, not chain validity: readmit validates the chain separately, and
            # calling a spec unreadmittable when readmit would merely reject its chain would
            # point the reader at the wrong repair.
            if (entry / "gate-evidence").is_dir():
                result.unsynced_actionable.append(entry.name)
            else:
                result.historical_no_provenance.append(entry.name)
        # A spec carrying an ssot-delta has already done its half of the contract and is
        # ready to sync the moment the repo is bootstrapped -- the backfill queue.
        if (entry / "ssot-delta.yaml").is_file() and not synced:
            result.awaiting_sync.append(entry.name)
    return result
