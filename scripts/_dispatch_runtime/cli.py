"""Operator CLI scaffold for the dispatch control plane."""

from __future__ import annotations

import argparse
import datetime
import os
import re
import time
from pathlib import Path
from typing import Sequence

from _builder_project_model.ownership_guard import evaluate_repo_ownership, refusal_message
from _dispatch_runtime.config import DispatchConfig, load_dispatch_config
from _dispatch_runtime.lane_common import _live_pgids_dir, sweep_orphan_pgids
from _dispatch_runtime.paths import runtime_dir
from _dispatch_runtime.phase_routing import route_lane
from _dispatch_runtime.queue_store import QueueStore, _read_yaml
from _dispatch_runtime.scheduler import DispatchScheduler
from _dispatch_runtime.state_model import TERMINAL_STATES, WorkItemState


class _RoutingExecutor:
    """Routes a dispatched item to the claude or codex lane by the lane's provider."""

    def __init__(self, config: DispatchConfig):
        from _dispatch_runtime.lane_claude_code_cli import ClaudeCodeCliLane
        from _dispatch_runtime.lane_codex_cli import CodexCliLane

        self._config = config
        self._claude = ClaudeCodeCliLane()
        self._codex = CodexCliLane()

    def _lane_for(self, lane_name: str):
        lane = self._config.lanes.get(lane_name)
        provider = (lane.provider if lane else lane_name) or lane_name
        return self._codex if "codex" in provider.lower() else self._claude

    def execute(self, task_ref, lane_name, attempt_context):
        return self._lane_for(lane_name).execute(task_ref, lane_name, attempt_context)


def _slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    slug = "-".join(s.split("-")[:6])[:48].strip("-")
    return slug or "spec"


