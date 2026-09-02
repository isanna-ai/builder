from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from _dispatch_runtime.config import DispatchConfig, LaneConfig
from _dispatch_runtime.lane_codex_cli import CodexCliLane
from _dispatch_runtime.lane_executor import DispatchResult, DispatchResultType
from _dispatch_runtime.queue_store import QueueStore
from _dispatch_runtime.scheduler import DispatchScheduler
from _dispatch_runtime.state_model import WorkItemState


@dataclass
class CompletedProcess:
    returncode: int
    stdout: str = ""
    stderr: str = ""
    pid: int = 5150


class FakeProcessRunner:
    def __init__(self, result: CompletedProcess):
        self.result = result
        self.calls: list[tuple[list[str], dict[str, object]]] = []

    def run(self, argv: list[str], **kwargs):
        self.calls.append((argv, kwargs))
        return self.result


class RecordingExecutor:
    def __init__(self, result: DispatchResult):
        self.result = result
        self.calls: list[tuple[dict[str, object], str, dict[str, object]]] = []

    def execute(self, task_ref, lane_name: str, attempt_context):
        self.calls.append((dict(task_ref), lane_name, dict(attempt_context)))
        return self.result


def dispatch_config(tmp_path: Path) -> DispatchConfig:
    return DispatchConfig(
        queue_store_path=tmp_path,
        lanes={"codex-cli": LaneConfig(name="codex-cli", provider="codex-cli", max_concurrency=1)},
        routing_policy={"default": "ordered", "tie_break": "lane_order"},
        cooldown_policy={"default_seconds": 30},
        retry_policy={"max_attempts": 3, "initial_seconds": 5, "max_seconds": 30, "jitter_seconds": 0},
    )


def success_result() -> DispatchResult:
    return DispatchResult(result_type=DispatchResultType.SUCCESS, metadata={"pid": 7, "logs": ["queue/attempts/a.log"]})


def human_block_result() -> DispatchResult:
    return DispatchResult(result_type=DispatchResultType.HUMAN_BLOCK, metadata={"pid": 8, "logs": ["queue/attempts/human.log"]})


def builder_workspace(tmp_path: Path) -> Path:
    (tmp_path / "scripts").mkdir(parents=True)
    (tmp_path / "scripts" / "builder-runner.py").write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    run_dir = tmp_path / ".builder" / "specs" / "demo" / "runs"
    run_dir.mkdir(parents=True)
    (run_dir / "task-T7.yaml").write_text("task: demo\n", encoding="utf-8")
    return tmp_path


def _seed_complete_5_implement(spec_dir: Path) -> None:
    """A COMPLETE, valid completion workspace for the 5-implement phase, so the lane's
    artifact-gated completion check genuinely passes rather than being asserted away. Kept out of
    the shared workspace fixture on purpose: a sibling test probes spec.yaml as an INVALID runner
    ref, so these files must exist only where SUCCESS is the subject. Proof of completion is a
    phase-log entry with a completed timestamp + advancing outcome, a spec.yaml whose
    status/current_phase corroborate 5-implement -> 6-verify, a required phase artifact, and a
    ready handoff pointing at the next phase."""
    (spec_dir / "phase-log.yaml").write_text(
        'phases:\n  - phase: 5-implement\n    completed: "2026-06-10T00:00:00Z"\n    outcome: SUCCEEDED\n',
        encoding="utf-8")
    (spec_dir / "spec.yaml").write_text("status: implementing\ncurrent_phase: 6-verify\n", encoding="utf-8")
    (spec_dir / "tasks.yaml").write_text("tasks: []\n", encoding="utf-8")
    (spec_dir / "handoff.yaml").write_text("next_phase: 6-verify\nready: true\n", encoding="utf-8")


