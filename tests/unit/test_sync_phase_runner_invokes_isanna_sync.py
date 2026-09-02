from __future__ import annotations

from pathlib import Path

import isanna as isanna_cli
from _yaml import yaml
from _dispatch_runtime.lane_common import Work, _run_host_sync_phase
from _validators.common import parse_yaml_like_file
from tests.unit.sync_evidence_support import write_host_scope


def test_isanna_sync_runs_against_a_named_spec(tmp_path: Path):
    (tmp_path / ".builder" / "specs" / "demo").mkdir(parents=True)
    (tmp_path / ".builder" / "specs" / "demo" / "spec.yaml").write_text("status: verified\ncurrent_phase: sync\n", encoding="utf-8")
    (tmp_path / ".builder" / "specs" / "demo" / "ssot-delta.yaml").write_text(
        "capabilities: []\nbehaviors: []\njourneys: []\n", encoding="utf-8"
    )
    (tmp_path / "tests" / "unit").mkdir(parents=True)
    (tmp_path / "tests" / "unit" / "test_demo.py").write_text("def test_sync_guard():\n    pass\n", encoding="utf-8")
    (tmp_path / "Makefile").write_text("gate:\n\tpytest tests/unit/test_demo.py -q\n", encoding="utf-8")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "system-behaviors.yaml").write_text(
        "schema: system-behaviors/v1\nbehaviors:\n  - id: b1\n    area: x\n    behavior: y\n    invariant: z\n    breaks_when: never\n    guarding_tests:\n      - tests/unit/test_demo.py::test_sync_guard\n",
        encoding="utf-8",
    )
    (tmp_path / ".builder" / "sync-adapter.yaml").write_text(
        "artifact: sync-adapter\nmappings: []\n", encoding="utf-8"
    )
    scope = write_host_scope(tmp_path, "demo")
    code = isanna_cli.main(["sync", "--root", str(tmp_path), "--spec", "demo", "--scope-evidence", str(tmp_path / ".builder" / "specs" / "demo" / "sync-scope.yaml")])
    assert code == 0


def test_host_phase_runtime_invokes_sync_and_projects_only_its_result(tmp_path: Path):
    spec_dir = tmp_path / ".builder" / "specs" / "demo"
    spec_dir.mkdir(parents=True)
    spec_dir.joinpath("spec.yaml").write_text("name: demo\nstatus: syncing\ncurrent_phase: sync\n", encoding="utf-8")
    spec_dir.joinpath("ssot-delta.yaml").write_text("capabilities: []\nbehaviors: []\njourneys: []\n", encoding="utf-8")
    (tmp_path / ".builder" / "sync-adapter.yaml").write_text("artifact: sync-adapter\nmappings: []\n", encoding="utf-8")
    (tmp_path / "tests" / "unit").mkdir(parents=True)
    (tmp_path / "tests" / "unit" / "test_demo.py").write_text("def test_sync_guard():\n    pass\n", encoding="utf-8")
    (tmp_path / "Makefile").write_text("gate:\n\tpytest tests/unit/test_demo.py -q\n", encoding="utf-8")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "system-behaviors.yaml").write_text(
        "schema: system-behaviors/v1\nbehaviors:\n  - id: b1\n    area: x\n    behavior: y\n    invariant: z\n    breaks_when: never\n    guarding_tests:\n      - tests/unit/test_demo.py::test_sync_guard\n",
        encoding="utf-8",
    )
    write_host_scope(tmp_path, "demo")
    work = Work("w1", "demo", "sync", tmp_path, tmp_path / ".builder" / "specs", None, None, tmp_path, tmp_path / "attempt.log")
    code, _detail = _run_host_sync_phase(work)
    spec, errors = parse_yaml_like_file(spec_dir / "spec.yaml")
    assert not errors and code == 0, _detail
    assert spec["status"] == "synced" and spec["current_phase"] == "sync"
    result, errors = parse_yaml_like_file(spec_dir / "sync-result.yaml")
    assert not errors
    assert result["sync_gate_id"].startswith("demo:sync:host_sync:")
    assert result["sync_gate_bundle"].startswith("gate-evidence/")