def _draft_spec(specs_dir: Path, intent: str, spec_id: str | None, *, plan_gate: bool = False,
                reviews: int | None = None) -> str:
    """Create a minimal spec skeleton at the `spec` phase from a natural-language intent.

    `plan_gate=True` opts this spec INTO the plan-approval gate (it will hold after the
    plan phase for a human `approve`); the default is full automation to verified.
    """
    chosen = spec_id or _slugify(intent)
    spec_dir = specs_dir / chosen
    if spec_dir.exists() and not spec_id:
        i = 2
        while (specs_dir / f"{chosen}-{i}").exists():
            i += 1
        chosen = f"{chosen}-{i}"
        spec_dir = specs_dir / chosen
    spec_dir.mkdir(parents=True, exist_ok=True)
    today = datetime.date.today().isoformat()
    # Single-line quoted scalar: works with both PyYAML and the repo's yaml shim
    # (the shim does not parse block scalars).
    flat = " ".join((intent or "").split()) or "(no intent provided)"
    flat = flat.replace("\\", "\\\\").replace('"', '\\"')
    gate_line = "plan_gate: true\n" if plan_gate else ""
    reviews_line = f"reviews: {reviews}\n" if reviews is not None else ""
    # R13: dispatcher-drafted specs default to ai_native so the autonomous path
    # does not pay for model-authored Markdown dual-write nothing reads at runtime.
    # Escape hatch: BUILDER_DRAFT_ARTIFACT_MODE=dual restores the old default.
    draft_mode = (os.environ.get("BUILDER_DRAFT_ARTIFACT_MODE") or "ai_native").strip().lower()
    if draft_mode not in ("dual", "ai_native"):
        draft_mode = "ai_native"
    (spec_dir / "spec.yaml").write_text(
        f'name: {chosen}\n'
        f'created: "{today}"\n'
        f'status: specifying\n'
        f'current_phase: spec\n'
        f'next_action: "/isanna-spec {chosen}"\n'
        f'artifact_mode: {draft_mode}\n'
        f'{gate_line}'
        f'{reviews_line}'
        f'summary: "{flat}"\n',
        encoding="utf-8",
    )
    if not (spec_dir / "phase-log.yaml").exists():
        (spec_dir / "phase-log.yaml").write_text("phases: []\n", encoding="utf-8")
    return chosen


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="builder-dispatch")
    parser.add_argument("--config", default=None, help="dispatch config path (default: active runtime directory)")
    subparsers = parser.add_subparsers(dest="command", required=True)

    enqueue = subparsers.add_parser("enqueue", help="enqueue a runner task reference")
    enqueue.add_argument("runner_task_ref")
    enqueue.add_argument("--lane")
    enqueue.add_argument("--priority", type=int, default=0)

    subparsers.add_parser("status", help="print dispatch queue status")

    lanes = subparsers.add_parser("lanes", help="inspect or control provider lanes")
    lane_subparsers = lanes.add_subparsers(dest="lane_command")
    pause = lane_subparsers.add_parser("pause", help="pause a lane")
    pause.add_argument("lane")
    resume = lane_subparsers.add_parser("resume", help="resume a lane")
    resume.add_argument("lane")

    cancel = subparsers.add_parser("cancel", help="cancel a queued or in-flight work item")
    cancel.add_argument("work_id")

    pause_item = subparsers.add_parser("pause", help="pause ONE queued work item (scheduler skips it until `continue`)")
    pause_item.add_argument("work_id")

    continue_item = subparsers.add_parser("continue", help="resume a paused work item back to queued")
    continue_item.add_argument("work_id")

    gc = subparsers.add_parser("gc", help="garbage-collect terminal (succeeded/failed/cancelled) work-item records")
    gc.add_argument("--older-than-days", type=float, default=0.0,
                    help="only GC terminal records older than N days (default 0 = all terminal records)")
    gc.add_argument("--dry-run", action="store_true", help="report what would be removed; delete nothing")
    gc.add_argument("--orphans", action="store_true",
                    help="also remove attempt/event records whose work item no longer exists "
                         "(subject to --older-than-days)")

    subparsers.add_parser("drain", help="stop admitting new dispatches")

    run_cmd = subparsers.add_parser("run", help="run the dispatch scheduler loop (the daemon)")
    run_cmd.add_argument("--once", action="store_true", help="dispatch a single item, wait for it, then exit")
    run_cmd.add_argument("--interval", type=float, default=15.0, help="idle poll interval in seconds")

    approve = subparsers.add_parser("approve", help="approve a held plan-approval gate; enqueue the next phase")
    approve.add_argument("spec")
    approve.add_argument("--lane")

    hold = subparsers.add_parser("hold", help="hold a pending lane-B veto window")
    hold.add_argument("spec")
    hold.add_argument("--reason", default="")

    draft = subparsers.add_parser("draft", help="draft a spec skeleton from a natural-language intent and enqueue the spec phase")
    draft.add_argument("intent")
    draft.add_argument("--lane")
    draft.add_argument("--spec", help="explicit spec id (default: slug of the intent)")
    draft.add_argument("--priority", type=int, default=0)
    draft.add_argument("--reviews", type=int, choices=(0, 1, 2),
                       help="independent reviewer count for this spec (0, 1, or 2)")
    draft.add_argument(
        "--plan-gate", action="store_true",
        help="opt this spec into the plan-approval gate (hold after plan for human `approve`); "
             "default is full automation to verified/ready-to-archive",
    )
    return parser


def _load_store(config_path: str) -> QueueStore:
    config = load_dispatch_config(config_path)
    return QueueStore(config.queue_store_path)