def test_builder_task_ref_drives_codex_and_resolves_runner_task_path(tmp_path: Path):
    # This test used to assert the lane shelled out to `codex exec python3 builder-runner.py
    # <taskref>`. That command shape no longer exists: CodexCliLane drives codex directly with an
    # autonomous-pipeline prompt (`codex exec ... -C <cwd> -m <model> <prompt>`) and never invokes
    # builder-runner.py. The old assertion had been RED and ungated -- it encoded a dispatch
    # architecture the production lane abandoned. Rewritten to the stable, current invariants:
    # the lane runs `codex exec` in the workspace, resolves the runner_task_ref to an absolute
    # path in metadata, and reaches SUCCESS against a complete 5-implement workspace. The exact
    # model/flags/prompt text are deliberately NOT asserted -- that is churn, not contract.
    builder_workspace_path = builder_workspace(tmp_path)
    _seed_complete_5_implement(builder_workspace_path / ".builder" / "specs" / "demo")
    previous_cwd = Path.cwd()
    os.chdir(builder_workspace_path)
    # This test is about the codex-lane WIRING (drives codex, resolves runner_task_ref) --
    # not host-verify semantics, and the fixture deliberately has no command map. Since
    # BUILDER_HOST_VERIFY_REQUIRE_COMMANDS now defaults to '1' (B5b, fail-closed), leaving
    # BUILDER_HOST_VERIFY at its own default ('enforce') would fail this workspace on
    # fail:unverifiable for a reason unrelated to what the test asserts. Scope the host-verify
    # gate off for the duration of this test only.
    saved_host_verify = os.environ.get("BUILDER_HOST_VERIFY")
    os.environ["BUILDER_HOST_VERIFY"] = "off"
    try:
        runner = FakeProcessRunner(CompletedProcess(returncode=0, stdout="ok\n"))
        lane = CodexCliLane(process_runner=runner)

        result = lane.execute(
            {
                "kind": "builder-runner-task",
                "runner_task_ref": ".builder/specs/demo/runs/task-T7.yaml",
                "phase": "5-implement",
                "notes": {"keep": "opaque"},
            },
            "codex-cli",
            {"attempt_id": "attempt-1", "work_id": "work-1", "log_path": "queue/attempts/attempt-1.log"},
        )

        # Resolve both sides: on macOS /var -> /private/var, and the lane emits the resolved cwd.
        ws = str(builder_workspace_path.resolve())
        assert result.result_type == DispatchResultType.SUCCESS
        assert len(runner.calls) == 1
        argv, kwargs = runner.calls[0]
        assert argv[:2] == ["codex", "exec"], "the lane drives codex exec"
        assert ws in argv, "codex runs in the workspace (-C <cwd>)"
        assert kwargs.get("cwd") == ws
        resolved = str((builder_workspace_path / ".builder" / "specs" / "demo" / "runs" / "task-T7.yaml").resolve())
        assert str(Path(result.metadata["runner_task_ref"]).resolve()) == resolved
    finally:
        if saved_host_verify is None:
            os.environ.pop("BUILDER_HOST_VERIFY", None)
        else:
            os.environ["BUILDER_HOST_VERIFY"] = saved_host_verify
        os.chdir(previous_cwd)


def test_lane_adapters_reject_non_run_contract_runner_task_refs(tmp_path: Path):
    builder_workspace_path = builder_workspace(tmp_path)
    previous_cwd = Path.cwd()
    os.chdir(builder_workspace_path)
    try:
        lane = CodexCliLane(process_runner=FakeProcessRunner(CompletedProcess(returncode=0)))
        for runner_task_ref in (
            ".builder/specs/demo/spec.yaml",
            ".builder/specs/demo/runs/not-a-task.txt",
            ".builder/specs/demo/runs/task-missing.yaml",
        ):
            try:
                lane.execute(
                    {"kind": "builder-runner-task", "runner_task_ref": runner_task_ref},
                    "codex-cli",
                    {"attempt_id": "attempt-1", "work_id": "work-1", "log_path": "queue/attempts/attempt-1.log"},
                )
            except ValueError:
                continue
            raise AssertionError(f"expected ValueError for {runner_task_ref}")
    finally:
        os.chdir(previous_cwd)


def test_blocked_human_item_preserves_attempt_history_without_redispatch(tmp_path: Path):
    store = QueueStore(tmp_path)
    item = store.enqueue(task_ref={"kind": "builder-runner-task", "runner_task_ref": "runs/task-T7.yaml"})
    executor = RecordingExecutor(human_block_result())
    scheduler = DispatchScheduler(store, dispatch_config(tmp_path), executor, owner_id="scheduler-a")

    assert scheduler.dispatch_once() == [item.id]
    assert scheduler.wait_for_attempts()

    blocked = store.get_item(item.id)
    assert blocked is not None
    assert blocked.state == WorkItemState.BLOCKED_HUMAN
    attempts_after_first_dispatch = dict(store.reconstruct().attempts)
    assert len(attempts_after_first_dispatch) == 1

    assert scheduler.dispatch_once() == []
    assert scheduler.wait_for_attempts()
    assert len(store.reconstruct().attempts) == 1
    assert len(executor.calls) == 1


def test_lease_reclaim_recovers_interrupted_attempt_without_duplicate_active_attempts(tmp_path: Path):
    store = QueueStore(tmp_path)
    item = store.enqueue(task_ref={"kind": "builder-runner-task", "runner_task_ref": "runs/task-T7.yaml"})
    store.record_attempt(item.id, attempt_id="attempt-stale", lane="codex-cli", metadata={"log_path": "queue/attempts/attempt-stale.log"})
    store.transition_item(
        item.id,
        WorkItemState.DISPATCHED,
        lease={"id": "lease-stale", "attempt_id": "attempt-stale", "lane": "codex-cli", "expires_at": "2099-01-01T00:00:00Z"},
    )
    executor = RecordingExecutor(success_result())
    scheduler = DispatchScheduler(store, dispatch_config(tmp_path), executor, owner_id="scheduler-a")

    assert scheduler.reclaim_stale_leases(active_attempt_ids=set()) == [item.id]
    assert scheduler.dispatch_once() == [item.id]
    assert scheduler.wait_for_attempts()

    updated = store.get_item(item.id)
    assert updated is not None
    assert updated.state == WorkItemState.SUCCEEDED
    attempt_ids = set(store.reconstruct().attempts)
    assert "attempt-stale" in attempt_ids
    assert len(attempt_ids) == 2
    assert executor.calls[0][2]["attempt_id"] != "attempt-stale"
