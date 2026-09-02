from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from secrets import token_hex
from typing import Any

from _dispatch_runtime import gate_evidence
from _sync.evidence import (
    changed_paths_digest,
    sha256_bytes,
    validate_readmission_verify_bundle,
    verified_tree_digest,
)
from _sync.locking import SpecMutationBusy, spec_mutation_lock
from _sync.publish import atomic_publish
from _validators.common import load_schema, parse_yaml_like_file, validate_schema

REPORT_SCHEMA = "sync-readmission-report/v1"
BOOTSTRAP_CUTOFF = datetime(2026, 7, 20, 23, 59, 59, tzinfo=timezone.utc)
BOOTSTRAP_AUTHORIZATION = (
    "OWNER DECISION — one-time provenance exception AUTHORIZED (verb 2 of the blocker options). "
    "Scope: bootstrap-era specs only (host-verified before the sync runtime existed, i.e. verify "
    "bundles written 2026-07-20 or earlier that lack an immutable snapshot identity). Mechanism: "
    "the host-recorded verify bundle chain (post-turn HEAD + bundle sha256 + host-recorded dirty-tree "
    "summaries) is accepted as sufficient verified-snapshot identity FOR RE-ADMISSION BASELINE DERIVATION "
    "ONLY. Constraints: (a) this never applies to specs verified under the sync runtime — they carry real "
    "immutable identities and MUST use them; (b) the derived baseline and the exception itself must be "
    "recorded verbatim in the re-admitted spec sync-scope evidence and sync-result (provenance: "
    "bootstrap-exception), so the map records the weaker provenance honestly rather than laundering it; "
    "(c) the exception expires when the bootstrap-era portfolio is re-admitted — the tool must refuse it "
    "afterward. Proceed to planning with sc-3/R6 satisfiable under this mechanism; R3 fail-closed rules "
    "stay intact for all non-bootstrap evidence."
)

_SPEC_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_GIT_SHA = re.compile(r"^[0-9a-f]{40}$")


@dataclass
class ReadmitFailure(Exception):
    code: str
    detail: str


