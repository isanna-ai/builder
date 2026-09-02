from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from unittest import SkipTest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
FIXTURES = ROOT / "tests" / "fixtures" / "builder_project_model" / "gate_coverage" / "v1"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


def _load_module():
    spec = importlib.util.spec_from_file_location("gate_coverage_contracts", SCRIPTS / "gate-coverage.py")
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


gc_mod = _load_module()


def _write_yaml(path: Path, data: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return path


def _repo(tmp_path, *, declared=".builder/dispatch-queue") -> Path:
    root = Path(tmp_path)
    (root / ".builder").mkdir(parents=True)
    _write_yaml(root / ".builder" / "dispatch.yaml", {"queue_store": {"path": declared}})
    return root


def _spec(root: Path, spec_id: str, status: str) -> Path:
    return _write_yaml(root / ".builder" / "specs" / spec_id / "spec.yaml", {"status": status})


def _attempt(queue_root: Path, *, spec_id: str, phase: str, gates: dict | None, attempt_id: str, created_at: str = "2026-07-13T00:00:00Z") -> None:
    metadata = {
        "spec_id": spec_id,
        "phase": phase,
        "decision": "phase-complete",
        "reason": "outcome: SUCCEEDED",
        "started_at": created_at,
    }
    if gates is not None:
        metadata["gates"] = gates
    _write_yaml(
        queue_root / "queue" / "attempts" / f"{attempt_id}.yaml",
        {"attempt_id": attempt_id, "metadata": metadata, "created_at": created_at},
    )


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_gate_coverage_characterization_legacy_unknown_self_certified_and_gates_off(tmp_path):
    root = _repo(tmp_path)
    queue = root / ".builder" / "dispatch-queue"
    _spec(root, "demo", "verified")
    _attempt(queue, spec_id="demo", phase="implement", gates=None, attempt_id="attempt-1")
    _attempt(queue, spec_id="demo", phase="verify", gates=None, attempt_id="attempt-2")
    report = gc_mod.scan_repo(root)
    expected = _fixture("legacy-unknown.json")
    assert report["specs"][0]["verification"] == expected["verification"]
    assert report["coverage"]["host_verify"] == expected["host_verify"]

    root = _repo(tmp_path / "self")
    queue = root / ".builder" / "dispatch-queue"
    _spec(root, "demo", "verified")
    _attempt(
        queue,
        spec_id="demo",
        phase="verify",
        gates={"host_verify": "abstain:no_commands", "source_diff": "abstain:non_gated_phase"},
        attempt_id="attempt-self",
    )
    report = gc_mod.scan_repo(root)
    expected = _fixture("self-certified.json")
    assert report["specs"][0]["verification"] == expected["verification"]
    assert report["specs"][0]["findings"][0]["class"] == expected["finding_class"]

    root = _repo(tmp_path / "off")
    queue = root / ".builder" / "dispatch-queue"
    _spec(root, "demo", "verified")
    _attempt(
        queue,
        spec_id="demo",
        phase="verify",
        gates={"host_verify": "abstain:off", "source_diff": "abstain:non_gated_phase"},
        attempt_id="attempt-off",
    )
    report = gc_mod.scan_repo(root)
    expected = _fixture("gates-off.json")
    assert report["specs"][0]["verification"] == expected["verification"]
    assert report["specs"][0]["findings"][0]["class"] == expected["finding_class"]


def test_gate_coverage_characterization_worst_case_wins_and_unreadable_becomes_blindness(tmp_path):
    if os.geteuid() == 0:
        raise SkipTest("root bypasses file permissions, so unreadable-evidence behavior cannot be tested as root")
    root = _repo(tmp_path)
    queue = root / ".builder" / "dispatch-queue"
    spec_yaml = _spec(root, "demo", "verified")
    _attempt(
        queue,
        spec_id="demo",
        phase="implement",
        gates={"host_verify": "fail:assertion_failure", "source_diff": "pass"},
        attempt_id="attempt-bad",
    )
    _attempt(
        queue,
        spec_id="demo",
        phase="verify",
        gates={"host_verify": "pass", "source_diff": "abstain:non_gated_phase"},
        attempt_id="attempt-good",
    )
    report = gc_mod.scan_repo(root)
    expected = _fixture("worst-case-wins.json")
    assert report["specs"][0]["verification"] == expected["verification"]
    assert report["specs"][0]["findings"][0]["class"] == expected["finding_class"]

    spec_yaml.chmod(0)
    try:
        blind = gc_mod.scan_repo(root)["specs"][0]
        expected = _fixture("blindness-unreadable.json")
        assert blind["claim"] == expected["claim"]
        assert blind["blindness"][0]["class"] == expected["blindness_class"]
    finally:
        spec_yaml.chmod(0o600)
