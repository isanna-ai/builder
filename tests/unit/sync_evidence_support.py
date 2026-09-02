from __future__ import annotations

from pathlib import Path

from _dispatch_runtime import gate_evidence
from _sync.evidence import (
    atomic_write_yaml,
    changed_paths_digest,
    sha256_bytes,
    sync_result_payload_digest,
    verified_tree_digest,
)


LOCKED_PATHS = ["amend the intent delta", "fix the SSOT", "file a narrowing task"]


def write_host_scope(root: Path, spec_id: str, changed_paths: list[str] | None = None) -> dict:
    paths = sorted(changed_paths or [])
    spec_dir = root / ".builder" / "specs" / spec_id
    evidence_dir = spec_dir / "gate-evidence"
    body = {
        "schema": gate_evidence.SCHEMA,
        "gate_id": "",
        "seq": 0,
        "gate": "host_verify",
        "polarity": "green",
        "spec_id": spec_id,
        "phase": "verify",
        "verdict": "pass",
        "git_head_sha": "head-1",
        "prev_bundle_sha256": "",
        "bundle_sha256": "",
    }
    bundle = gate_evidence.write_bundle(evidence_dir, body)
    assert bundle is not None
    delta = spec_dir / "ssot-delta.yaml"
    transaction_id = sha256_bytes(f"{spec_id}\0base-1\0seed".encode("utf-8"))
    atomic_write_yaml(spec_dir / "implementation-baseline.yaml", {
        "schema": "implementation-baseline/v1",
        "spec": spec_id,
        "implementation_baseline": "base-1",
        "baseline_paths": [],
        "baseline_paths_digest": changed_paths_digest([]),
        "worktree_root": str(root),
        "control_root": str(root.parent / "control"),
        "worktree_isolated": True,
        "transaction_id": transaction_id,
    })
    scope = {
        "schema": "sync-scope/v1",
        "spec": spec_id,
        "implementation_baseline": "base-1",
        "verified_head": "head-1",
        "verified_tree": verified_tree_digest(root, paths, "head-1"),
        "changed_paths": paths,
        "changed_paths_digest": changed_paths_digest(paths),
        "declared_delta_digest": sha256_bytes(delta.read_bytes()),
        "verify_gate_id": body["gate_id"],
        "verify_gate_bundle": f"gate-evidence/{bundle.name}",
        "verify_gate_sha256": body["bundle_sha256"],
        "worktree_root": str(root),
        "control_root": str(root.parent / "control"),
        "worktree_isolated": True,
        "transaction_id": transaction_id,
    }
    atomic_write_yaml(spec_dir / "sync-scope.yaml", scope)
    return scope


def write_sync_result(spec_dir: Path, scope: dict, result: str, *, undeclared=None) -> None:
    payload = {
        "spec": spec_dir.name,
        "worktree_root": scope["worktree_root"],
        "verify_gate_id": scope["verify_gate_id"],
        "verify_gate_sha256": scope["verify_gate_sha256"],
        "verified_tree": scope["verified_tree"],
        "changed_paths_digest": scope["changed_paths_digest"],
        "declared_delta_digest": scope["declared_delta_digest"],
        "preimage_manifest_digest": sha256_bytes(b""),
        "observed_tuples": undeclared or [],
        "undeclared_tuples": undeclared or [],
        "hook_exit_code": 0 if result == "synced" else 2,
        "publish_state": "published" if result == "synced" else "staged-only",
        "result": result,
        "resolution_paths": LOCKED_PATHS,
        "transaction_id": scope["transaction_id"],
    }
    body = {
        "schema": gate_evidence.SCHEMA,
        "gate_id": "",
        "seq": 0,
        "gate": "host_sync",
        "polarity": "green" if result == "synced" else "red",
        "spec_id": spec_dir.name,
        "phase": "sync",
        "verdict": "pass" if result == "synced" else "fail",
        "hook_exit_code": payload["hook_exit_code"],
        "result": result,
        "verify_gate_id": payload["verify_gate_id"],
        "verified_tree": payload["verified_tree"],
        "changed_paths_digest": payload["changed_paths_digest"],
        "declared_delta_digest": payload["declared_delta_digest"],
        "sync_result_payload_sha256": sync_result_payload_digest(payload),
        "prev_bundle_sha256": "",
        "bundle_sha256": "",
    }
    bundle = gate_evidence.write_bundle(spec_dir / "gate-evidence", body)
    assert bundle is not None
    payload["sync_gate_id"] = body["gate_id"]
    payload["sync_gate_bundle"] = f"gate-evidence/{bundle.name}"
    payload["sync_gate_sha256"] = body["bundle_sha256"]
    atomic_write_yaml(spec_dir / "sync-result.yaml", payload)