def _run_git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[bytes]:
    result = subprocess.run(
        ["git", "-C", str(root), *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )
    if check and result.returncode != 0:
        detail = result.stderr.decode("utf-8", "replace").strip() or "git command failed"
        raise ReadmitFailure("git-failure", detail)
    return result


def _safe_rel_path(value: Any) -> str:
    if not isinstance(value, str):
        raise ReadmitFailure("unsafe-path", f"unsafe path `{value}`")
    raw = value.strip().replace("\\", "/")
    path = Path(raw)
    if not raw or path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ReadmitFailure("unsafe-path", f"unsafe path `{value}`")
    if raw.startswith(".builder/") or raw == ".builder":
        raise ReadmitFailure("unsafe-path", f"mutable control path is outside source scope: `{value}`")
    return raw


def _load_yaml(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ReadmitFailure("unsafe-artifact", f"canonical file is missing or a symlink: {path}")
    data, errors = parse_yaml_like_file(path)
    if errors:
        raise ReadmitFailure("invalid-yaml", "; ".join(errors))
    if not isinstance(data, dict):
        raise ReadmitFailure("invalid-yaml", f"{path.name} is not a mapping")
    return data


def _bundle_rows(spec_dir: Path) -> list[tuple[Path, dict[str, Any]]]:
    evidence_dir = spec_dir / "gate-evidence"
    try:
        if (
            evidence_dir.is_symlink()
            or not evidence_dir.is_dir()
            or evidence_dir.resolve().parent != spec_dir.resolve()
        ):
            raise ReadmitFailure(
                "unsafe-evidence-directory",
                f"gate-evidence must be a canonical non-symlink directory under the target spec: {evidence_dir}",
            )
    except OSError as exc:
        raise ReadmitFailure("unsafe-evidence-directory", f"gate-evidence cannot be resolved: {evidence_dir}") from exc
    violations = gate_evidence.verify_chain(spec_dir)
    if violations:
        raise ReadmitFailure("broken-evidence-chain", "; ".join(violations))
    rows: list[tuple[Path, dict[str, Any]]] = []
    for path in sorted(evidence_dir.glob("*.yaml")):
        data = _load_yaml(path)
        if data.get("schema") != gate_evidence.SCHEMA:
            raise ReadmitFailure("invalid-bundle-schema", f"{path.name}: invalid gate-evidence schema")
        if data.get("spec_id") != spec_dir.name:
            raise ReadmitFailure("cross-spec-evidence", f"{path.name}: bundle belongs to another spec")
        rows.append((path, data))
    return rows


def _valid_git_commit(root: Path, value: Any, *, field: str) -> str:
    sha = str(value or "").strip()
    if not _GIT_SHA.fullmatch(sha):
        raise ReadmitFailure("missing-immutable-baseline", f"{field} is not a full Git commit identity")
    result = _run_git(root, "cat-file", "-e", f"{sha}^{{commit}}", check=False)
    if result.returncode != 0:
        raise ReadmitFailure("missing-immutable-baseline", f"{field} commit is unavailable: {sha}")
    return sha


def _finished_at(value: Any) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("timezone required")
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError) as exc:
        raise ReadmitFailure("invalid-finished-at", "verify bundle finished_at is not an ISO-8601 instant") from exc


def _consumed(spec_dir: Path) -> bool:
    report_path = spec_dir / "sync-readmission-report.yaml"
    if not report_path.exists():
        return False
    report = _load_yaml(report_path)
    return bool(report.get("status") == "ready" and report.get("provenance") == "bootstrap-exception")


def _dirty_summary(bundle: dict[str, Any]) -> dict[str, Any]:
    summary = bundle.get("diff_stat")
    if not isinstance(summary, dict) or not isinstance(summary.get("files"), list) or not summary["files"]:
        legacy = str(bundle.get("dirty_tree_summary") or "").strip()
        if legacy:
            return {"summary": legacy, "files": []}
        raise ReadmitFailure(
            "bootstrap-missing-corroboration",
            "bootstrap exception requires a host-recorded dirty-tree summary with paths",
        )
    files: list[str] = []
    for item in summary["files"]:
        raw = str(item or "").strip().replace("\\", "/")
        if raw and raw not in files:
            files.append(raw)
    if not files:
        raise ReadmitFailure("bootstrap-missing-corroboration", "host dirty-tree summary has no paths")
    return {
        "files_changed": summary.get("files_changed"),
        "insertions": summary.get("insertions"),
        "deletions": summary.get("deletions"),
        "files": files,
    }


def _derive_lineage(root: Path, spec_dir: Path) -> dict[str, Any]:
    if _consumed(spec_dir):
        raise ReadmitFailure("bootstrap-exception-expired", "bootstrap exception already consumed for this target")
    rows = _bundle_rows(spec_dir)
    baselines = [
        (path, row) for path, row in rows
        if row.get("gate") == "red_baseline" and row.get("verdict") == "pass" and row.get("git_head_sha")
    ]
    verifies = [
        (path, row) for path, row in rows
        if row.get("phase") == "verify" and row.get("gate") == "host_verify" and row.get("verdict") == "pass"
    ]
    if len(baselines) != 1 or len(verifies) != 1:
        raise ReadmitFailure(
            "ambiguous-lineage",
            f"expected one passing baseline and one passing verify bundle; found {len(baselines)} and {len(verifies)}",
        )
    baseline_path, baseline_bundle = baselines[0]
    verify_path, verify_bundle = verifies[0]
    if int(baseline_bundle.get("seq") or 0) >= int(verify_bundle.get("seq") or 0):
        raise ReadmitFailure("non-contiguous-lineage", "baseline bundle does not precede verify bundle")
    baseline = _valid_git_commit(root, baseline_bundle.get("git_head_sha"), field="baseline git_head_sha")
    verify_sha = str(verify_bundle.get("bundle_sha256") or "").strip()
    baseline_sha = str(baseline_bundle.get("bundle_sha256") or "").strip()
    if not _SHA256.fullmatch(verify_sha) or not _SHA256.fullmatch(baseline_sha):
        raise ReadmitFailure("invalid-bundle-digest", "lineage bundle SHA-256 is missing or malformed")

    snapshot = verify_bundle.get("verified_snapshot")
    if isinstance(snapshot, dict):
        immutable_head = _valid_git_commit(root, verify_bundle.get("git_head_sha"), field="verify git_head_sha")
        manifest = snapshot.get("path_manifest")
        if not isinstance(manifest, list):
            raise ReadmitFailure("incomplete-path-manifest", "immutable snapshot lacks path_manifest")
        changed_paths = [_safe_rel_path(row.get("path")) for row in manifest if isinstance(row, dict)]
        if len(changed_paths) != len(manifest) or changed_paths != sorted(set(changed_paths)):
            raise ReadmitFailure("incomplete-path-manifest", "snapshot path_manifest must be complete, sorted, and unique")
        for row in manifest:
            if not _SHA256.fullmatch(str(row.get("sha256") or "")):
                raise ReadmitFailure("incomplete-path-manifest", "snapshot manifest entry lacks a SHA-256")
        verified_tree = str(snapshot.get("verified_tree") or "").strip()
        snapshot_baseline = str(snapshot.get("implementation_baseline") or "").strip()
        if snapshot_baseline != baseline or not _SHA256.fullmatch(verified_tree):
            raise ReadmitFailure("missing-immutable-identity", "verified_snapshot identity does not bind the baseline")
        provenance = "immutable"
        summary: dict[str, Any] | None = None
        historical_manifest = {str(row["path"]): str(row["sha256"]) for row in manifest}
        derived_baseline = baseline
    else:
        if _finished_at(verify_bundle.get("finished_at")) > BOOTSTRAP_CUTOFF:
            raise ReadmitFailure("missing-immutable-identity", "verify bundle is newer than the bootstrap cutoff")
        derived_baseline = _valid_git_commit(root, verify_bundle.get("git_head_sha"), field="verify git_head_sha")
        summary = _dirty_summary(verify_bundle)
        changed_paths = []
        verified_tree = ""
        provenance = "bootstrap-exception"
        historical_manifest = {}

    return {
        "provenance": provenance,
        "implementation_baseline": derived_baseline,
        "historical_verified_head": immutable_head if isinstance(snapshot, dict) else str(verify_bundle.get("git_head_sha") or "").strip(),
        "historical_verified_tree": verified_tree,
        "historical_changed_paths": changed_paths,
        "historical_manifest": historical_manifest,
        "dirty_tree_summary": summary,
        "baseline_bundle": f"gate-evidence/{baseline_path.name}",
        "baseline_sha256": baseline_sha,
        "verified_bundle": f"gate-evidence/{verify_path.name}",
        "verified_sha256": verify_sha,
        "verify_commands": _verify_commands(verify_bundle),
    }


def _verify_commands(bundle: dict[str, Any]) -> list[str]:
    commands = [
        str(row.get("command") or "").strip()
        for row in (bundle.get("commands") or [])
        if isinstance(row, dict) and str(row.get("command") or "").strip()
    ]
    if not commands:
        command = bundle.get("command")
        if isinstance(command, list) and len(command) >= 3 and command[-2] == "-c":
            commands = [str(command[-1]).strip()]
        elif isinstance(command, str) and command.strip():
            commands = [command.strip()]
    if not commands:
        raise ReadmitFailure("missing-verify-commands", "passing historical verify bundle has no replayable command")
    return list(dict.fromkeys(commands))


def _candidate_root(root: Path, spec_id: str) -> Path:
    target = root / ".builder" / "worktrees" / spec_id
    if (
        not target.is_symlink()
        and target.is_dir()
        and target.resolve().parent == (root / ".builder" / "worktrees").resolve()
        and _run_git(target, "rev-parse", "--is-inside-work-tree", check=False).returncode == 0
    ):
        return target.resolve()
    if _run_git(root, "rev-parse", "--is-inside-work-tree", check=False).returncode != 0:
        raise ReadmitFailure("git-repository-required", f"readmission root is not a Git worktree: {root}")
    return root


def _historical_path_allows(path: str, raw_entries: list[str]) -> bool:
    for raw in raw_entries:
        entry = raw.strip().replace("\\", "/")
        if entry.endswith("/") and path.startswith(entry):
            return True
        if path == entry:
            return True
    return False


def _candidate_paths(candidate: Path, lineage: dict[str, Any]) -> list[str]:
    baseline = lineage["implementation_baseline"]
    if lineage["provenance"] == "immutable":
        paths = list(lineage["historical_changed_paths"])
        for rel in paths:
            source = candidate / rel
            actual = sha256_bytes(source.read_bytes()) if source.is_file() and not source.is_symlink() else sha256_bytes(b"<missing>")
            if actual != lineage["historical_manifest"][rel]:
                raise ReadmitFailure("snapshot-content-mismatch", f"candidate bytes do not match immutable manifest: {rel}")
        if verified_tree_digest(candidate, paths, lineage["historical_verified_head"]) != lineage["historical_verified_tree"]:
            raise ReadmitFailure("snapshot-content-mismatch", "candidate tree does not match immutable verified identity")
    else:
        raw_entries = list((lineage.get("dirty_tree_summary") or {}).get("files") or [])
        if candidate == candidate.resolve() and (candidate / ".git").is_dir():
            # A control-root fallback may contain other specs' changes. The host summary is the
            # only lawful historical selector; fresh verify creates the new exact manifest.
            paths = [entry for entry in raw_entries if not entry.endswith("/") and not entry.startswith(".builder/")]
        else:
            paths = []
        diff = _run_git(candidate, "diff", "--name-only", "-z", baseline, "--").stdout
        observed = [item.decode("utf-8", "surrogateescape") for item in diff.split(b"\0") if item]
        untracked = _run_git(candidate, "ls-files", "--others", "--exclude-standard", "-z").stdout
        observed += [item.decode("utf-8", "surrogateescape") for item in untracked.split(b"\0") if item]
        observed = [item for item in observed if not item.startswith(".builder/")]
        # The bootstrap summary may be weak, but it is still the only authorized historical
        # scope selector. Candidate-only paths absent from that host record are never admitted.
        paths = [item for item in observed if _historical_path_allows(item, raw_entries)]
    normalized = sorted({_safe_rel_path(item) for item in paths})
    if not normalized:
        raise ReadmitFailure("empty-candidate", "proved lineage has no replayable source paths")
    return normalized


def _copy_candidate(candidate: Path, worktree: Path, paths: list[str]) -> None:
    for rel in paths:
        source = candidate / rel
        destination = worktree / rel
        if source.is_symlink():
            raise ReadmitFailure("unsafe-candidate", f"candidate path is a symlink: {rel}")
        if source.is_file():
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
        elif not source.exists():
            if destination.is_dir():
                shutil.rmtree(destination)
            else:
                destination.unlink(missing_ok=True)
        else:
            raise ReadmitFailure("unsafe-candidate", f"candidate path is not a regular file: {rel}")


def _fresh_bundle_bytes(
    spec_dir: Path, spec_id: str, transaction_id: str, baseline: str, head: str,
    tree: str, paths: list[str], commands: list[str], results: list[Any],
) -> tuple[Path, dict[str, Any], bytes]:
    seq = gate_evidence.next_seq(spec_dir / "gate-evidence")
    gate_id = f"{spec_id}:verify:host_verify:{seq:04d}"
    manifest = []
    # `tree` binds bytes as a set; per-path digests make the complete manifest independently inspectable.
    for rel in paths:
        manifest.append({"path": rel, "sha256": ""})
    body: dict[str, Any] = {
        "schema": gate_evidence.SCHEMA,
        "gate_id": gate_id,
        "seq": seq,
        "gate": "host_verify",
        "polarity": "green",
        "spec_id": spec_id,
        "phase": "verify",
        "mode": "enforce",
        "verdict": "pass",
        "blocking": False,
        "git_head_sha": head,
        "transaction_id": transaction_id,
        "verified_snapshot": {
            "implementation_baseline": baseline,
            "verified_tree": tree,
            "changed_paths_digest": changed_paths_digest(paths),
            "path_manifest": manifest,
        },
        "changed_paths": paths,
        "commands": [
            {
                "command": command,
                "exit_code": result.exit_code,
                "timed_out": result.timed_out,
                "spawn_error": result.spawn_error,
                "started_at": result.started_at,
                "finished_at": result.finished_at,
            }
            for command, result in zip(commands, results)
        ],
        "finished_at": max((result.finished_at for result in results), default=""),
        "prev_bundle_sha256": gate_evidence.resolve_prev_sha(spec_dir / "gate-evidence"),
        "bundle_sha256": "",
    }
    body["bundle_sha256"] = gate_evidence.bundle_sha(body)
    destination = spec_dir / "gate-evidence" / f"{seq:04d}-host_verify-verify.yaml"
    return destination, body, _dump_yaml_bytes(body)


def _validate_staged(
    baseline: dict[str, Any], scope: dict[str, Any], report: dict[str, Any], bundle: dict[str, Any],
) -> None:
    schema_errors: list[str] = []
    for payload, schema_name, source_name in (
        (baseline, "implementation-baseline.schema.yaml", "implementation-baseline.yaml"),
        (scope, "sync-scope.schema.yaml", "sync-scope.yaml"),
        (report, "sync-readmission-report.schema.yaml", "sync-readmission-report.yaml"),
    ):
        schema, load_errors = load_schema(schema_name)
        schema_errors.extend(load_errors)
        schema_errors.extend(validate_schema(payload, schema, source_name))
    if schema_errors:
        raise ReadmitFailure("invalid-staged-artifact", "; ".join(schema_errors))
    bundle_errors = validate_readmission_verify_bundle(bundle)
    if bundle_errors:
        raise ReadmitFailure("invalid-fresh-bundle", "; ".join(bundle_errors))
    transaction_id = report.get("transaction_id")
    if not transaction_id or any(row.get("transaction_id") != transaction_id for row in (baseline, scope, bundle)):
        raise ReadmitFailure("mixed-transaction", "staged readmission set has mixed transaction identities")
    bundle_sha = gate_evidence.bundle_sha(bundle)
    if bundle.get("bundle_sha256") != bundle_sha or report.get("verify_gate_sha256") != bundle_sha:
        raise ReadmitFailure("invalid-fresh-bundle", "staged report does not bind the fresh verify bundle")
    if (
        scope.get("verify_gate_sha256") != report.get("verify_gate_sha256")
        or scope.get("verify_gate_id") != bundle.get("gate_id")
        or bundle.get("schema") != gate_evidence.SCHEMA
        or bundle.get("spec_id") != report.get("spec")
        or bundle.get("gate") != "host_verify"
        or bundle.get("phase") != "verify"
        or bundle.get("verdict") != "pass"
    ):
        raise ReadmitFailure("mixed-transaction", "staged scope and report bind different verify bundles")
    paths = scope.get("changed_paths")
    snapshot = bundle.get("verified_snapshot")
    manifest = snapshot.get("path_manifest") if isinstance(snapshot, dict) else None
    manifest_paths = [row.get("path") for row in manifest] if isinstance(manifest, list) else None
    if (
        baseline.get("spec") != scope.get("spec")
        or scope.get("spec") != report.get("spec")
        or baseline.get("implementation_baseline") != scope.get("implementation_baseline")
        or scope.get("implementation_baseline") != report.get("implementation_baseline")
        or baseline.get("baseline_paths") != paths
        or report.get("changed_paths") != paths
        or baseline.get("baseline_paths_digest") != changed_paths_digest(paths or [])
        or scope.get("changed_paths_digest") != changed_paths_digest(paths or [])
        or not isinstance(snapshot, dict)
        or snapshot.get("implementation_baseline") != scope.get("implementation_baseline")
        or snapshot.get("verified_tree") != scope.get("verified_tree")
        or snapshot.get("changed_paths_digest") != scope.get("changed_paths_digest")
        or manifest_paths != paths
        or any(not isinstance(row, dict) or not _SHA256.fullmatch(str(row.get("sha256") or "")) for row in (manifest or []))
    ):
        raise ReadmitFailure("invalid-staged-set", "staged baseline, scope, report, and snapshot bindings differ")
    if report.get("provenance") == "bootstrap-exception":
        if scope.get("derived_baseline") != report.get("derived_baseline"):
            raise ReadmitFailure("altered-baseline", "bootstrap derived baseline differs across staged artifacts")
        for row in (scope, report):
            if row.get("owner_authorization") != BOOTSTRAP_AUTHORIZATION:
                raise ReadmitFailure("altered-authorization", "bootstrap authorization is not byte-identical")


def _archive_preimages(spec_dir: Path, transaction_id: str, destinations: list[Path]) -> None:
    existing_report = spec_dir / "sync-readmission-report.yaml"
    if not existing_report.is_file() or existing_report.is_symlink():
        return
    old, errors = parse_yaml_like_file(existing_report)
    old_tx = str(old.get("transaction_id") or "unknown") if not errors else "unknown"
    history = spec_dir / "readmission-history"
    if history.is_symlink():
        raise ReadmitFailure("unsafe-history-directory", f"readmission history is a symlink: {history}")
    archive = history / f"{old_tx}-{transaction_id[:12]}"
    archive.mkdir(parents=True, exist_ok=False)
    for path in destinations:
        if path.is_file() and not path.is_symlink():
            target = archive / path.relative_to(spec_dir)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)