def run(argv: Sequence[str] | None = None, *, _store_override: QueueStore | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.config is None:
        args.config = str(runtime_dir(Path.cwd()) / "dispatch.yaml")

    def _get_store() -> QueueStore:
        if _store_override is not None:
            return _store_override
        return _load_store(args.config)

    if args.command == "enqueue":
        store = _get_store()
        ref = args.runner_task_ref
        _ref_name = Path(ref).name
        _ref_parent = Path(ref).parent.name
        task_ref: dict[str, object] = {"runner_task_ref": ref}
        if _ref_name.startswith("phase-") and _ref_parent == "runs":
            task_ref["kind"] = "builder-phase-batch"
        item = store.enqueue(
            task_ref=task_ref,
            lane=args.lane,
            priority=args.priority,
        )
        print(item.id)
        return 0

    if args.command == "status":
        from _dispatch_runtime.status import build_status_snapshot
        store = _get_store()
        snap = build_status_snapshot(store)
        print("Queue depth by state:")
        for state, count in sorted(snap.queue_depth.items()):
            print(f"  {state}: {count}")
        if snap.lane_inflight:
            print("Lane in-flight:")
            for lane, count in sorted(snap.lane_inflight.items()):
                print(f"  {lane}: {count}")
        if snap.lane_cooldown_remaining:
            print("Lane cooldown remaining (s):")
            for lane, secs in sorted(snap.lane_cooldown_remaining.items()):
                print(f"  {lane}: {secs}s")
        if snap.attempt_heartbeats:
            print("Attempt heartbeat age (s):")
            for attempt_id, age in sorted(snap.attempt_heartbeats.items()):
                print(f"  {attempt_id}: {age}s ago")
        if snap.recent_events:
            print(f"Recent events ({len(snap.recent_events)}):")
            for event in snap.recent_events[-5:]:
                print(f"  {event.created_at} {event.event_type} {event.work_id}")
        return 0

    if args.command == "lanes":
        lane_command = getattr(args, "lane_command", None)
        if lane_command is None:
            parser.error("lanes requires a subcommand")
        store = _get_store()
        lane_name: str = args.lane
        if lane_command == "pause":
            paused_path = store.lanes_dir / f"{lane_name}.paused"
            paused_path.write_text("paused", encoding="utf-8")
            print(f"lane {lane_name}: paused")
        elif lane_command == "resume":
            paused_path = store.lanes_dir / f"{lane_name}.paused"
            if paused_path.exists():
                paused_path.unlink()
            print(f"lane {lane_name}: resumed")
        return 0

    if args.command == "cancel":
        store = _get_store()
        work_id: str = args.work_id
        item = store.get_item(work_id)
        if item is None:
            print(f"error: work item not found: {work_id}", flush=True)
            return 1
        if item.state in TERMINAL_STATES:
            print(f"error: cannot cancel terminal item {work_id} (state={item.state.value})", flush=True)
            return 1
        store.transition_item(work_id, WorkItemState.CANCELLED)
        print(f"cancelled: {work_id}")
        return 0

    if args.command == "pause":
        store = _get_store()
        work_id = args.work_id
        item = store.get_item(work_id)
        if item is None:
            print(f"error: work item not found: {work_id}", flush=True)
            return 1
        if item.state != WorkItemState.QUEUED:
            print(
                f"error: can only pause a queued item; {work_id} is {item.state.value} "
                f"(use `cancel` for in-flight work)",
                flush=True,
            )
            return 1
        store.transition_item(work_id, WorkItemState.PAUSED)
        print(f"paused: {work_id} (scheduler will skip it; run `continue {work_id}` to resume)")
        return 0

    if args.command == "continue":
        store = _get_store()
        work_id = args.work_id
        item = store.get_item(work_id)
        if item is None:
            print(f"error: work item not found: {work_id}", flush=True)
            return 1
        if item.state != WorkItemState.PAUSED:
            print(f"error: can only continue a paused item; {work_id} is {item.state.value}", flush=True)
            return 1
        store.transition_item(work_id, WorkItemState.QUEUED)
        print(f"continued: {work_id} (re-queued)")
        return 0

    if args.command == "gc":
        store = _get_store()
        cutoff = None
        if args.older_than_days and args.older_than_days > 0:
            cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=args.older_than_days)

        def _parse_iso(value):
            try:
                return datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            except (ValueError, TypeError):
                return None

        removed = 0
        kept = 0
        removed_work_ids: set[str] = set()
        for path in sorted(store.items_dir.glob("*.yaml")):
            item = store.get_item(path.stem)
            if item is None or item.state not in TERMINAL_STATES:
                kept += 1
                continue
            if cutoff is not None:
                ts = _parse_iso(item.updated_at)
                if ts is not None and ts > cutoff:
                    kept += 1
                    continue
            if args.dry_run:
                print(f"would remove {path.name} ({item.state.value})")
            else:
                path.unlink()
            removed += 1
            removed_work_ids.add(path.stem)

        # A removed item's attempt/event history is otherwise orphaned forever
        # (attempts/events accumulate without bound across daemon ticks).
        attempts_removed = 0
        for path in sorted(store.attempts_dir.glob("*.yaml")):
            record = _read_yaml(path)
            if str(record.get("work_id")) not in removed_work_ids:
                continue
            if args.dry_run:
                print(f"would remove {path.name} (attempt for {record.get('work_id')})")
            else:
                path.unlink()
            attempts_removed += 1
        events_removed = 0
        for path in sorted(store.events_dir.glob("*.yaml")):
            record = _read_yaml(path)
            if str(record.get("work_id")) not in removed_work_ids:
                continue
            if args.dry_run:
                print(f"would remove {path.name} (event for {record.get('work_id')})")
            else:
                path.unlink()
            events_removed += 1

        orphans_removed = 0
        if args.orphans:
            # Attempts/events whose work item is gone for any other reason
            # (e.g. a prior gc run predating this fix, or manual item deletion).
            for directory, label in ((store.attempts_dir, "attempt"), (store.events_dir, "event")):
                for path in sorted(directory.glob("*.yaml")):
                    record = _read_yaml(path)
                    work_id = str(record.get("work_id") or "")
                    if work_id in removed_work_ids:
                        continue  # already accounted for above
                    if store.get_item(work_id) is not None:
                        continue  # work item still exists
                    if cutoff is not None:
                        ts = _parse_iso(record.get("created_at"))
                        if ts is not None and ts > cutoff:
                            continue
                    if args.dry_run:
                        print(f"would remove orphan {label} {path.name} (no matching work item)")
                    else:
                        path.unlink()
                    orphans_removed += 1

        verb = "would remove" if args.dry_run else "removed"
        window = f" older than {args.older_than_days}d" if cutoff is not None else ""
        print(
            f"gc: {verb} {removed} terminal record(s){window}, "
            f"{attempts_removed} attempt record(s), {events_removed} event record(s); "
            f"kept {kept} (active or in-window)"
        )
        if args.orphans:
            print(f"gc: {verb} {orphans_removed} orphan attempt/event record(s){window}")
        return 0

    if args.command == "drain":
        store = _get_store()
        drain_flag = store.queue_dir / ".drain"
        drain_flag.write_text("drain", encoding="utf-8")
        print("drain: dispatch paused; in-flight work will complete")
        return 0

    if args.command == "run":
        config_path = Path(args.config).resolve()
        project_dir = config_path.parent.parent
        ownership = evaluate_repo_ownership(project_dir)
        for finding in ownership.findings:
            print(f"ownership-guard: {finding}", flush=True)
        if ownership.owned:
            print(refusal_message(ownership, launcher_label="builder-dispatch run"), flush=True)
            return 2
        config = load_dispatch_config(str(config_path))
        store = _store_override or QueueStore(project_dir / config.queue_store_path)
        scheduler = DispatchScheduler(
            store, config, _RoutingExecutor(config),
            owner_id=f"dispatch-{os.getpid()}", project_dir=project_dir,
            # Lease must outlast the longest lane phase timeout (claude lane
            # DEFAULT_TIMEOUT=1800s) so reclaim_stale_leases() never reclaims a
            # still-running attempt and double-dispatches it (audit A3).
            lease_seconds=2100,
        )
        drain_flag = store.queue_dir / ".drain"
        # R12: one-shot sweep of agent groups orphaned by a SIGKILLed predecessor
        # daemon, BEFORE admitting work — else a reclaimed lease could dispatch a
        # second agent into a tree an orphan is still editing. OPT-IN (default off);
        # the path mirrors resolve_work's work.queue_root exactly so record==sweep.
        swept = sweep_orphan_pgids(_live_pgids_dir(runtime_dir(project_dir) / "dispatch-queue"))
        if swept:
            print(f"orphan-sweep: killed {len(swept)} stale agent group(s): {swept}", flush=True)
        print(
            f"builder-dispatch run: project={project_dir} queue={store.root} "
            f"interval={args.interval}s once={args.once}",
            flush=True,
        )
        while True:
            if drain_flag.exists():
                print("drain flag set; not admitting new work", flush=True)
                # Wait (bounded) for any in-flight attempt to finish and reap its agent
                # group, so a breaker-triggered drain (R6) cannot orphan a live agent
                # mid-write and leave a stranded RUNNING item for the next startup sweep.
                scheduler.wait_for_attempts(timeout=2100.0)
                if args.once:
                    print("run --once: no eligible work", flush=True)
                break
            try:
                dispatched = scheduler.dispatch_once()
            except Exception as exc:  # noqa: BLE001 - one bad turn must not kill the daemon
                print(f"dispatch error: {exc}", flush=True)
                dispatched = []
            if args.once:
                if dispatched:
                    scheduler.wait_for_attempts(timeout=2000.0)
                    print(f"dispatched {dispatched}", flush=True)
                else:
                    print("run --once: no eligible work", flush=True)
                break
            if dispatched:
                print(f"dispatched {dispatched}", flush=True)
                time.sleep(1.0)  # drain queue quickly across lanes without busy-spin
                continue
            time.sleep(args.interval)
        return 0

    if args.command == "approve":
        config_path = Path(args.config).resolve()
        project_dir = config_path.parent.parent
        config = load_dispatch_config(str(config_path))
        store = _store_override or QueueStore(project_dir / config.queue_store_path)
        marker = store.queue_dir / "gates" / f"{args.spec}.json"
        if not marker.exists():
            print(f"error: no plan-approval gate pending for spec: {args.spec}", flush=True)
            return 1
        import json
        gate = json.loads(marker.read_text(encoding="utf-8"))
        next_phase = gate["next_phase"]
        ref = f"{runtime_dir(project_dir).name}/specs/{args.spec}/runs/phase-{next_phase}.yaml"
        item = store.enqueue(
            task_ref={"kind": "builder-phase-batch", "runner_task_ref": ref, "spec_id": args.spec},
            lane=(args.lane or gate.get("lane") or None),
            priority=int(gate.get("priority", 0)),
        )
        # Durable approval token: the dispatch-admission guard (lane_common.resolve_work)
        # lets post-gate phases (implement/verify) run ONLY when this exists. Written
        # AFTER the implement enqueue and BEFORE unlinking the plan marker so there is no
        # window where the gate is consumed but approval is unrecorded.
        (store.queue_dir / "gates" / f"{args.spec}.approved").write_text(
            f"approved phase: {next_phase}\n", encoding="utf-8"
        )
        marker.unlink()
        print(f"approved: {args.spec} -> enqueued {next_phase} as {item.id}")
        return 0

    if args.command == "hold":
        config_path = Path(args.config).resolve()
        project_dir = config_path.parent.parent
        config = load_dispatch_config(str(config_path))
        store = _store_override or QueueStore(project_dir / config.queue_store_path)
        scheduler = DispatchScheduler(
            store, config, executor=None, owner_id=f"operator-{os.getpid()}", project_dir=project_dir,
        )
        if not scheduler.hold_veto_window(args.spec, reason=args.reason):
            print(f"error: no lane-B veto window pending for spec: {args.spec}", flush=True)
            return 1
        print(f"held: {args.spec}")
        return 0

    if args.command == "draft":
        config_path = Path(args.config).resolve()
        project_dir = config_path.parent.parent
        config = load_dispatch_config(str(config_path))
        store = _store_override or QueueStore(project_dir / config.queue_store_path)
        specs_dir = runtime_dir(project_dir) / "specs"
        spec_id = _draft_spec(
            specs_dir, args.intent, args.spec,
            plan_gate=bool(getattr(args, "plan_gate", False)), reviews=args.reviews,
        )
        ref = f"{runtime_dir(project_dir).name}/specs/{spec_id}/runs/phase-spec.yaml"
        lane = args.lane or route_lane("spec", list(config.lanes.keys()),
                                       default_lane=config.pipeline.get("default_lane", "claude"))
        item = store.enqueue(
            task_ref={"kind": "builder-phase-batch", "runner_task_ref": ref, "spec_id": spec_id},
            lane=lane,
            priority=args.priority,
        )
        print(f"drafted: {spec_id} -> enqueued spec as {item.id}")
        return 0

    parser.error(f"unknown command: {args.command}")
    return 2
