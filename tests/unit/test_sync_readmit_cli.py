from __future__ import annotations

from pathlib import Path
import subprocess

import isanna
from _dispatch_runtime import gate_evidence


def _seed_spec(root: Path, spec_id: str, *, status: str = "verified") -> Path:
    if not (root / ".git").exists():
        root.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        subprocess.run(["git", "-C", str(root), "config", "user.email", "tests@example.invalid"], check=True)  # publish-ok: test git identity fixture
        subprocess.run(["git", "-C", str(root), "config", "user.name", "Builder Tests"], check=True)
        root.joinpath("README.md").write_text("baseline\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(root), "add", "README.md"], check=True)
        subprocess.run(["git", "-C", str(root), "commit", "-qm", "baseline"], check=True)
    baseline = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"], check=True, capture_output=True, text=True,
    ).stdout.strip()
    spec_dir = root / ".builder" / "specs" / spec_id
    spec_dir.mkdir(parents=True)
    spec_dir.joinpath("spec.yaml").write_text(f"name: {spec_id}\nstatus: {status}\ncurrent_phase: sync\n", encoding="utf-8")
    spec_dir.joinpath("ssot-delta.yaml").write_text("capabilities: []\nbehaviors: []\njourneys: []\n", encoding="utf-8")
    gate = spec_dir / "gate-evidence"
    gate.mkdir()
    baseline_body = {
        "schema": gate_evidence.SCHEMA,
        "gate_id": "",
        "seq": 0,
        "gate": "red_baseline",
        "polarity": "red",
        "spec_id": spec_id,
        "phase": "plan",
        "verdict": "pass",
        "git_head_sha": baseline,
        "prev_bundle_sha256": "",
        "bundle_sha256": "",
    }
    assert gate_evidence.write_bundle(gate, baseline_body) is not None
    source = root / "src" / f"{spec_id}.txt"
    source.parent.mkdir(exist_ok=True)
    source.write_text(f"{spec_id}\n", encoding="utf-8")
    command = (
        "python3 -c \"from pathlib import Path; "
        f"assert Path('src/{spec_id}.txt').read_text() == '{spec_id}\\\\n'\""
    )
    body = {
        "schema": gate_evidence.SCHEMA,
        "gate_id": "",
        "seq": 0,
        "gate": "host_verify",
        "polarity": "green",
        "spec_id": spec_id,
        "phase": "verify",
        "verdict": "pass",
        "git_head_sha": baseline,
        "finished_at": "2026-07-20T12:00:00Z",
        "diff_stat": {"files_changed": 1, "insertions": 1, "deletions": 0, "files": [f"src/{spec_id}.txt"]},
        "commands": [{"command": command, "exit_code": 0}],
        "prev_bundle_sha256": "",
        "bundle_sha256": "",
    }
    assert gate_evidence.write_bundle(gate, body) is not None
    return spec_dir


def test_sync_readmit_requires_named_spec(tmp_path: Path):
    code = isanna.main(["sync-readmit", "--root", str(tmp_path), "--spec", "missing"])
    assert code == 2


def test_sync_readmit_rejects_already_synced(tmp_path: Path):
    _seed_spec(tmp_path, "demo", status="synced")
    code = isanna.main(["sync-readmit", "--root", str(tmp_path), "--spec", "demo"])
    assert code == 2


def test_sync_readmit_writes_spec_local_scope_only_for_the_named_spec(tmp_path: Path):
    _seed_spec(tmp_path, "demo")
    _seed_spec(tmp_path, "other")
    code = isanna.main(["sync-readmit", "--root", str(tmp_path), "--spec", "demo"])
    assert code == 0
    assert (tmp_path / ".builder" / "specs" / "demo" / "sync-scope.yaml").is_file()
    assert not (tmp_path / ".builder" / "specs" / "other" / "sync-scope.yaml").exists()