def _readmit_spec(root: Path, spec_id: str) -> tuple[int, dict[str, Any]]:
    root = root.resolve()
    if not _SPEC_ID.fullmatch(str(spec_id or "")) or spec_id in {".", ".."}:
        raise ReadmitFailure("invalid-spec", f"invalid explicit spec id `{spec_id}`")
    specs_root = root / ".builder" / "specs"
    spec_dir = specs_root / spec_id
    if not spec_dir.is_dir():
        raise ReadmitFailure("missing-spec", f"spec path not found: {spec_dir}")
    try:
        if spec_dir.is_symlink() or spec_dir.resolve().parent != specs_root.resolve():
            raise ReadmitFailure(
                "unsafe-spec-path", f"spec path must be a canonical non-symlink child of {specs_root}: {spec_dir}",
            )
    except OSError as exc:
        raise ReadmitFailure("unsafe-spec-path", f"spec path cannot be resolved: {spec_dir}") from exc
    spec = _load_yaml(spec_dir / "spec.yaml")
    if str(spec.get("status", "")).strip() == "synced":
        raise ReadmitFailure("already-synced", f"spec `{spec_id}` has lifecycle status synced")

    try:
        lock_context = spec_mutation_lock(root, spec_id, blocking=False, owner="sync-readmit")
        with lock_context:
            lineage = _derive_lineage(root, spec_dir)
            lineage["spec_id"] = spec_id
            candidate = _candidate_root(root, spec_id)
            paths = _candidate_paths(candidate, lineage)
            if lineage["provenance"] == "bootstrap-exception":
                # The exception identifies the historical source baseline but does not turn
                # those unverifiable dirty bytes into current sync evidence. Rebind the proved
                # target path set to the current control-tree snapshot and verify that snapshot.
                canonical_baseline = _valid_git_commit(
                    root, _run_git(root, "rev-parse", "HEAD").stdout.decode().strip(), field="current git_head_sha",
                )
                replay_source = root
            else:
                canonical_baseline = lineage["implementation_baseline"]
                replay_source = candidate
            temp_parent = root / ".builder" / "readmission-worktrees"
            temp_parent.mkdir(parents=True, exist_ok=True)
            temp_root = Path(tempfile.mkdtemp(prefix=f"{spec_id}-", dir=str(temp_parent)))
            worktree = temp_root / "worktree"
            added = False
            try:
                added = True
                _run_git(root, "worktree", "add", "--detach", str(worktree), canonical_baseline)
                _copy_candidate(replay_source, worktree, paths)
                if lineage["provenance"] == "immutable":
                    actual = _run_git(worktree, "diff", "--name-only", "-z", canonical_baseline, "--").stdout
                    actual_paths = sorted(
                        _safe_rel_path(item.decode("utf-8", "surrogateescape"))
                        for item in actual.split(b"\0") if item and not item.startswith(b".builder/")
                    )
                    untracked = _run_git(worktree, "ls-files", "--others", "--exclude-standard", "-z").stdout
                    actual_paths += [
                        _safe_rel_path(item.decode("utf-8", "surrogateescape"))
                        for item in untracked.split(b"\0") if item and not item.startswith(b".builder/")
                    ]
                    if sorted(set(actual_paths)) != paths:
                        raise ReadmitFailure("replay-scope-mismatch", "fresh replay changed-path manifest differs from candidate")

                from _dispatch_runtime.lane_common import _run_verify_commands_detailed

                results = _run_verify_commands_detailed(lineage["verify_commands"], str(worktree), capture=True)
                failures = [result.command for result in results if not result.ok]
                if failures:
                    detail = "\n".join(
                        (result.stderr_tail or result.stdout_tail or result.spawn_error or "no output")[-4000:]
                        for result in results if not result.ok
                    )
                    raise ReadmitFailure("fresh-verify-failed", f"fresh isolated verify failed: {failures}; {detail}")
                head = _run_git(worktree, "rev-parse", "HEAD").stdout.decode().strip()
                tree = verified_tree_digest(worktree, paths, head)
                if verified_tree_digest(root, paths, head) != tree:
                    raise ReadmitFailure("stale-control-tree", "fresh verified bytes do not match the sync target root")
                transaction_id = sha256_bytes(
                    f"{spec_id}\0{canonical_baseline}\0{lineage['implementation_baseline']}\0{lineage['verified_sha256']}\0{token_hex(16)}".encode()
                )
                bundle_path, fresh_bundle, bundle_bytes = _fresh_bundle_bytes(
                    spec_dir, spec_id, transaction_id, canonical_baseline, head,
                    tree, paths, lineage["verify_commands"], results,
                )
                for item in fresh_bundle["verified_snapshot"]["path_manifest"]:
                    source = worktree / item["path"]
                    item["sha256"] = sha256_bytes(source.read_bytes()) if source.is_file() else sha256_bytes(b"<missing>")
                fresh_bundle["bundle_sha256"] = gate_evidence.bundle_sha(fresh_bundle)
                bundle_bytes = _dump_yaml_bytes(fresh_bundle)
                bundle_ref = f"gate-evidence/{bundle_path.name}"
                delta_path = spec_dir / "ssot-delta.yaml"
                if delta_path.is_symlink() or not delta_path.is_file():
                    raise ReadmitFailure("missing-delta", f"missing or unsafe canonical delta: {delta_path}")
                roots = {"worktree_root": str(root), "control_root": str(root / ".builder"), "worktree_isolated": True}
                baseline = {
                    "schema": "implementation-baseline/v1", "spec": spec_id,
                    "implementation_baseline": canonical_baseline,
                    "baseline_paths": paths, "baseline_paths_digest": changed_paths_digest(paths),
                    **roots, "transaction_id": transaction_id,
                }
                scope: dict[str, Any] = {
                    "schema": "sync-scope/v1", "spec": spec_id, "admission": "readmission",
                    "implementation_baseline": canonical_baseline,
                    "verified_head": head, "verified_tree": tree, "changed_paths": paths,
                    "changed_paths_digest": changed_paths_digest(paths),
                    "declared_delta_digest": sha256_bytes(delta_path.read_bytes()),
                    "verify_gate_id": fresh_bundle["gate_id"], "verify_gate_bundle": bundle_ref,
                    "verify_gate_sha256": fresh_bundle["bundle_sha256"], **roots,
                    "transaction_id": transaction_id,
                }
                report: dict[str, Any] = {
                    "schema": REPORT_SCHEMA, "spec": spec_id, "status": "ready", "result_code": "ok",
                    "transaction_id": transaction_id, "provenance": lineage["provenance"],
                    "implementation_baseline": canonical_baseline,
                    "verified_head": head, "verified_tree": tree, "changed_paths": paths,
                    "verify_gate_bundle": bundle_ref, "verify_gate_sha256": fresh_bundle["bundle_sha256"],
                    "source_evidence": {
                        "baseline_bundle": lineage["baseline_bundle"], "baseline_sha256": lineage["baseline_sha256"],
                        "verified_bundle": lineage["verified_bundle"], "verified_sha256": lineage["verified_sha256"],
                        "verified_head": lineage["historical_verified_head"],
                    },
                }
                if lineage["provenance"] == "bootstrap-exception":
                    scope.update(provenance="bootstrap-exception", owner_authorization=BOOTSTRAP_AUTHORIZATION,
                                 derived_baseline=lineage["implementation_baseline"])
                    report.update(owner_authorization=BOOTSTRAP_AUTHORIZATION,
                                  derived_baseline=lineage["implementation_baseline"])
                    report["source_evidence"]["dirty_tree_summary"] = lineage["dirty_tree_summary"]
                _validate_staged(baseline, scope, report, fresh_bundle)
                destinations = [
                    spec_dir / "implementation-baseline.yaml", spec_dir / "sync-scope.yaml",
                    bundle_path, spec_dir / "sync-readmission-report.yaml",
                ]
                _archive_preimages(spec_dir, transaction_id, destinations)
                # Insertion order is the publication protocol: the canonical report is the commit marker.
                atomic_publish({
                    destinations[0]: _dump_yaml_bytes(baseline), destinations[1]: _dump_yaml_bytes(scope),
                    destinations[2]: bundle_bytes, destinations[3]: _dump_yaml_bytes(report),
                })
                return 0, report
            finally:
                if added:
                    _run_git(root, "worktree", "remove", "--force", str(worktree), check=False)
                try:
                    temp_root.rmdir()
                    temp_parent.rmdir()
                except OSError:
                    pass
    except SpecMutationBusy as exc:
        raise ReadmitFailure("mutation-contention", str(exc)) from exc