def test_host_phase_runtime_does_not_reuse_a_stale_divergence_result(tmp_path: Path):
    spec_dir = tmp_path / ".builder" / "specs" / "demo"
    spec_dir.mkdir(parents=True)
    spec_dir.joinpath("spec.yaml").write_text(
        "name: demo\nstatus: syncing\ncurrent_phase: sync\n", encoding="utf-8"
    )
    spec_dir.joinpath("ssot-delta.yaml").write_text(
        "capabilities: []\nbehaviors: []\njourneys: []\n", encoding="utf-8"
    )
    write_host_scope(tmp_path, "demo")
    spec_dir.joinpath("sync-result.yaml").write_text("result: divergence\n", encoding="utf-8")
    work = Work(
        "w1", "demo", "sync", tmp_path, tmp_path / ".builder" / "specs",
        None, None, tmp_path, tmp_path / "attempt.log",
    )
    code, _detail = _run_host_sync_phase(work)
    spec, errors = parse_yaml_like_file(spec_dir / "spec.yaml")
    assert not errors and code != 0
    assert spec["status"] == "syncing"
    result, errors = parse_yaml_like_file(spec_dir / "sync-result.yaml")
    assert not errors and result["result"] == "bootstrap_required"
    assert "sync_gate_id" in result


def test_host_phase_runtime_keeps_invalid_scope_failure_syncing(tmp_path: Path):
    spec_dir = tmp_path / ".builder" / "specs" / "demo"
    spec_dir.mkdir(parents=True)
    spec_dir.joinpath("spec.yaml").write_text(
        "name: demo\nstatus: verified\ncurrent_phase: sync\n", encoding="utf-8"
    )
    spec_dir.joinpath("ssot-delta.yaml").write_text(
        "capabilities: []\nbehaviors: []\njourneys: []\n", encoding="utf-8"
    )
    work = Work(
        "w1", "demo", "sync", tmp_path, tmp_path / ".builder" / "specs",
        None, None, tmp_path, tmp_path / "attempt.log",
    )
    code, _detail = _run_host_sync_phase(work)
    spec, errors = parse_yaml_like_file(spec_dir / "spec.yaml")
    assert not errors and code != 0
    assert spec["status"] == "syncing" and spec["current_phase"] == "sync"


def test_host_phase_runtime_repairs_legacy_scope_transaction_binding(tmp_path: Path):
    spec_dir = tmp_path / ".builder" / "specs" / "demo"
    spec_dir.mkdir(parents=True)
    spec_dir.joinpath("spec.yaml").write_text("name: demo\nstatus: syncing\ncurrent_phase: sync\n", encoding="utf-8")
    spec_dir.joinpath("ssot-delta.yaml").write_text("capabilities: []\nbehaviors: []\njourneys: []\n", encoding="utf-8")
    (tmp_path / ".builder" / "sync-adapter.yaml").write_text("artifact: sync-adapter\nmappings: []\n", encoding="utf-8")
    (tmp_path / "tests" / "unit").mkdir(parents=True)
    (tmp_path / "tests" / "unit" / "test_demo.py").write_text("def test_sync_guard():\n    pass\n", encoding="utf-8")
    (tmp_path / "Makefile").write_text("gate:\n\tpytest tests/unit/test_demo.py -q\n", encoding="utf-8")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "system-behaviors.yaml").write_text(
        "schema: system-behaviors/v1\nbehaviors:\n  - id: b1\n    area: x\n    behavior: y\n    invariant: z\n    breaks_when: never\n    guarding_tests:\n      - tests/unit/test_demo.py::test_sync_guard\n",
        encoding="utf-8",
    )
    write_host_scope(tmp_path, "demo")
    baseline_path = spec_dir / "implementation-baseline.yaml"
    scope_path = spec_dir / "sync-scope.yaml"
    baseline = parse_yaml_like_file(baseline_path)[0]
    scope = parse_yaml_like_file(scope_path)[0]
    baseline.pop("transaction_id", None)
    scope.pop("transaction_id", None)
    baseline_path.write_text(yaml.safe_dump(baseline, sort_keys=False, allow_unicode=True), encoding="utf-8")
    scope_path.write_text(yaml.safe_dump(scope, sort_keys=False, allow_unicode=True), encoding="utf-8")

    work = Work("w1", "demo", "sync", tmp_path, tmp_path / ".builder" / "specs", None, None, tmp_path, tmp_path / "attempt.log")
    code, detail = _run_host_sync_phase(work)

    repaired_baseline, baseline_errors = parse_yaml_like_file(baseline_path)
    repaired_scope, scope_errors = parse_yaml_like_file(scope_path)
    assert not baseline_errors and not scope_errors and code == 0, detail
    assert repaired_baseline["transaction_id"] == repaired_scope["transaction_id"]
