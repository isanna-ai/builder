#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
TMPDIR=$(mktemp -d)
trap 'rm -rf "$TMPDIR"' EXIT

export PYTHONPATH="$REPO_ROOT/scripts"
export CODEX_API_KEY="dummy-codex"
export CLAUDE_CODE_API_KEY="dummy-claude"
export TMPDIR_SMOKE="$TMPDIR"

mkdir -p "$TMPDIR/.builder/specs/demo/runs"

cat > "$TMPDIR/.builder/specs/demo/runs/task-T7.yaml" <<'YAML'
artifact: runner-task
id: task-T7
YAML

cat > "$TMPDIR/.builder/dispatch.yaml" <<'YAML'
queue_store:
  path: TMPDIR_PLACEHOLDER/.builder/dispatch
lanes:
  - name: codex-cli
    provider: codex-cli
    max_concurrency: 1
    secrets:
      api_key: ${CODEX_API_KEY}
  - name: claude-code-cli
    provider: claude-code-cli
    max_concurrency: 1
    secrets:
      api_key: ${CLAUDE_CODE_API_KEY}
routing_policy:
  default: ordered
  tie_break: lane_order
cooldown_policy:
  default_seconds: 30
retry_policy:
  max_attempts: 2
  initial_seconds: 1
  max_seconds: 5
  jitter_seconds: 0
YAML
sed -i "s|TMPDIR_PLACEHOLDER|$TMPDIR|g" "$TMPDIR/.builder/dispatch.yaml"

cd "$TMPDIR" && python3 "$REPO_ROOT/scripts/builder-dispatch.py" --config "$TMPDIR/.builder/dispatch.yaml" enqueue .builder/specs/demo/runs/task-T7.yaml > "$TMPDIR/work-id.txt"
WORK_ID=$(tr -d '\n' < "$TMPDIR/work-id.txt")

python3 <<'PY'
import os
from dataclasses import dataclass
from _dispatch_runtime.config import load_dispatch_config
from _dispatch_runtime.lane_executor import DispatchResult, DispatchResultType
from _dispatch_runtime.queue_store import QueueStore
from _dispatch_runtime.scheduler import DispatchScheduler
from _dispatch_runtime.state_model import WorkItemState


@dataclass
class RecordingExecutor:
    calls: list[tuple[dict, str, dict]]

    def execute(self, task_ref, lane_name, attempt_context):
        self.calls.append((dict(task_ref), lane_name, dict(attempt_context)))
        return DispatchResult(
            result_type=DispatchResultType.SUCCESS,
            metadata={
                "pid": 999,
                "logs": [attempt_context["log_path"]],
                "runner_task_ref": task_ref["runner_task_ref"],
            },
        )


root = os.environ["TMPDIR_SMOKE"]
os.chdir(root)
config = load_dispatch_config(f"{root}/.builder/dispatch.yaml")
store = QueueStore(config.queue_store_path)
executor = RecordingExecutor(calls=[])
scheduler = DispatchScheduler(store, config, executor, owner_id="smoke")

scheduled = scheduler.dispatch_once()
assert len(scheduled) == 1, scheduled
assert scheduler.wait_for_attempts(timeout=2.0)
item = store.get_item(scheduled[0])
assert item is not None and item.state == WorkItemState.SUCCEEDED
attempts = store.reconstruct().attempts
assert attempts, "expected attempt record"
assert any(a.metadata.get("runner_task_ref", "").endswith("task-T7.yaml") for a in attempts.values())
assert len(executor.calls) == 1

recovery = store.enqueue(task_ref={"kind": "builder-runner-task", "runner_task_ref": ".builder/specs/demo/runs/task-T7.yaml"})
store.record_attempt(recovery.id, attempt_id="attempt-stale", lane="codex-cli", metadata={"log_path": "queue/attempts/attempt-stale.log"})
store.transition_item(
    recovery.id,
    WorkItemState.DISPATCHED,
    lease={"id": "lease-stale", "attempt_id": "attempt-stale", "lane": "codex-cli", "expires_at": "2099-01-01T00:00:00Z"},
)
assert scheduler.reclaim_stale_leases(active_attempt_ids=set()) == [recovery.id]
rescheduled = scheduler.dispatch_once()
assert rescheduled == [recovery.id], rescheduled
assert scheduler.wait_for_attempts(timeout=2.0)
recovered = store.get_item(recovery.id)
assert recovered is not None and recovered.state == WorkItemState.SUCCEEDED
assert len(store.reconstruct().attempts) >= 3
assert len(executor.calls) == 2
PY

STATUS_OUTPUT=$(cd "$TMPDIR" && python3 "$REPO_ROOT/scripts/builder-dispatch.py" --config "$TMPDIR/.builder/dispatch.yaml" status)
printf '%s\n' "$STATUS_OUTPUT" | grep -q 'Queue depth by state:'
printf '%s\n' "$STATUS_OUTPUT" | grep -q 'succeeded: 2'

python3 <<'PY'
import os
from _dispatch_runtime.config import load_dispatch_config
from _dispatch_runtime.queue_store import QueueStore

root = os.environ["TMPDIR_SMOKE"]
config = load_dispatch_config(f"{root}/.builder/dispatch.yaml")
store = QueueStore(config.queue_store_path)
snapshot = store.reconstruct()
assert len(snapshot.events) >= 5, len(snapshot.events)
assert any(event.event_type == "enqueue" for event in snapshot.events)
assert any(event.event_type == "attempt_recorded" for event in snapshot.events)
PY

echo "dispatch smoke test passed for $WORK_ID"