def _record_insufficiency(root: Path, spec_id: str, failure: ReadmitFailure) -> None:
    """Persist a typed failure only when doing so cannot replace prior canonical evidence."""
    if not _SPEC_ID.fullmatch(str(spec_id or "")) or failure.code in {
        "invalid-spec", "missing-spec", "already-synced", "mutation-contention", "unsafe-spec-path",
    }:
        return
    specs_root = root.resolve() / ".builder" / "specs"
    spec_dir = specs_root / spec_id
    report_path = spec_dir / "sync-readmission-report.yaml"
    try:
        if (
            spec_dir.is_symlink()
            or not spec_dir.is_dir()
            or spec_dir.resolve().parent != specs_root.resolve()
            or report_path.exists()
            or report_path.is_symlink()
        ):
            return
        with spec_mutation_lock(root, spec_id, blocking=False, owner="sync-readmit-report"):
            if report_path.exists() or report_path.is_symlink():
                return
            payload = {
                "schema": REPORT_SCHEMA,
                "spec": spec_id,
                "status": "blocked",
                "result_code": failure.code,
                "detail": failure.detail,
            }
            schema, schema_errors = load_schema("sync-readmission-report.schema.yaml")
            errors = schema_errors + validate_schema(payload, schema, "sync-readmission-report.yaml")
            if not errors:
                atomic_publish({report_path: _dump_yaml_bytes(payload)})
    except (OSError, SpecMutationBusy):
        return


def readmit_spec(root: Path, spec_id: str) -> tuple[int, dict[str, Any]]:
    try:
        return _readmit_spec(root, spec_id)
    except ReadmitFailure as exc:
        _record_insufficiency(root, spec_id, exc)
        raise


def _dump_yaml_bytes(payload: dict[str, Any]) -> bytes:
    from _yaml import yaml

    return yaml.safe_dump(payload, sort_keys=False, allow_unicode=True).encode("utf-8")
