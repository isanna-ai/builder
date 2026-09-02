from __future__ import annotations

from pathlib import Path

from _dispatch_runtime.phase_runtime import write_sync_result
from _sync.readmit import BOOTSTRAP_AUTHORIZATION, readmit_spec
from _validators.common import parse_yaml_like_file
from tests.unit.test_sync_readmit_cli import _seed_spec


def test_sync_result_carries_bootstrap_exception_visibility(tmp_path: Path):
    spec_dir = _seed_spec(tmp_path, "demo")
    readmit_spec(tmp_path, "demo")
    scope, _ = parse_yaml_like_file(spec_dir / "sync-scope.yaml")
    payload = {
        "spec": "demo",
        "worktree_root": str(tmp_path),
        "verify_gate_id": scope["verify_gate_id"],
        "verify_gate_sha256": scope["verify_gate_sha256"],
        "verified_tree": scope["verified_tree"],
        "changed_paths_digest": scope["changed_paths_digest"],
        "declared_delta_digest": scope["declared_delta_digest"],
        "preimage_manifest_digest": "0" * 64,
        "observed_tuples": [],
        "undeclared_tuples": [],
        "hook_exit_code": 2,
        "publish_state": "staged-only",
        "result": "bootstrap_required",
        "resolution_paths": ["amend the intent delta", "fix the SSOT", "file a narrowing task"],
        "sync_gate_id": "sg",
        "sync_gate_bundle": "gate-evidence/x.yaml",
        "sync_gate_sha256": "1" * 64,
        "transaction_id": scope["transaction_id"],
        "provenance": "bootstrap-exception",
        "owner_authorization": BOOTSTRAP_AUTHORIZATION,
        "derived_baseline": scope["derived_baseline"],
    }
    write_sync_result(spec_dir, payload)
    result, _ = parse_yaml_like_file(spec_dir / "sync-result.yaml")
    assert result["owner_authorization"] == BOOTSTRAP_AUTHORIZATION

