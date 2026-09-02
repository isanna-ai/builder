from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from _dispatch_runtime import gate_evidence
from _validators.common import load_schema, parse_yaml_like_file, validate_schema

SCHEMA = "sync-scope/v1"

_READMISSION_BUNDLE_FIELDS = {
    "schema", "gate_id", "seq", "gate", "polarity", "spec_id", "phase", "mode", "verdict",
    "blocking", "git_head_sha", "transaction_id", "verified_snapshot", "changed_paths", "commands",
    "finished_at", "prev_bundle_sha256", "bundle_sha256",
}
_READMISSION_COMMAND_FIELDS = {
    "command", "exit_code", "timed_out", "spawn_error", "started_at", "finished_at",
}
_READMISSION_SNAPSHOT_FIELDS = {
    "implementation_baseline", "verified_tree", "changed_paths_digest", "path_manifest",
}


def validate_readmission_verify_bundle(data: dict[str, Any]) -> list[str]:
    """Validate the strict readmission-specific gate-evidence enrichment."""
    errors: list[str] = []
    missing = sorted(_READMISSION_BUNDLE_FIELDS.difference(data))
    unknown = sorted(set(data).difference(_READMISSION_BUNDLE_FIELDS))
    if missing:
        errors.append(f"fresh verify bundle: missing fields {missing}")
    if unknown:
        errors.append(f"fresh verify bundle: unknown fields {unknown}")
    commands = data.get("commands")
    if not isinstance(commands, list) or not commands:
        errors.append("fresh verify bundle: commands must be a non-empty list")
    else:
        for index, row in enumerate(commands):
            if not isinstance(row, dict) or set(row) != _READMISSION_COMMAND_FIELDS:
                errors.append(f"fresh verify bundle: commands[{index}] has a non-strict shape")
    snapshot = data.get("verified_snapshot")
    if not isinstance(snapshot, dict) or set(snapshot) != _READMISSION_SNAPSHOT_FIELDS:
        errors.append("fresh verify bundle: verified_snapshot has a non-strict shape")
        return errors
    manifest = snapshot.get("path_manifest")
    if not isinstance(manifest, list) or any(
        not isinstance(row, dict) or set(row) != {"path", "sha256"} for row in manifest
    ):
        errors.append("fresh verify bundle: path_manifest has a non-strict shape")
    return errors


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def changed_paths_digest(paths: list[str]) -> str:
    normalized = sorted(dict.fromkeys(paths))
    return sha256_bytes(("\n".join(normalized) + "\n").encode("utf-8"))


