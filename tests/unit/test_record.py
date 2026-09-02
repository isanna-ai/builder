"""Tests for scripts/record.py."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "record.py"
SCRIPTS = _SCRIPT_PATH.parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


def _load_module():
    spec = importlib.util.spec_from_file_location("record", _SCRIPT_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


record_mod = _load_module()

GP = {
    "host_verify": "pass",
    "source_diff": "pass",
    "red_baseline": "abstain:non_gated_phase",
    "packet_contract": "abstain:non_gated_phase",
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
    lane="codex",
    metadata_extra=None,
) -> Path:
    attempt_id = attempt_id or f"attempt-{len(list((Path(queue_root) / 'queue' / 'attempts').glob('*.yaml'))) + 1}"
    metadata = {
        "spec_id": spec_id,
        "phase": phase,
        "decision": decision,
        "reason": reason,
        "started_at": created_at,
        "lane": lane,
    }
    if gates is not None:
        metadata["gates"] = gates
    if gate_evidence is not None:
        metadata["gate_evidence"] = gate_evidence
    if metadata_extra:
        metadata.update(metadata_extra)
    return _write_yaml(
        Path(queue_root) / "queue" / "attempts" / f"{attempt_id}.yaml",
        {"attempt_id": attempt_id, "metadata": metadata, "created_at": created_at},
    )


def _spec(root, spec_id, status="specified", extra=None) -> Path:
    spec = Path(root) / ".builder" / "specs" / spec_id
    data = {
        "name": spec_id,
        "status": status,
        "current_phase": "6-verify",
        "next_action": "Run the next Builder phase.",
        "lane": "codex",
    }
    if extra:
        data.update(extra)
    _write_yaml(spec / "spec.yaml", data)
    return spec


def _intent(root: Path, intent_id: str, specs: list[str]) -> Path:
    return _write_yaml(root / ".builder" / "intents" / intent_id / "intent.yaml", {
        "artifact": "intent-object",
        "intent": intent_id,
        "title": intent_id,
        "status": "accepted",
        "problem": "p",
        "why": "w",
        "success_criteria": [{"id": "sc-1", "statement": "s"}],
        "non_goals": ["n"],
        "ssot_delta": {"capabilities": [], "behaviors": [], "journeys": []},
        "specs": specs,
    })


def _build(root: Path, out: Path) -> Path:
    code = record_mod.main(["build", "--root", str(root), "--out", str(out)])
    assert code == 0
    return out / root.name


def _export(root: Path, spec_id: str, out: Path) -> Path:
    code = record_mod.main(["export", spec_id, "--root", str(root), "--out", str(out)])
    assert code == 0
    return out


def _spec_page(out: Path, root: Path, spec_id: str) -> str:
    return (out / root.name / "spec" / f"{spec_id}.html").read_text(encoding="utf-8")


def _bundle(spec: Path, name="0001-host_verify-verify.yaml", extra=None) -> Path:
    data = {
        "seq": 1,
        "gate": "host_verify",
        "spec_id": spec.name,
        "phase": "verify",
        "task_id": "T1",
        "attempt_id": "attempt-1",
        "command": ["python3", "-m", "pytest", "tests/unit/test_demo.py"],
        "cwd": "/repo",
        "env_fingerprint": ["PYTHONPATH"],
        "exit_code": 0,
        "started_at": "2026-07-13T00:00:00Z",
        "finished_at": "2026-07-13T00:00:01Z",
        "duration_ms": 1000,
        "stdout_tail": "ok",
        "stderr_tail": "",
        "stdout_bytes_total": 2,
        "stderr_bytes_total": 0,
        "truncated": False,
        "git_head_sha": "abc123",
        "diff_stat": {"files_changed": 1, "insertions": 2, "deletions": 0, "files": ["demo.py"]},
        "diff_patch_tail": "",
        "verdict": "pass",
        "failure_reason": "",
        "host": {"hostname": "host", "dispatcher_version": "dev"},
        "prev_bundle_sha256": "",
        "bundle_sha256": "",
    }
    if extra:
        data.update(extra)
    if not extra or "bundle_sha256" not in extra:
        data["bundle_sha256"] = record_mod._bundle_sha(data)
    return _write_yaml(spec / "gate-evidence" / name, data)


def _bundle_ref(bundle: Path, *, sha256=None) -> dict:
    data = record_mod._load_yaml(bundle)
    assert isinstance(data, dict)
    return {
        "path": f"gate-evidence/{bundle.name}",
        "sha256": sha256 if sha256 is not None else record_mod._bundle_sha(data),
    }


def _requirements(spec: Path, criteria: list[dict]) -> None:
    _write_yaml(spec / "requirements.yaml", {"requirements": [{"id": "R1", "acceptance": criteria}]})


def _tasks(spec: Path, tasks: list[dict]) -> None:
    _write_yaml(spec / "tasks.yaml", {"tasks": tasks})


def _trace(spec: Path, task_ids: list[str]) -> None:
    _write_yaml(
        spec / "traceability.yaml",
        {"task_links": [{"task_id": task_id, "evidence_ids": [f"E-{task_id}"]} for task_id in task_ids]},
    )


def _evidence(spec: Path, task_id: str, *, step="GREEN") -> None:
    _write_yaml(
        spec / "evidence" / f"task-{task_id}.yaml",
        {
            "task_id": task_id,
            "entries": [{"id": step, "step": step, "command": "pytest tests/unit/test_demo.py", "exit_code": 0}],
        },
    )


def _run_cli_for_exit(argv) -> tuple[object, str, str]:
    stdout = StringIO()
    stderr = StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        try:
            record_mod.main(argv)
        except SystemExit as exc:
            return exc.code, stdout.getvalue(), stderr.getvalue()
    raise AssertionError("CLI did not exit through argparse")


def test_version_flag_prints_env_override() -> None:
    with patch.dict(os.environ, {"BUILDER_DISPATCHER_VERSION": "test-version"}):
        code, stdout, stderr = _run_cli_for_exit(["--version"])

    assert code == 0
    assert stdout == "record.py test-version\n"
    assert stderr == ""


def test_version_flag_unknown_fallback() -> None:
    with patch.object(record_mod, "_load_dispatcher_version", return_value=lambda: "  "):
        empty_code, empty_stdout, empty_stderr = _run_cli_for_exit(["--version"])
    assert empty_code == 0
    assert empty_stdout == "record.py unknown\n"
    assert empty_stderr == ""

    def fail_to_load():
        raise ImportError("unavailable")

    with patch.object(record_mod, "_load_dispatcher_version", side_effect=fail_to_load):
        load_code, load_stdout, load_stderr = _run_cli_for_exit(["--version"])
    assert load_code == 0
    assert load_stdout == "record.py unknown\n"
    assert load_stderr == ""


def test_version_flag_missing_subcommand_still_errors() -> None:
    code, stdout, stderr = _run_cli_for_exit([])

    assert code != 0
    assert stdout == ""
    assert "the following arguments are required: cmd" in stderr


def test_version_flag_leaves_subcommand_dispatch_unchanged() -> None:
    calls = []

    def fake_build(args):
        calls.append(("build", args.cmd))
        return 11

    def fake_export(args):
        calls.append(("export", args.cmd, args.spec_id))
        return 12

    with patch.object(record_mod, "build", side_effect=fake_build), patch.object(
        record_mod, "export", side_effect=fake_export
    ):
        assert record_mod.main(["build"]) == 11
        assert record_mod.main(["export", "demo-spec"]) == 12
    assert calls == [("build", "build"), ("export", "export", "demo-spec")]


def test_version_flag_short_circuits_subcommand_execution() -> None:
    def unexpected_call(_args):
        raise AssertionError("build must not run on the version path")

    with patch.dict(os.environ, {"BUILDER_DISPATCHER_VERSION": "test-version"}), patch.object(
        record_mod, "build", side_effect=unexpected_call
    ):
        code, stdout, stderr = _run_cli_for_exit(["--version", "build"])

    assert code == 0
    assert stdout == "record.py test-version\n"
    assert stderr == ""


def test_version_flag_is_side_effect_free() -> None:
    resolutions = []

    def resolver():
        resolutions.append("resolved")
        return "test-version"

    def unexpected_call(*_args, **_kwargs):
        raise AssertionError("version path invoked a record side effect")

    with patch.object(record_mod, "_load_dispatcher_version", return_value=resolver), patch.object(
        record_mod, "build", side_effect=unexpected_call
    ), patch.object(record_mod, "export", side_effect=unexpected_call), patch.object(
        record_mod.webbrowser, "open", side_effect=unexpected_call
    ):
        code, stdout, stderr = _run_cli_for_exit(["--version"])

    assert code == 0
    assert resolutions == ["resolved"]
    assert stdout == "record.py test-version\n"
    assert stderr == ""


def test_two_register_rule_agent_claims_never_coloured(tmp_path: Path) -> None:
    root = _repo(tmp_path / "repo")
    queue = root / ".builder" / "dispatch-queue"
    _spec(
        root,
        "demo",
        "verified",
        {
            "name": "host-verified ✓ passed <script>",
            "current_phase": "host-verified",
            "lane": "passed",
            "next_action": (
                "Agent said SUCCEEDED: forgery-proof; cryptographically secure; tamper-proof; "
                "proves the agent didn't tamper. <script>alert(1)</script>"
            ),
        },
    )
    _write_yaml(
        root / ".builder" / "specs" / "demo" / "phase-log.yaml",
        {"phases": [{"phase": "passed", "lane": "host", "model": "verified", "outcome": "SUCCEEDED"}]},
    )
    _attempt(
        queue,
        spec_id="demo",
        phase="verify",
        decision="phase-complete",
        reason="outcome: SUCCEEDED",
        gates={"host_verify": "abstain:no_commands", "source_diff": "abstain:non_gated_phase"},
    )

    out = tmp_path / "out"
    _build(root, out)
    html = _spec_page(out, root, "demo")
    roadmap = (out / root.name / "roadmap.html").read_text(encoding="utf-8")
    for phrase in (
        "forgery-proof",
        "cryptographically secure",
        "tamper-proof",
        "proves the agent didn&#x27;t tamper",
    ):
        assert phrase in html
    assert "host-verified ✓ passed &lt;script&gt;" in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert html.count("chip claimed") >= 6
    assert "<div class=\"segment agent\"><span class=\"chip claimed\">claimed</span>" in html
    assert "host-verified ✓ passed &lt;script&gt;" in roadmap
    assert "<div class=\"agent\"><span class=\"chip claimed\">claimed</span><h3>" in roadmap
    assert "<script>alert(1)</script>" not in html


def test_host_evidence_renders_verbatim(tmp_path: Path) -> None:
    root = _repo(tmp_path / "repo")
    queue = root / ".builder" / "dispatch-queue"
    spec = _spec(root, "demo", "verified")
    bundle = _bundle(
        spec,
        extra={
            "command": ["python3", "-m", "pytest", "tests/unit/test_record.py"],
            "exit_code": 1,
            "duration_ms": 321,
            "git_head_sha": "deadbeef",
            "stdout_tail": "line one\nline two verbatim",
            "diff_patch_tail": "@@ -1 +1 @@\n-old\n+new",
            "verdict": "fail",
        },
    )
    _attempt(
        queue,
        spec_id="demo",
        phase="verify",
        decision="phase-complete",
        reason="outcome: SUCCEEDED",
        gates=GP,
        gate_evidence=[_bundle_ref(bundle)],
    )

    out = tmp_path / "out"
    _build(root, out)
    html = _spec_page(out, root, "demo")
    assert "[&quot;python3&quot;, &quot;-m&quot;, &quot;pytest&quot;, &quot;tests/unit/test_record.py&quot;]" in html
    assert "exit_code: 1" in html
    assert "duration_ms: 321" in html
    assert "git_head_sha: deadbeef" in html
    assert "gate: host_verify" in html
    assert "verdict: fail" in html
    assert "polarity: unknown" in html
    assert "mode: unknown" in html
    assert "blocking: unknown" in html
    assert "failure_class: —" in html
    assert "line one\nline two verbatim" in html
    assert "@@ -1 +1 @@\n-old\n+new" in html
    assert '<section class="host-seal host-bad">' in html


def test_unreferenced_agent_bundle_is_never_host_evidence(tmp_path: Path) -> None:
    root = _repo(tmp_path / "repo")
    spec = _spec(root, "demo", "verified")
    _bundle(spec, extra={"stdout_tail": "agent says green", "verdict": "pass", "exit_code": 0})

    out = tmp_path / "out"
    _build(root, out)
    html = _spec_page(out, root, "demo")
    assert "unauthenticated — not referenced by any host record" in html
    assert "unreferenced bundle" in html
    assert "agent says green" not in html
    assert "HOST-EXECUTED" not in html
    assert '<span class="stamp">unknown</span>' in html


def test_absolute_bundle_reference_is_not_read(tmp_path: Path) -> None:
    root = _repo(tmp_path / "repo")
    queue = root / ".builder" / "dispatch-queue"
    _spec(root, "demo", "verified")
    outside = _write_yaml(
        tmp_path / "outside.yaml",
        {"spec_id": "demo", "attempt_id": "attempt-1", "verdict": "pass", "exit_code": 0, "stdout_tail": "OUTSIDE SECRET"},
    )
    data = record_mod._load_yaml(outside)
    _attempt(
        queue,
        spec_id="demo",
        phase="verify",
        decision="phase-complete",
        gates=GP,
        gate_evidence=[{"path": str(outside), "sha256": record_mod._bundle_sha(data)}],
    )

    out = tmp_path / "out"
    _build(root, out)
    html = _spec_page(out, root, "demo")
    assert "absolute paths are forbidden" in html
    assert "OUTSIDE SECRET" not in html
    assert "HOST-EXECUTED" not in html
    assert '<span class="stamp">unknown</span>' in html


def test_sha_mismatch_is_unauthenticated(tmp_path: Path) -> None:
    root = _repo(tmp_path / "repo")
    queue = root / ".builder" / "dispatch-queue"
    spec = _spec(root, "demo", "verified")
    bundle = _bundle(spec, extra={"verdict": "pass", "exit_code": 0})
    _attempt(
        queue,
        spec_id="demo",
        phase="verify",
        decision="phase-complete",
        gates=GP,
        gate_evidence=[_bundle_ref(bundle, sha256="0" * 64)],
    )

    out = tmp_path / "out"
    _build(root, out)
    html = _spec_page(out, root, "demo")
    assert "sha256 mismatch" in html
    assert "HOST-EXECUTED" not in html
    assert '<span class="stamp">unknown</span>' in html


def test_cross_spec_bundle_replay_does_not_authenticate(tmp_path: Path) -> None:
    root = _repo(tmp_path / "repo")
    queue = root / ".builder" / "dispatch-queue"
    spec_a = _spec(root, "spec-a", "verified")
    spec_b = _spec(root, "spec-b", "verified")
    original = _bundle(spec_a, extra={"spec_id": "spec-a", "verdict": "pass", "exit_code": 0})
    copied = spec_b / "gate-evidence" / original.name
    copied.parent.mkdir(parents=True)
    shutil.copyfile(original, copied)
    _attempt(
        queue,
        spec_id="spec-b",
        phase="verify",
        decision="phase-complete",
        gates=GP,
        gate_evidence=[_bundle_ref(copied)],
    )

    out = tmp_path / "out"
    _build(root, out)
    html = _spec_page(out, root, "spec-b")
    assert "bundle names a different spec" in html
    assert "HOST-EXECUTED" not in html
    assert '<span class="stamp">unknown</span>' in html


def test_red_baseline_pass_is_green_even_when_command_fails(tmp_path: Path) -> None:
    root = _repo(tmp_path / "repo")
    queue = root / ".builder" / "dispatch-queue"
    spec = _spec(root, "demo", "verified")
    bundle = _bundle(
        spec,
        name="0001-red_baseline-implement.yaml",
        extra={
            "gate": "red_baseline",
            "polarity": "red",
            "mode": "enforce",
            "blocking": False,
            "failure_class": None,
            "verdict": "pass",
            "exit_code": 1,
        },
    )
    gates = dict(GP)
    gates["red_baseline"] = "pass"
    _attempt(
        queue,
        spec_id="demo",
        phase="implement",
        decision="phase-complete",
        gates=gates,
        gate_evidence=[_bundle_ref(bundle)],
    )

    out = tmp_path / "out"
    _build(root, out)
    html = _spec_page(out, root, "demo")
    assert '<section class="host-seal host-ok">' in html
    assert "gate: red_baseline" in html
    assert "verdict: pass" in html
    assert "polarity: red" in html
    assert "mode: enforce" in html
    assert "blocking: False" in html
    assert "exit_code: 1" in html
    assert "the command is EXPECTED to fail; failing is the pass condition" in html


def test_rejected_turn_is_shown_not_hidden(tmp_path: Path) -> None:
    root = _repo(tmp_path / "repo")
    queue = root / ".builder" / "dispatch-queue"
    _spec(root, "demo", "verified")
    bad = dict(GP)
    bad["host_verify"] = "fail:assertion_failure"
    _attempt(queue, spec_id="demo", phase="verify", decision="phase-complete", reason="outcome: SUCCEEDED", gates=bad)

    out = tmp_path / "out"
    _build(root, out)
    html = _spec_page(out, root, "demo")
    assert "fail:assertion_failure" in html
    assert "rejected" in html


def test_unknown_spec_never_rendered_verified(tmp_path: Path) -> None:
    root = _repo(tmp_path / "repo")
    queue = root / ".builder" / "dispatch-queue"
    _spec(root, "demo", "verified", {"name": "host-verified ✓ passed"})
    _attempt(queue, spec_id="demo", phase="verify", decision="phase-complete", reason="outcome: SUCCEEDED")

    out = tmp_path / "out"
    _build(root, out)
    html = _spec_page(out, root, "demo")
    assert '<span class="stamp">unknown</span>' in html
    assert "host-verified ✓ passed" in html
    assert '<span class="stamp">host-verified</span>' not in html


def test_integrity_fraction_strict(tmp_path: Path) -> None:
    root = _repo(tmp_path / "repo")
    queue = root / ".builder" / "dispatch-queue"
    spec = _spec(root, "demo", "verified")
    _requirements(
        spec,
        [
            {"id": "AC1", "priority": "must", "oracle": {"type": "automated_test"}},
            {"id": "AC2", "priority": "must", "oracle": {"type": "automated_test"}},
            {"id": "AC3", "priority": "should", "oracle": {"type": "human_only"}},
        ],
    )
    _tasks(
        spec,
        [
            {"id": "T1", "verify": [{"command": "pytest tests/unit/test_demo.py", "proves": ["AC1"]}]},
            {"id": "T2", "verify": [{"command": "pytest tests/unit/test_other.py", "proves": ["AC2"]}]},
        ],
    )
    _trace(spec, ["T1", "T2"])
    _evidence(spec, "T2")  # Agent-written evidence cannot increase the fraction.
    bundle = _bundle(
        spec,
        extra={
            "command": ["/bin/sh", "-c", "pytest tests/unit/test_demo.py"],
            "task_id": "T1",
            "task_ids": ["T1"],
            "exit_code": 0,
            "verdict": "pass",
        },
    )
    _attempt(queue, spec_id="demo", phase="verify", decision="phase-complete", gates=GP, gate_evidence=[_bundle_ref(bundle)])

    out = tmp_path / "out"
    _build(root, out)
    html = _spec_page(out, root, "demo")
    assert "1/2" in html
    assert "must-criteria host-anchored +1 human-only" in html
    assert "machine-proven" not in html
    assert '<div class="agent"><span class="chip claimed">claimed</span> <strong>integrity</strong>' in html


def test_integrity_agent_evidence_alone_never_proves(tmp_path: Path) -> None:
    root = _repo(tmp_path / "repo")
    spec = _spec(root, "demo", "verified")
    _requirements(spec, [{"id": "AC1", "priority": "must", "oracle": {"type": "automated_test"}}])
    _tasks(spec, [{"id": "T1", "verify": [{"command": "pytest demo.py", "proves": ["AC1"]}]}])
    _trace(spec, ["T1"])
    _evidence(spec, "T1")

    out = tmp_path / "out"
    _build(root, out)
    html = _spec_page(out, root, "demo")
    assert "0/1 must-criteria claimed; no authenticated host command records" in html
    assert "machine-proven" not in html


def test_no_structured_acs_renders_dash_not_zero(tmp_path: Path) -> None:
    root = _repo(tmp_path / "repo")
    _spec(root, "demo", "verified")

    out = tmp_path / "out"
    _build(root, out)
    html = _spec_page(out, root, "demo")
    assert "<strong>integrity</strong>: — no structured acceptance criteria" in html
    assert "0/0 must-criteria" not in html


def test_roadmap_columns_and_required_edges(tmp_path: Path) -> None:
    root = _repo(tmp_path / "repo")
    _spec(root, "alpha", "planned")
    _spec(root, "beta", "verified")
    _write_yaml(
        root / ".builder" / "dependencies.yaml",
        {
            "dependencies": [
                {"from": "beta", "to": "alpha", "type": "required"},
                {"from": "alpha", "to": "beta", "type": "optional"},
            ]
        },
    )

    out = tmp_path / "out"
    _build(root, out)
    html = (out / root.name / "roadmap.html").read_text(encoding="utf-8")
    assert "Ready" in html
    assert "Verified" in html
    assert "data-from=\"beta\" data-to=\"alpha\"" in html
    assert "edge-required" in html
    assert "data-kind=\"optional\"" in html
    assert "edge-required\" x1=\"80\" y1=\"51\" x2=\"290\" y2=\"51\" data-from=\"alpha\"" not in html


def test_blocked_dep_is_loud(tmp_path: Path) -> None:
    root = _repo(tmp_path / "repo")
    queue = root / ".builder" / "dispatch-queue"
    _spec(root, "demo", "planned")
    _write_yaml(
        queue / "queue" / "items" / "work-1.yaml",
        {
            "id": "work-1",
            "state": "BLOCKED_DEP",
            "task_ref": {"spec_id": "demo"},
            "unmet_deps": ["platform-identity"],
        },
    )

    out = tmp_path / "out"
    _build(root, out)
    html = (out / root.name / "roadmap.html").read_text(encoding="utf-8")
    assert "blocked-dep" in html
    assert "BLOCKED_DEP" in html
    assert "platform-identity" in html


def test_backlog_capability_panel_never_uses_the_host_register() -> None:
    owner = record_mod.planning.BacklogCapabilityOwner(
        target="capability.search",
        change="rewire",
        intent_id="search-intent",
        release_id="r1",
        visible_state="accepted",
        intent_path=".builder/intents/search-intent/intent.yaml",
    )
    owners = record_mod.planning.BacklogCapabilityOwners(
        rows=(owner,), collision_intent_ids=("search-intent",)
    )

    html = record_mod._backlog_capability_panel({"capability.search": owners}, [])

    assert "Active backlog · claimed register" in html
    assert '<span class="chip claimed">claimed</span>' in html
    assert "host-seal" not in html
    assert "badge--host" not in html



def test_export_is_self_contained(tmp_path: Path) -> None:
    root = _repo(tmp_path / "repo")
    _spec(root, "demo", "verified")

    html_path = _export(root, "demo", tmp_path / "export.html")
    html = html_path.read_text(encoding="utf-8")
    assert "http://" not in html
    assert "https://" not in html
    assert "<script src" not in html
    assert "<link rel=\"stylesheet\"" not in html
    assert "<style>" in html


def test_export_redacts_untrusted_secrets(tmp_path: Path) -> None:
    root = _repo(tmp_path / "repo")
    queue = root / ".builder" / "dispatch-queue"
    token = "ghp_abcdefghijklmnopqrstuvwxyz123456"  # publish-ok: deliberate redaction fixture
    spec = _spec(root, "demo", "verified", {"next_action": f"use {token}"})
    bundle = _bundle(
        spec,
        extra={
            "command": ["/bin/sh", "-c", f"echo {token}"],
            "cwd": f"/secret/{token}",
            "stdout_tail": "Bearer abcdefghijklmnopqrstuvwxyz123456",
            "stderr_tail": f"password={token}",
            "diff_patch_tail": f"+token={token}",
            "verdict": "fail",
            "exit_code": 1,
        },
    )
    _attempt(queue, spec_id="demo", phase="verify", decision="rejected", gates=GP, gate_evidence=[_bundle_ref(bundle)])

    html_path = _export(root, "demo", tmp_path / "export.html")
    html = html_path.read_text(encoding="utf-8")
    assert token not in html
    assert "abcdefghijklmnopqrstuvwxyz123456" not in html
    assert html.count("[redacted]") >= 4


def test_malformed_parseable_records_degrade_without_traceback(tmp_path: Path) -> None:
    root = _repo(tmp_path / "repo")
    queue = root / ".builder" / "dispatch-queue"
    spec = _spec(root, "demo", "verified", {"next_action": "tamper-proof <script>not code</script>"})
    _write_yaml(
        queue / "queue" / "attempts" / "attempt-null.yaml",
        {"attempt_id": "attempt-null", "metadata": None, "created_at": "2026-07-13T00:00:00Z"},
    )
    bundle = _bundle(
        spec,
        extra={"attempt_id": "attempt-2", "exit_code": "abc", "verdict": "surprising", "stdout_tail": "still visible"},
    )
    _attempt(
        queue,
        attempt_id="attempt-2",
        spec_id="demo",
        phase="verify",
        decision="phase-complete",
        gates=GP,
        gate_evidence=[_bundle_ref(bundle)],
    )

    out = tmp_path / "out"
    _build(root, out)
    html = _spec_page(out, root, "demo")
    assert "tamper-proof &lt;script&gt;not code&lt;/script&gt;" in html
    assert "exit_code: unknown" in html
    assert "verdict: unknown" in html
    assert '<section class="host-seal host-unknown">' in html


def test_operational_failure_exits_2_without_traceback(tmp_path: Path) -> None:
    root = _repo(tmp_path / "repo")

    def fail(*_args, **_kwargs):
        raise RuntimeError("controlled failure")

    stdout = StringIO()
    stderr = StringIO()
    with patch.object(record_mod, "_build_repo", side_effect=fail), redirect_stdout(stdout), redirect_stderr(stderr):
        code = record_mod.main(["build", "--root", str(root), "--out", str(tmp_path / "out")])
    assert code == 2
    assert "controlled failure" in stderr.getvalue()
    assert "Traceback" not in stderr.getvalue()


def test_banned_strings_absent(tmp_path: Path) -> None:
    root = _repo(tmp_path / "repo")
    queue = root / ".builder" / "dispatch-queue"
    spec = _spec(root, "demo", "verified")
    bundle = _bundle(spec)
    _attempt(queue, spec_id="demo", phase="verify", decision="phase-complete", gates=GP, gate_evidence=[_bundle_ref(bundle)])

    out = tmp_path / "out"
    _build(root, out)
    pages = list(out.rglob("*.html"))
    assert pages
    all_html = "\n".join(path.read_text(encoding="utf-8") for path in pages)
    for token in ("forgery-proof", "cryptographically secure", "tamper-proof"):
        assert token not in all_html
    assert "tamper-evident" in all_html


def test_ab_bench_dirs_excluded(tmp_path: Path) -> None:
    root = _repo(tmp_path / "repo")
    _spec(root, "ab-bench-noise", "verified")
    _spec(root, "hidden-spec", "verified")
    _spec(root, "visible-spec", "verified")
    (root / ".recordignore").write_text("hidden-*\n", encoding="utf-8")

    out = tmp_path / "out"
    _build(root, out)
    all_html = "\n".join(path.read_text(encoding="utf-8") for path in out.rglob("*.html"))
    assert "visible-spec" in all_html
    assert "ab-bench-noise" not in all_html
    assert "hidden-spec" not in all_html


def test_never_writes_outside_out(tmp_path: Path) -> None:
    root = _repo(tmp_path / "repo")
    _spec(root, "demo", "verified")
    before = sorted(str(p.relative_to(root)) for p in root.rglob("*"))

    out = tmp_path / "record-out"
    _build(root, out)
    after = sorted(str(p.relative_to(root)) for p in root.rglob("*"))
    assert before == after


def test_fresh_subprocess_import_writes_no_bytecode_to_copied_tree(tmp_path: Path) -> None:
    copied = tmp_path / "copied"
    source_root = _SCRIPT_PATH.parents[1]
    shutil.copytree(
        source_root / "scripts",
        copied / "scripts",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    root = _repo(tmp_path / "fixture-repo")
    _spec(root, "demo", "verified")
    before = sorted(str(path.relative_to(copied)) for path in copied.rglob("*"))
    env = dict(os.environ)
    env.pop("PYTHONDONTWRITEBYTECODE", None)
    env["PYTHONPATH"] = os.pathsep.join([str(copied / "scripts"), str(copied)])
    result = subprocess.run(
        [
            sys.executable,
            str(copied / "scripts" / "record.py"),
            "build",
            "--root",
            str(root),
            "--out",
            str(tmp_path / "fresh-out"),
        ],
        cwd=copied,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    after = sorted(str(path.relative_to(copied)) for path in copied.rglob("*"))
    assert result.returncode == 0, result.stderr
    assert before == after
    assert not list(copied.rglob("*.pyc"))


def test_unstamped_not_in_flight_spec_counts_unknown_in_fleet(tmp_path: Path) -> None:
    root = _repo(tmp_path / "repo")
    _spec(root, "demo", "specified")

    out = tmp_path / "out"
    _build(root, out)
    html = (out / "index.html").read_text(encoding="utf-8")
    assert "0 / 0 / 1" in html


def test_missing_builder_exits_2(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    code = record_mod.main(["build", "--root", str(root), "--out", str(tmp_path / "out")])
    assert code == 2


def test_chain_note_present_and_never_intact_when_unverifiable(tmp_path: Path) -> None:
    root = _repo(tmp_path / "repo")
    queue = root / ".builder" / "dispatch-queue"
    spec = _spec(root, "demo", "verified")
    bundle = _bundle(spec, extra={"bundle_sha256": "wrong"})
    _attempt(queue, spec_id="demo", phase="verify", decision="phase-complete", gates=GP, gate_evidence=[_bundle_ref(bundle)])

    out = tmp_path / "out"
    _build(root, out)
    html = _spec_page(out, root, "demo")
    assert "tamper-evident" in html
    assert "intact" not in html


def test_all_build_is_project_first_and_keeps_release_roadmap(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    builder = _repo(workspace / "builder")
    _spec(builder, "record-v3", "planned")
    _write_yaml(builder / ".builder" / "product.yaml",
                {"product": "builder", "title": "Builder", "repos": [{"alias": "builder"}]})
    _intent(builder, "record-v3-work", ["record-v3"])
    _write_yaml(builder / ".builder" / "releases" / "v3.yaml", {
        "release": "v3", "product": "builder", "title": "Record v3", "status": "active",
        "intents": ["record-v3-work"],
    })
    out = tmp_path / "out"

    assert record_mod.main(["build", "--all", str(workspace), "--out", str(out)]) == 0
    fleet = (out / "index.html").read_text(encoding="utf-8")
    project = (out / "projects" / "builder.html").read_text(encoding="utf-8")
    repo = (out / "builder" / "roadmap.html").read_text(encoding="utf-8")

    assert "Workspace portfolio · by project" in fleet
    assert "Project portfolio" in fleet and "project-grid" in fleet
    assert "Project → release target → roadmap" in project
    assert "Record v3" in project and "Dependency-ordered roadmap" in project
    assert "Project → repos → specs" in project
    assert "Owned through specs by" in repo and "Builder" in repo


def test_release_refs_support_spec_level_shared_repo_ownership(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    shared = _repo(workspace / "shared-platform")
    _spec(shared, "alpha-shell", "planned")
    _spec(shared, "beta-api", "planned")
    alpha_home = _repo(workspace / "alpha-home")
    beta_home = _repo(workspace / "beta-home")
    _write_yaml(alpha_home / ".builder" / "product.yaml", {
        "product": "alpha", "title": "Alpha",
        "repos": [{"alias": "alpha-home"}, {"alias": "shared-platform"}],
    })
    _write_yaml(beta_home / ".builder" / "product.yaml", {
        "product": "beta", "title": "Beta", "repos": [{"alias": "beta-home"}],
    })
    _intent(alpha_home, "alpha-release-work", ["shared-platform/alpha-shell"])
    _intent(beta_home, "beta-release-work", ["shared-platform/beta-api"])
    _write_yaml(alpha_home / ".builder" / "releases" / "alpha-r1.yaml", {
        "release": "alpha-r1", "product": "alpha", "title": "Alpha R1", "status": "active",
        "intents": ["alpha-release-work"],
    })
    _write_yaml(beta_home / ".builder" / "releases" / "beta-r1.yaml", {
        "release": "beta-r1", "product": "beta", "title": "Beta R1", "status": "active",
        "intents": ["beta-release-work"],
    })
    out = tmp_path / "out"

    assert record_mod.main(["build", "--all", str(workspace), "--out", str(out)]) == 0
    alpha = (out / "projects" / "alpha.html").read_text(encoding="utf-8")
    beta = (out / "projects" / "beta.html").read_text(encoding="utf-8")
    repo = (out / "shared-platform" / "roadmap.html").read_text(encoding="utf-8")

    assert "shared-platform" in alpha and "alpha-shell" in alpha
    assert "shared-platform" in beta and "beta-api" in beta
    assert "Alpha" in repo and "Beta" in repo
    assert "projects/alpha.html" in repo and "projects/beta.html" in repo


def test_split_release_views_parks_archived_and_abandoned() -> None:
    class _R:
        def __init__(self, status):
            self.status = status

    views = [{"release": _R(s)} for s in ("active", "ARCHIVED", "draft", "abandoned", "")]
    active, archived = record_mod._split_release_views(views)
    assert [v["release"].status for v in active] == ["active", "draft", ""]
    assert [v["release"].status for v in archived] == ["ARCHIVED", "abandoned"]


def test_archived_release_targets_are_parked_behind_a_toggle(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    builder = _repo(workspace / "builder")
    _spec(builder, "live-spec", "planned")
    _spec(builder, "old-spec", "planned")
    _write_yaml(builder / ".builder" / "product.yaml",
                {"product": "builder", "title": "Builder", "repos": [{"alias": "builder"}]})
    _intent(builder, "live-work", ["live-spec"])
    _write_yaml(builder / ".builder" / "releases" / "live.yaml", {
        "release": "live", "product": "builder", "title": "Live Target", "status": "active",
        "intents": ["live-work"],
    })
    _write_yaml(builder / ".builder" / "releases" / "old.yaml", {
        "release": "old", "product": "builder", "title": "Parked Target", "status": "archived",
        "specs": ["old-spec"],
    })
    out = tmp_path / "out"

    assert record_mod.main(["build", "--all", str(workspace), "--out", str(out)]) == 0
    project = (out / "projects" / "builder.html").read_text(encoding="utf-8")

    assert "archived-reveal" in project
    before_toggle = project.split("archived-reveal")[0]
    assert "Live Target" in before_toggle          # active target shown directly
    assert "Parked Target" not in before_toggle    # archived hidden until revealed
    assert "Parked Target" in project              # ...but present inside the toggle


def test_build_all_skips_symlinked_repo_aliases(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    real = _repo(workspace / "realrepo")
    _spec(real, "only-spec", "planned")
    # A back-compat alias symlink (aliasrepo -> realrepo) must not render a duplicate repo.
    (workspace / "aliasrepo").symlink_to(real, target_is_directory=True)
    out = tmp_path / "out"

    assert record_mod.main(["build", "--all", str(workspace), "--out", str(out)]) == 0
    assert (out / "realrepo" / "roadmap.html").exists()
    assert not (out / "aliasrepo").exists()


def test_builder_home_operational_panels_do_not_change_provenance_registers(tmp_path: Path) -> None:
    fixture = Path(__file__).resolve().parents[1] / "fixtures" / "builder_project_model" / "home" / "portfolio"
    workspace = tmp_path / "portfolio"
    shutil.copytree(fixture, workspace)
    for repo_name in ("alpha-repo", "beta-repo", "shared-repo"):
        repo = workspace / repo_name
        (repo / ".git").mkdir(parents=True)
        dispatch = repo / ".builder" / "dispatch.yaml"
        dispatch.parent.mkdir(parents=True, exist_ok=True)
        dispatch.write_text('{"queue_store":{"path":".builder/dispatch-queue"}}\n', encoding="utf-8")
    state = workspace / ".builder-home" / "state"
    _write_yaml(state / "daemon.json", {
        "schema_version": 1,
        "pid": 4242,
        "heartbeat_at": "2026-07-18T00:00:00Z",
        "findings": ["ownership: alpha loud refusal"],
    })
    _write_yaml(state / "providers" / "codex-cli.json", {
        "provider": "codex-cli", "cooldown_until": "2026-07-18T00:01:00Z",
    })
    _write_yaml(state / "sessions" / "slot-a.json", {
        "slot_id": "slot-a", "provider": "codex-cli", "repo_id": "alpha-repo", "state": "active",
    })
    out = tmp_path / "out-operational"
    assert record_mod.main(["build", "--all", str(workspace), "--out", str(out)]) == 0
    index = (out / "index.html").read_text(encoding="utf-8")
    assert "Operational state" in index
    assert "Provider capacity / cooldown" in index and "slot-a" in index
    assert "ownership: alpha loud refusal" in index
    assert "not a host verdict and not part of either provenance register" in index
