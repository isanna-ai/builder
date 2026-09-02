"""Tests for scripts/gate-coverage.py."""

from __future__ import annotations

import importlib.util
import io
import os
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path
from unittest import SkipTest

_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "gate-coverage.py"
SCRIPTS = _SCRIPT_PATH.parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


def _load_module():
    spec = importlib.util.spec_from_file_location("gate_coverage", _SCRIPT_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # Register before exec: dataclasses (Py>=3.14) resolve annotations via
    # sys.modules[cls.__module__]; a path-loaded module must be discoverable there.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


gc_mod = _load_module()

GP = {
    "host_verify": "pass",
    "source_diff": "pass",
    "red_baseline": "abstain:non_gated_phase",
    "packet_contract": "abstain:off",
}


def _write_yaml(path: Path, data: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return path


def _repo(tmp_path, *, declared=".builder/dispatch-queue") -> Path:
    root = Path(tmp_path)
    (root / ".builder").mkdir(parents=True)
    _write_yaml(root / ".builder" / "dispatch.yaml", {"queue_store": {"path": declared}})
    return root


def _skip_unreadable_permission_probe_as_root() -> None:
    if os.geteuid() == 0:
        raise SkipTest("root bypasses file permissions, so unreadable-evidence behavior cannot be tested as root")


def _attempt(
    queue_root,
    *,
    spec_id,
    phase,
    decision,
    reason="",
    gates=None,
    gate_evidence=None,
    created_at="2026-07-13T00:00:00Z",
    attempt_id=None,
) -> Path:
    attempt_id = attempt_id or f"attempt-{len(list((Path(queue_root) / 'queue' / 'attempts').glob('*.yaml'))) + 1}"
    metadata = {
        "spec_id": spec_id,
        "phase": phase,
        "decision": decision,
        "reason": reason,
        "started_at": created_at,
    }
    if gates is not None:
        metadata["gates"] = gates
    if gate_evidence is not None:
        metadata["gate_evidence"] = gate_evidence
    return _write_yaml(
        Path(queue_root) / "queue" / "attempts" / f"{attempt_id}.yaml",
        {"attempt_id": attempt_id, "metadata": metadata, "created_at": created_at},
    )


def _spec(root, spec_id, status) -> Path:
    spec = Path(root) / ".builder" / "specs" / spec_id
    _write_yaml(spec / "spec.yaml", {"status": status})
    return spec


def _report(root: Path) -> dict:
    return gc_mod.scan_repo(root)


def test_legacy_attempts_unknown_never_covered(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    queue = root / ".builder" / "dispatch-queue"
    _spec(root, "demo", "verified")
    _attempt(queue, spec_id="demo", phase="implement", decision="phase-complete", reason="outcome: SUCCEEDED")
    _attempt(queue, spec_id="demo", phase="verify", decision="phase-complete", reason="outcome: SUCCEEDED")

    report = _report(root)
    assert report["coverage"]["host_verify"] == {
        "adjudicated": 0,
        "claimed": 0,
        "unknown": 2,
        "not_applicable": 0,
    }
    spec = report["specs"][0]
    assert spec["verification"] == "unknown"
    assert spec["unknown_turns"] == 2


def test_self_certified_detected(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    queue = root / ".builder" / "dispatch-queue"
    _spec(root, "demo", "verified")
    _attempt(
        queue,
        spec_id="demo",
        phase="verify",
        decision="phase-complete",
        reason="phase 'verify' completed with outcome: SUCCEEDED",
        gates={"host_verify": "abstain:no_commands", "source_diff": "abstain:non_gated_phase"},
        attempt_id="attempt-self",
    )

    spec = _report(root)["specs"][0]
    assert spec["verification"] == "self-reported"
    assert spec["findings"][0]["class"] == "self-certified"
    assert spec["findings"][0]["attempt_id"] == "attempt-self"
    assert spec["findings"][0]["phase"] == "verify"
    assert gc_mod.main(["--root", str(root), "--check"]) == 1


def test_abstain_off_not_self_certified(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    queue = root / ".builder" / "dispatch-queue"
    _spec(root, "demo", "verified")
    _attempt(
        queue,
        spec_id="demo",
        phase="verify",
        decision="phase-complete",
        reason="outcome: SUCCEEDED",
        gates={"host_verify": "abstain:off", "source_diff": "abstain:non_gated_phase"},
    )

    spec = _report(root)["specs"][0]
    assert [f["class"] for f in spec["findings"]] == ["gates-off"]
    assert spec["verification"] == "self-reported"
    assert gc_mod.main(["--root", str(root), "--check"]) == 0


def test_host_verified_happy_path(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    queue = root / ".builder" / "dispatch-queue"
    _spec(root, "demo", "verified")
    _attempt(queue, spec_id="demo", phase="implement", decision="phase-complete", reason="outcome: SUCCEEDED", gates=GP)
    _attempt(
        queue,
        spec_id="demo",
        phase="verify",
        decision="phase-complete",
        reason="outcome: SUCCEEDED",
        gates={"host_verify": "pass", "source_diff": "abstain:non_gated_phase"},
    )

    report = _report(root)
    assert report["specs"][0]["verification"] == "host-verified"
    assert report["specs"][0]["findings"] == []
    assert report["coverage"]["host_verify"]["adjudicated"] == 2
    assert report["coverage"]["host_verify"]["claimed"] == 2


def test_healthy_repo_has_zero_blindness(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    queue = root / ".builder" / "dispatch-queue"
    _spec(root, "demo", "verified")
    _attempt(
        queue,
        spec_id="demo",
        phase="verify",
        decision="phase-complete",
        reason="outcome: SUCCEEDED",
        gates={"host_verify": "pass", "source_diff": "abstain:non_gated_phase"},
    )

    report = _report(root)
    assert report["blindness"] == []
    assert report["specs"][0]["blindness"] == []
    assert report["specs"][0]["verification"] == "host-verified"


def test_healthy_symlinked_queue_root_has_zero_blindness(tmp_path: Path) -> None:
    root = _repo(tmp_path / "repo")
    real_queue = tmp_path / "queue-store"
    (root / ".builder" / "dispatch-queue").symlink_to(real_queue, target_is_directory=True)
    _spec(root, "demo", "verified")
    _attempt(
        real_queue,
        spec_id="demo",
        phase="verify",
        decision="phase-complete",
        reason="outcome: SUCCEEDED",
        gates={"host_verify": "pass", "source_diff": "abstain:non_gated_phase"},
    )

    report = _report(root)
    assert report["blindness"] == []
    assert report["specs"][0]["blindness"] == []
    assert report["specs"][0]["verification"] == "host-verified"


def test_unreadable_spec_claim_is_blindness(tmp_path: Path) -> None:
    _skip_unreadable_permission_probe_as_root()
    root = _repo(tmp_path)
    queue = root / ".builder" / "dispatch-queue"
    spec_yaml = _spec(root, "demo", "verified") / "spec.yaml"
    _attempt(
        queue,
        spec_id="demo",
        phase="verify",
        decision="phase-complete",
        reason="outcome: SUCCEEDED",
        gates={"host_verify": "pass", "source_diff": "abstain:non_gated_phase"},
    )
    spec_yaml.chmod(0)

    try:
        report = _report(root)
        row = report["specs"][0]
        assert row["claim"] == "?"
        assert row["verification"] != "host-verified"
        assert [item["class"] for item in row["blindness"]] == ["spec-claim-unreadable"]
    finally:
        spec_yaml.chmod(0o600)


def test_mixed_attempts_worst_case_wins(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    queue = root / ".builder" / "dispatch-queue"
    _spec(root, "demo", "verified")
    bad = dict(GP)
    bad["host_verify"] = "fail:assertion_failure"
    _attempt(queue, spec_id="demo", phase="implement", decision="phase-complete", reason="outcome: SUCCEEDED", gates=bad)
    _attempt(
        queue,
        spec_id="demo",
        phase="verify",
        decision="phase-complete",
        reason="outcome: SUCCEEDED",
        gates={"host_verify": "pass", "source_diff": "abstain:non_gated_phase"},
    )

    spec = _report(root)["specs"][0]
    assert spec["verification"] == "self-reported"
    assert [f["class"] for f in spec["findings"]] == ["host-contradicted"]
    assert gc_mod.main(["--root", str(root), "--check"]) == 1


def test_unknown_beats_verified_but_not_findings(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    queue = root / ".builder" / "dispatch-queue"
    _spec(root, "demo", "verified")
    _attempt(queue, spec_id="demo", phase="implement", decision="phase-complete", reason="outcome: SUCCEEDED")
    _attempt(
        queue,
        spec_id="demo",
        phase="verify",
        decision="phase-complete",
        reason="outcome: SUCCEEDED",
        gates={"host_verify": "pass", "source_diff": "abstain:non_gated_phase"},
    )
    assert _report(root)["specs"][0]["verification"] == "unknown"

    _attempt(
        queue,
        spec_id="demo",
        phase="verify",
        decision="phase-complete",
        reason="outcome: SUCCEEDED",
        gates={"host_verify": "abstain:no_commands", "source_diff": "abstain:non_gated_phase"},
    )
    assert _report(root)["specs"][0]["verification"] == "self-reported"


def test_turn_incomplete_not_in_denominator(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    queue = root / ".builder" / "dispatch-queue"
    _attempt(
        queue,
        spec_id="demo",
        phase="implement",
        decision="resume-same-session",
        gates={gate: "abstain:turn_incomplete" for gate in gc_mod.GATE_NAMES},
    )

    coverage = _report(root)["coverage"]
    assert coverage == {
        "host_verify": {"adjudicated": 0, "claimed": 0, "unknown": 0, "not_applicable": 1},
        "source_diff": {"adjudicated": 0, "claimed": 0, "unknown": 0, "not_applicable": 1},
        "red_baseline": {"adjudicated": 0, "claimed": 0, "unknown": 0, "not_applicable": 0},
        "packet_contract": {"adjudicated": 0, "claimed": 0, "unknown": 0, "not_applicable": 1},
    }


def test_rework_loop_counted_and_audited(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    queue = root / ".builder" / "dispatch-queue"
    _spec(root, "demo", "verified")
    _attempt(
        queue,
        spec_id="demo",
        phase="verify",
        decision="phase-complete",
        reason="outcome: VERIFIED_WITH_TASKS",
        gates={"host_verify": "abstain:no_commands", "source_diff": "abstain:non_gated_phase"},
        created_at="2026-07-13T00:00:00Z",
    )
    _attempt(
        queue,
        spec_id="demo",
        phase="verify",
        decision="phase-complete",
        reason="outcome: SUCCEEDED",
        gates={"host_verify": "pass", "source_diff": "abstain:non_gated_phase"},
        created_at="2026-07-13T00:01:00Z",
    )

    spec = _report(root)["specs"][0]
    assert spec["rework_loops"] == 1
    assert spec["verification"] == "self-reported"
    assert [f["class"] for f in spec["findings"]] == ["self-certified"]


def test_in_flight_spec_unstamped(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    queue = root / ".builder" / "dispatch-queue"
    _spec(root, "demo", "implementing")
    _attempt(queue, spec_id="demo", phase="implement", decision="phase-complete", reason="outcome: SUCCEEDED", gates=GP)

    spec = _report(root)["specs"][0]
    assert spec["verification"] is None
    assert spec["findings"] == []


def test_completed_claim_with_no_host_record(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _spec(root, "demo", "verified")

    spec = _report(root)["specs"][0]
    assert spec["verification"] == "unknown"
    assert [f["class"] for f in spec["findings"]] == ["no-host-record"]
    assert gc_mod.main(["--root", str(root), "--check"]) == 0


def test_queue_root_found_when_dispatch_yaml_declares_a_container_path(tmp_path: Path) -> None:
    """REGRESSION: dispatch.yaml is authored for the CONTAINER, so on the host its declared
    queue_store.path (e.g. /path/to/project/.builder/dispatch-queue) does not exist. The
    original resolution derived every fallback candidate from declared.parent, so they all inherited
    the dead prefix and the REAL queue -- sitting right next to the config -- was never a candidate.
    The tool then reported "no dispatch history" for every repo while the attempts sat on
    disk. An audit tool that silently sees nothing is worse than no audit tool.
    """
    root = _repo(tmp_path, declared="/path/to/project/.builder/dispatch-queue")
    queue_root = root / ".builder" / "dispatch-queue"
    _attempt(
        queue_root,
        spec_id="s1",
        phase="verify",
        decision="phase-complete",
        reason="phase 'verify' completed with outcome: SUCCEEDED",
        gates=None,  # legacy: no gate record
    )
    resolved, source, _ = gc_mod.resolve_queue_root(root, None)
    assert resolved == queue_root.resolve(), "root-relative queue must be found despite a dead declared path"
    assert source == "fallback"

    report = _report(root)
    assert report["coverage"]["host_verify"]["unknown"] == 1, "the real attempt must be seen, as unknown"


def test_queue_root_sqlite3_red_herring(tmp_path: Path) -> None:
    root = _repo(tmp_path, declared=".builder/dispatch-queue.sqlite3")
    declared = root / ".builder" / "dispatch-queue.sqlite3"
    sibling = root / ".builder" / "dispatch-queue"
    (declared / "queue").mkdir(parents=True)
    _attempt(sibling, spec_id="demo", phase="verify", decision="phase-complete", gates={"host_verify": "pass"})

    resolved, source, _ = gc_mod.resolve_queue_root(root, None)
    assert resolved == sibling.resolve()
    assert source == "candidates"

    root2 = _repo(tmp_path / "second", declared=".builder/dispatch-queue.sqlite3")
    declared2 = root2 / ".builder" / "dispatch-queue.sqlite3"
    _attempt(declared2, spec_id="demo", phase="verify", decision="phase-complete", gates={"host_verify": "pass"})
    resolved2, source2, _ = gc_mod.resolve_queue_root(root2, None)
    assert resolved2 == declared2.resolve()
    assert source2 == "config"


def test_gate_specific_denominators(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    queue = root / ".builder" / "dispatch-queue"
    _attempt(
        queue,
        spec_id="demo",
        phase="plan",
        decision="phase-complete",
        gates={
            "host_verify": "abstain:non_gated_phase",
            "source_diff": "abstain:non_gated_phase",
            "red_baseline": "pass",
            "packet_contract": "abstain:non_gated_phase",
        },
    )

    report = _report(root)
    assert report["coverage"]["red_baseline"]["claimed"] == 1
    assert report["coverage"]["red_baseline"]["adjudicated"] == 1
    assert report["coverage"]["host_verify"]["claimed"] == 0


def test_rejected_turn_gate_fail_is_covered(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    queue = root / ".builder" / "dispatch-queue"
    gates = dict(GP)
    gates["host_verify"] = "fail:assertion_failure"
    _attempt(queue, spec_id="demo", phase="implement", decision="resume-same-session", gates=gates)

    report = _report(root)
    assert report["coverage"]["host_verify"]["adjudicated"] == 1
    assert report["coverage"]["host_verify"]["claimed"] == 1
    assert report["specs"][0]["findings"] == []


def test_unreadable_attempt_counted_not_fatal(tmp_path: Path) -> None:
    _skip_unreadable_permission_probe_as_root()
    root = _repo(tmp_path)
    attempt = root / ".builder" / "dispatch-queue" / "queue" / "attempts" / "bad.yaml"
    attempt.parent.mkdir(parents=True)
    attempt.write_text("{}\n", encoding="utf-8")
    attempt.chmod(0)

    try:
        report = _report(root)
        assert report["diagnostics"]["unreadable_attempts"] == 1
        assert [item["class"] for item in report["blindness"]] == ["unreadable-attempt"]
        assert gc_mod.main(["--root", str(root)]) == 0
    finally:
        attempt.chmod(0o600)


def test_malformed_gates_mapping_is_unknown(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    queue = root / ".builder" / "dispatch-queue"
    _spec(root, "demo", "verified")
    _attempt(
        queue,
        spec_id="demo",
        phase="verify",
        decision="phase-complete",
        reason="outcome: SUCCEEDED",
        gates="not-a-map",
    )

    report = _report(root)
    assert report["specs"][0]["verification"] == "unknown"
    assert report["coverage"]["host_verify"]["unknown"] == 1


def test_json_shape(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _spec(root, "demo", "implementing")

    report = _report(root)
    assert set(report) == {
        "root", "queue_root", "queue_roots", "queue_root_source", "queue_candidates",
        "coverage", "specs", "diagnostics", "blindness",
    }
    assert set(report["coverage"]) == set(gc_mod.GATE_NAMES)
    assert all(
        set(stats) == {"adjudicated", "claimed", "unknown", "not_applicable"}
        for stats in report["coverage"].values()
    )
    spec = report["specs"][0]
    assert set(spec) == {
        "spec", "claim", "verification", "accepted_turns", "unknown_turns",
        "rework_loops", "findings", "blindness", "chain",
    }
    assert spec["verification"] is None
    assert spec["blindness"] == []
    assert spec["chain"] == {"checked": False}
    assert report["diagnostics"] == {"unreadable_attempts": 0, "attempts_without_spec_id": 0}


def test_exit_zero_without_check_despite_findings(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    queue = root / ".builder" / "dispatch-queue"
    _spec(root, "demo", "verified")
    _attempt(
        queue,
        spec_id="demo",
        phase="verify",
        decision="phase-complete",
        reason="outcome: SUCCEEDED",
        gates={"host_verify": "abstain:no_commands", "source_diff": "abstain:non_gated_phase"},
    )

    report = _report(root)
    assert [finding["class"] for finding in report["specs"][0]["findings"]] == ["self-certified"]
    assert gc_mod.main(["--root", str(root)]) == 0


def test_all_scans_multiple_repos(tmp_path: Path) -> None:
    parent = tmp_path / "projects"
    repo_b = _repo(parent / "b")
    repo_a = _repo(parent / "a")
    _spec(repo_b, "b-spec", "implementing")
    _spec(repo_a, "a-spec", "implementing")

    stdout = io.StringIO()
    with redirect_stdout(stdout):
        assert gc_mod.main(["--all", str(parent), "--json"]) == 0
    out = json.loads(stdout.getvalue())
    assert len(out["repos"]) == 2
    assert [Path(r["root"]).name for r in out["repos"]] == ["a", "b"]


def test_missing_builder_exits_2(tmp_path: Path) -> None:
    assert gc_mod.main(["--root", str(tmp_path)]) == 2


def test_chain_cross_check_missing_bundle(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    queue = root / ".builder" / "dispatch-queue"
    _spec(root, "demo", "verified")
    _attempt(
        queue,
        spec_id="demo",
        phase="verify",
        decision="phase-complete",
        reason="outcome: SUCCEEDED",
        gates={"host_verify": "pass", "source_diff": "abstain:non_gated_phase"},
        gate_evidence=[{"path": "gate-evidence/missing.yaml", "sha256": "abc"}],
    )

    spec = gc_mod.scan_repo(root, check_chain=True)["specs"][0]
    assert "chain-violation" in [f["class"] for f in spec["findings"]]
    assert gc_mod.main(["--root", str(root), "--verify-chain", "--check"]) == 1


def test_chain_intact_via_real_bundles(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    queue = root / ".builder" / "dispatch-queue"
    spec = _spec(root, "demo", "verified")
    if str(Path(__file__).resolve().parents[2] / "scripts") not in sys.path:
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
    from _dispatch_runtime import gate_evidence

    body1 = {"spec_id": "demo", "phase": "verify", "gate": "host_verify", "verdict": "pass"}
    body2 = {"spec_id": "demo", "phase": "implement", "gate": "source_diff", "verdict": "pass"}
    path1 = gate_evidence.write_bundle(spec / "gate-evidence", body1)
    path2 = gate_evidence.write_bundle(spec / "gate-evidence", body2)
    assert path1 is not None and path2 is not None
    _attempt(
        queue,
        spec_id="demo",
        phase="verify",
        decision="phase-complete",
        reason="outcome: SUCCEEDED",
        gates={"host_verify": "pass", "source_diff": "abstain:non_gated_phase"},
        gate_evidence=[
            {"path": str(path1.relative_to(spec)), "sha256": body1["bundle_sha256"]},
            {"path": str(path2.relative_to(spec)), "sha256": body2["bundle_sha256"]},
        ],
    )

    report = gc_mod.scan_repo(root, check_chain=True)
    expected_checked = True if gc_mod._YAML_IS_PYYAML else "partial"
    assert report["specs"][0]["chain"] == {
        "checked": expected_checked,
        "bundles": 2,
        "violations": [],
    }
    if gc_mod._YAML_IS_PYYAML:
        assert report["specs"][0]["verification"] == "host-verified"
    else:
        assert report["specs"][0]["verification"] == "unknown"
        assert [item["class"] for item in report["specs"][0]["blindness"]] == [
            "chain-hash-unverified"
        ]


def test_default_check_chain_is_on_and_demotes_a_mutated_bundle(tmp_path: Path) -> None:
    """B4 FLIP regression: scan_repo's check_chain default is True — callers that pass NO
    kwarg (e.g. planning._scan_repo) still get chain verification. A gate-evidence bundle
    mutated AFTER write (its recorded bundle_sha256 no longer matches its own captured
    content — exactly the class of corruption a blind rename/sed sweep can inflict) must
    surface a chain-violation finding and demote the spec off host-verified, even when the
    caller relies on the default rather than passing check_chain=True explicitly."""
    root = _repo(tmp_path)
    queue = root / ".builder" / "dispatch-queue"
    spec = _spec(root, "demo", "verified")
    if str(Path(__file__).resolve().parents[2] / "scripts") not in sys.path:
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
    from _dispatch_runtime import gate_evidence

    body = {"spec_id": "demo", "phase": "verify", "gate": "host_verify", "verdict": "pass"}
    bundle_path = gate_evidence.write_bundle(spec / "gate-evidence", body)
    assert bundle_path is not None
    _attempt(
        queue,
        spec_id="demo",
        phase="verify",
        decision="phase-complete",
        reason="outcome: SUCCEEDED",
        gates={"host_verify": "pass"},
        gate_evidence=[{"path": str(bundle_path.relative_to(spec)), "sha256": body["bundle_sha256"]}],
    )

    # Mutate captured bytes in-place post-write (the same class of corruption as a blind
    # rename/sed sweep touching a captured command string) WITHOUT recomputing bundle_sha256 —
    # the stored hash no longer matches the bundle's own bytes.
    raw = bundle_path.read_text(encoding="utf-8")
    mutated = raw.replace("host_verify", "host_verify_mutated", 1)
    assert mutated != raw
    bundle_path.write_text(mutated, encoding="utf-8")

    # No check_chain kwarg — exercises the DEFAULT, not an explicit opt-in.
    report = gc_mod.scan_repo(root)
    row = report["specs"][0]
    assert "chain-violation" in [f["class"] for f in row["findings"]]
    assert row["verification"] != "host-verified"


def test_no_verify_chain_flag_opts_out_of_the_default(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _spec(root, "demo", "implementing")
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = gc_mod.main(["--root", str(root), "--no-verify-chain"])
    assert code == 0


def test_split_queues_union_all_attempt_history(tmp_path: Path) -> None:
    root = _repo(tmp_path, declared=".builder/dispatch-queue.sqlite3")
    declared = root / ".builder" / "dispatch-queue.sqlite3"
    sibling = root / ".builder" / "dispatch-queue"
    _spec(root, "demo", "verified")
    _attempt(
        declared,
        spec_id="demo",
        phase="verify",
        decision="phase-complete",
        reason="outcome: SUCCEEDED",
        gates={"host_verify": "pass", "source_diff": "abstain:non_gated_phase"},
        attempt_id="clean-verify",
    )
    _attempt(
        sibling,
        spec_id="demo",
        phase="implement",
        decision="phase-complete",
        reason="outcome: SUCCEEDED",
        gates={**GP, "host_verify": "abstain:no_commands"},
        attempt_id="self-certified-implement",
    )
    # Event volume must not influence attempt discovery.
    for index in range(5):
        _write_yaml(declared / "queue" / "events" / f"event-{index}.yaml", {"event": index})

    report = _report(root)
    assert report["queue_roots"] == [str(declared.resolve()), str(sibling.resolve())]
    assert report["coverage"]["host_verify"]["claimed"] == 2
    spec = report["specs"][0]
    assert spec["verification"] == "self-reported"
    assert [finding["class"] for finding in spec["findings"]] == ["self-certified"]


def test_broken_attempts_symlink_in_one_queue_is_repo_blindness(tmp_path: Path) -> None:
    root = _repo(tmp_path, declared=".builder/dispatch-queue.sqlite3")
    declared = root / ".builder" / "dispatch-queue.sqlite3"
    sibling = root / ".builder" / "dispatch-queue"
    _spec(root, "demo", "verified")
    (declared / "queue").mkdir(parents=True)
    (declared / "queue" / "attempts").symlink_to("missing-attempts")
    _attempt(
        sibling,
        spec_id="demo",
        phase="verify",
        decision="phase-complete",
        reason="outcome: SUCCEEDED",
        gates={"host_verify": "pass", "source_diff": "abstain:non_gated_phase"},
    )

    report = _report(root)
    row = report["specs"][0]
    assert row["verification"] != "host-verified"
    assert "queue-attempts-unreadable" in [item["class"] for item in report["blindness"]]
    assert "queue-attempts-unreadable" in [item["class"] for item in row["blindness"]]


def test_duplicate_attempt_ids_dedupe_and_differences_are_blindness(tmp_path: Path) -> None:
    root = _repo(tmp_path, declared=".builder/dispatch-queue.sqlite3")
    declared = root / ".builder" / "dispatch-queue.sqlite3"
    sibling = root / ".builder" / "dispatch-queue"
    _spec(root, "demo", "verified")
    kwargs = {
        "spec_id": "demo",
        "phase": "verify",
        "decision": "phase-complete",
        "reason": "outcome: SUCCEEDED",
        "gates": {"host_verify": "pass", "source_diff": "abstain:non_gated_phase"},
        "attempt_id": "same",
    }
    _attempt(declared, **kwargs)
    _attempt(sibling, **kwargs)
    report = _report(root)
    assert report["coverage"]["host_verify"]["claimed"] == 1
    assert report["specs"][0]["verification"] == "host-verified"

    _attempt(
        sibling,
        spec_id="demo",
        phase="verify",
        decision="phase-complete",
        reason="outcome: SUCCEEDED",
        gates={"host_verify": "abstain:off", "source_diff": "abstain:non_gated_phase"},
        attempt_id="same",
    )
    report = _report(root)
    assert [item["class"] for item in report["blindness"]] == ["ambiguous-attempt"]
    assert report["specs"][0]["verification"] == "unknown"


def test_unreadable_attempt_directory_is_operational_error(tmp_path: Path) -> None:
    _skip_unreadable_permission_probe_as_root()
    root = _repo(tmp_path)
    attempts = root / ".builder" / "dispatch-queue" / "queue" / "attempts"
    attempts.mkdir(parents=True)
    attempts.chmod(0)
    try:
        assert gc_mod.main(["--root", str(root)]) == 2
    finally:
        attempts.chmod(0o700)


def test_json_attempt_records_are_audited(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    queue = root / ".builder" / "dispatch-queue"
    _spec(root, "demo", "verified")
    path = _attempt(
        queue,
        spec_id="demo",
        phase="verify",
        decision="phase-complete",
        reason="outcome: SUCCEEDED",
        gates={"host_verify": "abstain:no_commands", "source_diff": "abstain:non_gated_phase"},
        attempt_id="odd",
    )
    path.rename(path.with_suffix(".json"))

    spec = _report(root)["specs"][0]
    assert spec["accepted_turns"] == 1
    assert spec["verification"] == "self-reported"
    assert [finding["class"] for finding in spec["findings"]] == ["self-certified"]


def test_unknown_attempt_extension_is_blindness(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    queue = root / ".builder" / "dispatch-queue"
    _spec(root, "demo", "verified")
    _attempt(
        queue,
        spec_id="demo",
        phase="verify",
        decision="phase-complete",
        reason="outcome: SUCCEEDED",
        gates={"host_verify": "pass", "source_diff": "abstain:non_gated_phase"},
    )
    unknown = queue / "queue" / "attempts" / "historical.record"
    unknown.write_text('{"metadata": {"spec_id": "demo"}}\n', encoding="utf-8")
    (unknown.parent / "ignored.log").write_text("noise\n", encoding="utf-8")
    (unknown.parent / "ignored.tmp").write_text("noise\n", encoding="utf-8")

    report = _report(root)
    assert [item["class"] for item in report["blindness"]] == ["unknown-attempt-extension"]
    assert report["specs"][0]["verification"] == "unknown"
    assert "BLINDNESS" in gc_mod.render_human(report)


def test_unparseable_attempt_is_blindness(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    queue = root / ".builder" / "dispatch-queue"
    _spec(root, "demo", "verified")
    _attempt(
        queue,
        spec_id="demo",
        phase="verify",
        decision="phase-complete",
        reason="outcome: SUCCEEDED",
        gates={"host_verify": "pass", "source_diff": "abstain:non_gated_phase"},
    )
    (queue / "queue" / "attempts" / "broken.yaml").write_text("{null", encoding="utf-8")

    report = _report(root)
    assert report["diagnostics"]["unreadable_attempts"] == 1
    assert [item["class"] for item in report["blindness"]] == ["unparseable-attempt"]
    assert report["specs"][0]["verification"] == "unknown"


def test_chain_rejects_absolute_traversal_and_escaping_symlink(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    queue = root / ".builder" / "dispatch-queue"
    spec_dir = _spec(root, "demo", "verified")
    external = tmp_path / "0001-host_verify-verify.yaml"
    external.write_text('{"bundle_sha256": "forged"}\n', encoding="utf-8")
    evidence_dir = spec_dir / "gate-evidence"
    evidence_dir.mkdir()
    (evidence_dir / "0003-host_verify-verify.yaml").symlink_to(external)
    _attempt(
        queue,
        spec_id="demo",
        phase="verify",
        decision="phase-complete",
        reason="outcome: SUCCEEDED",
        gates={"host_verify": "pass", "source_diff": "abstain:non_gated_phase"},
        gate_evidence=[
            {"path": str(external), "sha256": "forged"},
            {"path": "gate-evidence/../0002-host_verify-verify.yaml", "sha256": "forged"},
            {"path": "gate-evidence/0003-host_verify-verify.yaml", "sha256": "forged"},
        ],
    )

    row = gc_mod.scan_repo(root, check_chain=True)["specs"][0]
    violations = [finding for finding in row["findings"] if finding["class"] == "chain-violation"]
    assert len(violations) >= 3
    details = " ".join(finding["detail"] for finding in violations)
    assert "absolute path is forbidden" in details
    assert "parent traversal is forbidden" in details
    assert "path escapes gate-evidence" in details
    assert row["verification"] == "unknown"


def test_gate_evidence_file_is_chain_violation_and_blindness(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    queue = root / ".builder" / "dispatch-queue"
    spec_dir = _spec(root, "demo", "verified")
    (spec_dir / "gate-evidence").write_text("not a directory\n", encoding="utf-8")
    _attempt(
        queue,
        spec_id="demo",
        phase="verify",
        decision="phase-complete",
        reason="outcome: SUCCEEDED",
        gates={"host_verify": "pass", "source_diff": "abstain:non_gated_phase"},
    )

    row = gc_mod.scan_repo(root, check_chain=True)["specs"][0]
    assert row["verification"] != "host-verified"
    assert "chain-violation" in [finding["class"] for finding in row["findings"]]
    assert "gate-evidence-unreadable" in [item["class"] for item in row["blindness"]]


def test_symlinked_spec_directory_is_blindness(tmp_path: Path) -> None:
    root = _repo(tmp_path / "repo")
    queue = root / ".builder" / "dispatch-queue"
    external = tmp_path / "external" / "demo"
    _write_yaml(external / "spec.yaml", {"status": "verified"})
    specs_root = root / ".builder" / "specs"
    specs_root.mkdir()
    (specs_root / "demo").symlink_to(external, target_is_directory=True)
    _attempt(
        queue,
        spec_id="demo",
        phase="verify",
        decision="phase-complete",
        reason="outcome: SUCCEEDED",
        gates={"host_verify": "pass", "source_diff": "abstain:non_gated_phase"},
    )

    report = _report(root)
    row = report["specs"][0]
    assert row["claim"] == "?", "the external claim must not be read"
    assert row["verification"] != "host-verified"
    assert "spec-directory-unreadable" in [item["class"] for item in row["blindness"]]


def test_malformed_gate_evidence_entries_are_chain_violations(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    queue = root / ".builder" / "dispatch-queue"
    _spec(root, "demo", "verified")
    _attempt(
        queue,
        spec_id="demo",
        phase="verify",
        decision="phase-complete",
        reason="outcome: SUCCEEDED",
        gates={"host_verify": "pass", "source_diff": "abstain:non_gated_phase"},
        gate_evidence=["garbage", None, {}, {"sha256": "x"}],
    )

    row = gc_mod.scan_repo(root, check_chain=True)["specs"][0]
    violations = [finding for finding in row["findings"] if finding["class"] == "chain-violation"]
    assert len(violations) == 4
    assert all("malformed gate_evidence reference" in finding["detail"] for finding in violations)
    assert gc_mod.main(["--root", str(root), "--verify-chain", "--check"]) == 1


def test_completed_claim_without_terminal_verify_surfaces_self_certified(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    queue = root / ".builder" / "dispatch-queue"
    _spec(root, "demo", "verified")
    _attempt(
        queue,
        spec_id="demo",
        phase="implement",
        decision="phase-complete",
        reason="outcome: SUCCEEDED",
        gates={**GP, "host_verify": "abstain:no_commands"},
    )

    row = _report(root)["specs"][0]
    assert row["verification"] == "unknown"
    assert [finding["class"] for finding in row["findings"]] == [
        "no-host-record", "self-certified"
    ]
    assert gc_mod.main(["--root", str(root), "--check"]) == 1


def test_casefold_spec_id_collision_does_not_create_verified_phantom(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    queue = root / ".builder" / "dispatch-queue"
    _spec(root, "demo", "verified")
    _attempt(
        queue,
        spec_id="Demo",
        phase="verify",
        decision="phase-complete",
        reason="outcome: SUCCEEDED",
        gates={"host_verify": "pass", "source_diff": "abstain:non_gated_phase"},
    )

    report = _report(root)
    assert [row["spec"] for row in report["specs"]] == ["Demo", "demo"]
    assert [row["verification"] for row in report["specs"]] == ["unknown", "unknown"]
    assert all(
        [item["class"] for item in row["blindness"]] == ["ambiguous-spec-id"]
        for row in report["specs"]
    )


def test_shim_chain_is_partial_and_never_intact(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    queue = root / ".builder" / "dispatch-queue"
    spec_dir = _spec(root, "demo", "verified")
    bundle = spec_dir / "gate-evidence" / "0001-host_verify-verify.yaml"
    _write_yaml(bundle, {"bundle_sha256": "deliberately-wrong"})
    _attempt(
        queue,
        spec_id="demo",
        phase="verify",
        decision="phase-complete",
        reason="outcome: SUCCEEDED",
        gates={"host_verify": "pass", "source_diff": "abstain:non_gated_phase"},
        gate_evidence=[{
            "path": "gate-evidence/0001-host_verify-verify.yaml",
            "sha256": "deliberately-wrong",
        }],
    )

    was_pyyaml = gc_mod._YAML_IS_PYYAML
    gc_mod._YAML_IS_PYYAML = False
    try:
        report = gc_mod.scan_repo(root, check_chain=True)
    finally:
        gc_mod._YAML_IS_PYYAML = was_pyyaml
    row = report["specs"][0]
    assert row["chain"]["checked"] == "partial"
    assert row["verification"] == "unknown"
    rendered = gc_mod.render_human(report)
    assert "cross-links checked; hash chain NOT verified (requires PyYAML)" in rendered
    assert "intact" not in rendered


def test_healthy_symlinked_attempts_dir_is_not_blindness(tmp_path: Path) -> None:
    """FALSE-ALARM REGRESSION: a queue/attempts symlinked to a real, readable directory is a
    LEGITIMATE layout. Flagging it as blindness downgraded a genuinely host-verified spec to
    unknown. An audit tool that cries wolf gets muted -- as damaging as the silence it was built
    to fix. Only a BROKEN symlink or a wrong type is blindness."""
    root = _repo(tmp_path)
    real = tmp_path / "elsewhere" / "attempts"
    real.mkdir(parents=True)
    queue = root / ".builder" / "dispatch-queue" / "queue"
    queue.mkdir(parents=True)
    (queue / "attempts").symlink_to(real)
    _spec(root, "demo", "verified")
    _attempt(
        root / ".builder" / "dispatch-queue",
        spec_id="demo",
        phase="verify",
        decision="phase-complete",
        reason="outcome: SUCCEEDED",
        gates={"host_verify": "pass", "source_diff": "abstain:non_gated_phase"},
    )

    report = _report(root)
    assert report["blindness"] == [], "a valid symlinked attempts dir must not be blindness"
    assert report["specs"][0]["verification"] == "host-verified"


def test_orphan_attempt_without_spec_id_does_not_taint_other_specs(tmp_path: Path) -> None:
    """FALSE-ALARM REGRESSION: an attempt record with no spec_id (a lane that failed BEFORE it
    bound to a spec -- e.g. a 401'd claude-lane attempt) is an unbindable pre-binding artifact that
    belongs to NO spec. It must not downgrade OTHER specs' genuinely host-verified verdict. Two such
    stubs once made the gate-coverage scanner globally blind and zeroed the entire builder roadmap's
    fulfillment (every intent read 0% despite real gate-evidence + passing verify turns on disk). It
    stays surfaced via the diagnostics counter; it is simply no longer global blindness."""
    root = _repo(tmp_path)
    _spec(root, "demo", "verified")
    _attempt(
        root / ".builder" / "dispatch-queue",
        spec_id="demo",
        phase="verify",
        decision="phase-complete",
        reason="outcome: SUCCEEDED",
        gates={"host_verify": "pass", "source_diff": "abstain:non_gated_phase"},
    )
    # A parseable orphan: work_id/lane/metadata present, but no spec_id (work never bound a spec).
    _write_yaml(
        root / ".builder" / "dispatch-queue" / "queue" / "attempts" / "attempt-orphan.yaml",
        {"attempt_id": "attempt-orphan", "metadata": {"lane": "claude"}, "created_at": "2026-07-21T00:00:00Z"},
    )

    report = _report(root)
    assert report["specs"][0]["verification"] == "host-verified", report["specs"][0]
    # Still surfaced -- not silently dropped -- just not tainting the spec.
    assert report["diagnostics"]["attempts_without_spec_id"] == 1
    assert not any(
        b["class"] == "attempt-without-spec-id" and "demo" in b.get("specs", [])
        for b in report["specs"][0]["blindness"]
    ), "orphan blindness must not attach to a real spec"


def test_empty_gate_evidence_dir_is_nothing_to_verify(tmp_path: Path) -> None:
    """FALSE-ALARM REGRESSION: an empty gate-evidence/ with no referenced bundles is 'nothing to
    verify', not a chain violation. verify_chain() reports an empty dir as a violation (correct for
    ITS caller); here it is healthy -- evidence was simply never written for this spec. Treating it
    as a violation made `--check` exit 1 on a clean repo."""
    root = _repo(tmp_path)
    spec_dir = _spec(root, "demo", "verified")
    (spec_dir / "gate-evidence").mkdir()
    _attempt(
        root / ".builder" / "dispatch-queue",
        spec_id="demo",
        phase="verify",
        decision="phase-complete",
        reason="outcome: SUCCEEDED",
        gates={"host_verify": "pass", "source_diff": "abstain:non_gated_phase"},
    )

    report = gc_mod.scan_repo(root, check_chain=True)
    spec = report["specs"][0]
    assert spec["chain"]["violations"] == []
    assert spec["verification"] == "host-verified"
    assert gc_mod.main(["--root", str(root), "--verify-chain", "--check"]) == 0


def test_resolve_queue_root_helper_accepts_symlinked_attempts(tmp_path: Path) -> None:
    """FALSE-ALARM REGRESSION: the resolve_queue_root() compatibility helper probed queue/attempts
    WITHOUT allow_symlink, so a healthy symlinked attempts dir raised OperationalError here while the
    scanner accepted it. Every probe of the same artifact must classify it the same way."""
    root = _repo(tmp_path)
    real = tmp_path / "elsewhere" / "attempts"
    real.mkdir(parents=True)
    queue_root = root / ".builder" / "dispatch-queue"
    (queue_root / "queue").mkdir(parents=True)
    (queue_root / "queue" / "attempts").symlink_to(real)
    _attempt(
        queue_root,
        spec_id="demo",
        phase="verify",
        decision="phase-complete",
        reason="outcome: SUCCEEDED",
        gates={"host_verify": "pass", "source_diff": "abstain:non_gated_phase"},
    )

    resolved, _source, _n = gc_mod.resolve_queue_root(root, None)
    assert resolved == queue_root.resolve()