def sync_result_payload_digest(payload: dict[str, Any]) -> str:
    """Digest the hook payload before the host adds its sync-gate binding."""
    clean = dict(payload)
    for key in ("sync_gate_id", "sync_gate_bundle", "sync_gate_sha256"):
        clean.pop(key, None)
    encoded = json.dumps(clean, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return sha256_bytes(encoded)


def verified_tree_digest(root: Path, paths: list[str], head: str) -> str:
    digest = hashlib.sha256()
    digest.update(f"HEAD\0{head}\0".encode("utf-8"))
    for rel in sorted(dict.fromkeys(paths)):
        path = root / rel
        digest.update(rel.encode("utf-8") + b"\0")
        if path.is_file() and not path.is_symlink():
            digest.update(path.read_bytes())
        else:
            digest.update(b"<missing>")
        digest.update(b"\0")
    return digest.hexdigest()


def _resolved_text_path(value: Any) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return str(Path(raw).resolve())
    except OSError:
        return None


def repair_legacy_sync_transaction(root: Path, spec_dir: Path, path: Path | None = None) -> bool:
    """Backfill the verify->sync transaction binding for legacy non-readmission scopes.

    This is intentionally narrow: only ordinary sync scope/baseline artifacts that are
    otherwise canonical and simply predate the finalized `transaction_id` contract are
    rewritten. Readmission sets, conflicting identities, or structurally ambiguous files
    remain fail-closed.
    """
    scope_path = spec_dir / "sync-scope.yaml"
    source = path or scope_path
    baseline_path = spec_dir / "implementation-baseline.yaml"
    try:
        if source.is_symlink() or source.resolve() != scope_path.resolve():
            return False
        if baseline_path.is_symlink() or not baseline_path.is_file():
            return False
    except OSError:
        return False
    scope, scope_errors = parse_yaml_like_file(scope_path)
    baseline, baseline_errors = parse_yaml_like_file(baseline_path)
    if scope_errors or baseline_errors or not isinstance(scope, dict) or not isinstance(baseline, dict):
        return False
    if scope.get("admission") == "readmission":
        return False
    if scope.get("schema") != SCHEMA or baseline.get("schema") != "implementation-baseline/v1":
        return False
    if scope.get("spec") != spec_dir.name or baseline.get("spec") != spec_dir.name:
        return False

    scope_tx = str(scope.get("transaction_id") or "").strip()
    baseline_tx = str(baseline.get("transaction_id") or "").strip()
    if scope_tx and baseline_tx:
        return False
    if scope_tx and baseline_tx and scope_tx != baseline_tx:
        return False

    baseline_head = str(baseline.get("implementation_baseline") or "").strip()
    scope_head = str(scope.get("implementation_baseline") or "").strip()
    if not baseline_head or (scope_head and scope_head != baseline_head):
        return False

    worktree_root = _resolved_text_path(scope.get("worktree_root")) or _resolved_text_path(baseline.get("worktree_root"))
    control_root = _resolved_text_path(scope.get("control_root")) or _resolved_text_path(baseline.get("control_root"))
    baseline_worktree = _resolved_text_path(baseline.get("worktree_root"))
    scope_worktree = _resolved_text_path(scope.get("worktree_root"))
    baseline_control = _resolved_text_path(baseline.get("control_root"))
    scope_control = _resolved_text_path(scope.get("control_root"))
    if (
        not worktree_root
        or not control_root
        or worktree_root != str(root.resolve())
        or (baseline_worktree and baseline_worktree != worktree_root)
        or (scope_worktree and scope_worktree != worktree_root)
        or (baseline_control and baseline_control != control_root)
        or (scope_control and scope_control != control_root)
    ):
        return False

    transaction_id = scope_tx or baseline_tx or sha256_bytes(
        f"{spec_dir.name}\0{baseline_head}\0{worktree_root}\0{control_root}".encode("utf-8")
    )
    changed = False
    if not baseline_tx:
        baseline = dict(baseline)
        baseline["transaction_id"] = transaction_id
        changed = True
    if not scope_tx:
        scope = dict(scope)
        scope["transaction_id"] = transaction_id
        changed = True
    if changed:
        atomic_write_yaml(baseline_path, baseline)
        atomic_write_yaml(scope_path, scope)
    return changed


def _safe_relative_path(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    raw = value.strip().replace("\\", "/")
    path = Path(raw)
    if not raw or path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        return None
    return raw


def atomic_write_yaml(path: Path, payload: dict[str, Any]) -> None:
    from _yaml import yaml

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(yaml.safe_dump(payload, sort_keys=False, allow_unicode=True))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def validate_scope_evidence(
    root: Path, spec_dir: Path, path: Path | None = None, *, verify_snapshot: bool = True,
) -> tuple[dict[str, Any] | None, list[str]]:
    source = path or (spec_dir / "sync-scope.yaml")
    errors: list[str] = []
    try:
        if source.is_symlink() or source.resolve() != (spec_dir / "sync-scope.yaml").resolve():
            return None, ["sync-scope.yaml: evidence must use the canonical non-symlink spec-local path"]
    except OSError:
        return None, ["sync-scope.yaml: evidence path cannot be resolved"]
    data, parse_errors = parse_yaml_like_file(source)
    if parse_errors:
        return None, parse_errors
    if data.get("schema") != SCHEMA:
        errors.append(f"sync-scope.yaml: schema must be {SCHEMA}")
    if data.get("spec") != spec_dir.name:
        errors.append("sync-scope.yaml: spec does not match the owning spec directory")
    disclosure_fields = ("provenance", "owner_authorization", "derived_baseline")
    if any(key in data for key in disclosure_fields) and data.get("admission") != "readmission":
        errors.append("sync-scope.yaml: bootstrap disclosure requires a committed readmission set")
    if data.get("admission") == "readmission":
        report_path = spec_dir / "sync-readmission-report.yaml"
        try:
            if report_path.is_symlink() or not report_path.is_file():
                errors.append("sync-scope.yaml: readmission commit marker is missing or unsafe")
            else:
                report, report_errors = parse_yaml_like_file(report_path)
                schema, schema_errors = load_schema("sync-readmission-report.schema.yaml")
                errors.extend(report_errors + schema_errors)
                errors.extend(validate_schema(report, schema, "sync-readmission-report.yaml"))
                from _validators.common import ValidationContext
                from _validators.sync_artifacts import run_sync_readmission_report

                report_validation = run_sync_readmission_report(ValidationContext(spec_dir=spec_dir))
                errors.extend(report_validation.errors)
                pairs = (
                    ("spec", "spec"), ("transaction_id", "transaction_id"),
                    ("implementation_baseline", "implementation_baseline"),
                    ("verified_head", "verified_head"), ("verified_tree", "verified_tree"),
                    ("changed_paths", "changed_paths"), ("verify_gate_bundle", "verify_gate_bundle"),
                    ("verify_gate_sha256", "verify_gate_sha256"),
                )
                if report.get("status") != "ready" or any(data.get(a) != report.get(b) for a, b in pairs):
                    errors.append("sync-scope.yaml: readmission set is missing its matching committed report")
                if report.get("provenance") == "bootstrap-exception":
                    from _sync.readmit import BOOTSTRAP_AUTHORIZATION

                    if data.get("provenance") != "bootstrap-exception":
                        errors.append("sync-scope.yaml: bootstrap provenance does not match committed report")
                    if data.get("owner_authorization") != BOOTSTRAP_AUTHORIZATION:
                        errors.append("sync-scope.yaml: bootstrap authorization is not the exact owner decision")
                    if data.get("owner_authorization") != report.get("owner_authorization"):
                        errors.append("sync-scope.yaml: bootstrap authorization does not match committed report")
                    if data.get("derived_baseline") != report.get("derived_baseline"):
                        errors.append("sync-scope.yaml: derived baseline does not match committed report")
                elif any(key in data for key in disclosure_fields):
                    errors.append("sync-scope.yaml: immutable readmission cannot introduce bootstrap disclosure")
        except OSError:
            errors.append("sync-scope.yaml: readmission commit marker cannot be read")
    if data.get("worktree_isolated") is not True:
        errors.append("sync-scope.yaml: worktree_isolated must be true")
    transaction_id = str(data.get("transaction_id", "")).strip()
    if not transaction_id:
        errors.append("sync-scope.yaml: transaction_id is required")
    worktree_root = str(data.get("worktree_root", "")).strip()
    control_root = str(data.get("control_root", "")).strip()
    if not worktree_root or not control_root or Path(worktree_root).resolve() == Path(control_root).resolve():
        errors.append("sync-scope.yaml: distinct worktree_root and control_root are required")
    if worktree_root and Path(worktree_root).resolve() != root.resolve():
        errors.append("sync-scope.yaml: worktree_root does not match the sync repository")

    raw_paths = data.get("changed_paths")
    paths = raw_paths if isinstance(raw_paths, list) else []
    if not isinstance(raw_paths, list):
        errors.append("sync-scope.yaml: changed_paths must be a list")
    normalized: list[str] = []
    for index, raw in enumerate(paths):
        rel = _safe_relative_path(raw)
        if rel is None or rel.startswith(".builder/"):
            errors.append(f"sync-scope.yaml: changed_paths[{index}] is not a safe source path")
        else:
            normalized.append(rel)
    if normalized != sorted(set(normalized)):
        errors.append("sync-scope.yaml: changed_paths must be sorted and unique")
    if data.get("changed_paths_digest") != changed_paths_digest(normalized):
        errors.append("sync-scope.yaml: changed_paths_digest does not match changed_paths")

    delta_path = spec_dir / "ssot-delta.yaml"
    if not delta_path.is_file() or data.get("declared_delta_digest") != sha256_bytes(delta_path.read_bytes()):
        errors.append("sync-scope.yaml: declared_delta_digest does not match ssot-delta.yaml")

    baseline = str(data.get("implementation_baseline", "")).strip()
    head = str(data.get("verified_head", "")).strip()
    tree = str(data.get("verified_tree", "")).strip()
    if not baseline or not head or not tree:
        errors.append("sync-scope.yaml: implementation_baseline, verified_head, and verified_tree are required")
    elif verify_snapshot and tree != verified_tree_digest(root, normalized, head):
        errors.append("sync-scope.yaml: verified_tree does not match the verified source snapshot")

    baseline_data, baseline_errors = parse_yaml_like_file(spec_dir / "implementation-baseline.yaml")
    if baseline_errors:
        errors.extend(baseline_errors)
    else:
        baseline_paths = baseline_data.get("baseline_paths")
        baseline_paths = baseline_paths if isinstance(baseline_paths, list) else []
        if baseline_data.get("schema") != "implementation-baseline/v1":
            errors.append("implementation-baseline.yaml: schema must be implementation-baseline/v1")
        if baseline_data.get("spec") != spec_dir.name:
            errors.append("implementation-baseline.yaml: spec does not match the owning spec directory")
        if baseline_data.get("implementation_baseline") != baseline:
            errors.append("sync-scope.yaml: implementation_baseline does not match implementation-baseline.yaml")
        if baseline_data.get("baseline_paths_digest") != changed_paths_digest(baseline_paths):
            errors.append("implementation-baseline.yaml: baseline_paths_digest does not match baseline_paths")
        if baseline_data.get("worktree_root") != worktree_root or baseline_data.get("control_root") != control_root:
            errors.append("sync-scope.yaml: worktree roots do not match implementation-baseline.yaml")
        if baseline_data.get("worktree_isolated") is not True:
            errors.append("implementation-baseline.yaml: worktree_isolated must be true")
        if baseline_data.get("transaction_id") != transaction_id:
            errors.append("sync-scope.yaml: transaction_id does not match implementation-baseline.yaml")

    bundle_ref = _safe_relative_path(data.get("verify_gate_bundle"))
    expected_prefix = "gate-evidence/"
    if bundle_ref is None or not bundle_ref.startswith(expected_prefix):
        errors.append("sync-scope.yaml: verify_gate_bundle must be inside gate-evidence/")
    else:
        bundle_path = spec_dir / bundle_ref
        try:
            if bundle_path.is_symlink() or not bundle_path.is_file() or bundle_path.resolve().parent != (spec_dir / "gate-evidence").resolve():
                errors.append("sync-scope.yaml: verify gate bundle is missing or unsafe")
            else:
                bundle, bundle_errors = parse_yaml_like_file(bundle_path)
                errors.extend(bundle_errors)
                if data.get("admission") == "readmission" and isinstance(bundle, dict):
                    errors.extend(validate_readmission_verify_bundle(bundle))
                stored = str(bundle.get("bundle_sha256", ""))
                if stored != gate_evidence.bundle_sha(bundle) or stored != data.get("verify_gate_sha256"):
                    errors.append("sync-scope.yaml: verify gate bundle digest is not corroborated")
                if bundle.get("gate_id") != data.get("verify_gate_id"):
                    errors.append("sync-scope.yaml: verify_gate_id does not match the gate bundle")
                if data.get("admission") == "readmission" and bundle.get("transaction_id") != transaction_id:
                    errors.append("sync-scope.yaml: verify gate transaction_id does not match")
                snapshot = bundle.get("verified_snapshot")
                if data.get("admission") == "readmission" and not isinstance(snapshot, dict):
                    errors.append("sync-scope.yaml: readmission verify bundle lacks an immutable fresh snapshot")
                elif isinstance(snapshot, dict):
                    if snapshot.get("implementation_baseline") != baseline:
                        errors.append("sync-scope.yaml: verify snapshot baseline does not match")
                    if snapshot.get("verified_tree") != tree:
                        errors.append("sync-scope.yaml: verify snapshot tree does not match")
                    if snapshot.get("changed_paths_digest") != data.get("changed_paths_digest"):
                        errors.append("sync-scope.yaml: verify snapshot path digest does not match")
                    manifest = snapshot.get("path_manifest")
                    manifest_paths = [row.get("path") for row in manifest] if isinstance(manifest, list) else None
                    if manifest_paths != normalized:
                        errors.append("sync-scope.yaml: verify snapshot manifest does not match changed_paths")
                if not (
                    bundle.get("schema") == gate_evidence.SCHEMA
                    and bundle.get("spec_id") == spec_dir.name
                    and bundle.get("phase") == "verify"
                    and bundle.get("gate") == "host_verify"
                    and bundle.get("verdict") == "pass"
                    and bundle.get("git_head_sha") == head
                ):
                    errors.append("sync-scope.yaml: gate bundle is not a matching passing host verify verdict")
        except OSError:
            errors.append("sync-scope.yaml: verify gate bundle cannot be read")
    chain_errors = gate_evidence.verify_chain(spec_dir)
    if chain_errors:
        errors.extend(f"sync-scope.yaml: gate-evidence chain: {item}" for item in chain_errors)
    return (data if not errors else None), errors


def result_is_corroborated(spec_dir: Path, result: dict[str, Any]) -> bool:
    root = Path(str(result.get("worktree_root", "")))
    scope, errors = validate_scope_evidence(root, spec_dir, verify_snapshot=False)
    if errors or scope is None:
        return False
    pairs = (
        ("verify_gate_id", "verify_gate_id"),
        ("verified_tree", "verified_tree"),
        ("changed_paths_digest", "changed_paths_digest"),
        ("declared_delta_digest", "declared_delta_digest"),
        ("verify_gate_sha256", "verify_gate_sha256"),
    )
    if result.get("spec") != spec_dir.name or not all(result.get(a) == scope.get(b) for a, b in pairs):
        return False
    if result.get("transaction_id") != scope.get("transaction_id"):
        return False
    if scope.get("provenance") == "bootstrap-exception":
        if result.get("provenance") != "bootstrap-exception":
            return False
        if result.get("owner_authorization") != scope.get("owner_authorization"):
            return False
        if result.get("derived_baseline") != scope.get("derived_baseline"):
            return False
    elif any(key in result for key in ("provenance", "owner_authorization", "derived_baseline")):
        return False

    bundle_ref = _safe_relative_path(result.get("sync_gate_bundle"))
    if bundle_ref is None or not bundle_ref.startswith("gate-evidence/"):
        return False
    bundle_path = spec_dir / bundle_ref
    try:
        if bundle_path.is_symlink() or not bundle_path.is_file() or bundle_path.resolve().parent != (spec_dir / "gate-evidence").resolve():
            return False
        bundle, bundle_errors = parse_yaml_like_file(bundle_path)
    except OSError:
        return False
    if bundle_errors:
        return False
    stored = str(bundle.get("bundle_sha256", ""))
    expected_verdict = "pass" if result.get("result") == "synced" else "fail"
    return bool(
        stored
        and stored == gate_evidence.bundle_sha(bundle)
        and stored == result.get("sync_gate_sha256")
        and bundle.get("gate_id") == result.get("sync_gate_id")
        and bundle.get("schema") == gate_evidence.SCHEMA
        and bundle.get("spec_id") == spec_dir.name
        and bundle.get("phase") == "sync"
        and bundle.get("gate") == "host_sync"
        and bundle.get("verdict") == expected_verdict
        and bundle.get("hook_exit_code") == result.get("hook_exit_code")
        and bundle.get("result") == result.get("result")
        and bundle.get("verify_gate_id") == result.get("verify_gate_id")
        and bundle.get("verified_tree") == result.get("verified_tree")
        and bundle.get("changed_paths_digest") == result.get("changed_paths_digest")
        and bundle.get("declared_delta_digest") == result.get("declared_delta_digest")
        and (scope.get("admission") != "readmission" or bundle.get("transaction_id") == result.get("transaction_id"))
        and bundle.get("sync_result_payload_sha256") == sync_result_payload_digest(result)
    )
