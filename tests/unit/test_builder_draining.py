from __future__ import annotations

import json
import os
import shutil
import sys
import textwrap
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
FIXTURES = ROOT / "tests" / "fixtures" / "builder_project_model" / "declarations" / "v1"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from _builder_project_model.attribution import AdmissionStore, Membership, receipt_for_work, should_enqueue_physical
from _builder_project_model.draining import FederatedDrainer
from _builder_project_model.fair_share import FairShareCandidate, choose_candidate, sort_group
from _builder_project_model.governor import BuilderGovernor
from _builder_project_model.home import load_builder_home
from _builder_project_model.readiness import evaluate_cross_repo_dependencies
from _builder_project_model.repo_controller import RepoController
from _dispatch_runtime.lane_common import _pgid_group_alive, _reap_group
from _dispatch_runtime.paths import runtime_dir
from _dispatch_runtime.queue_store import QueueStore


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _wait_for_path(path: Path, timeout_seconds: float = 5) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline and not path.exists():
        time.sleep(0.01)
    assert path.exists()


def _repo(root: Path, name: str) -> Path:
    repo = root / name
    (repo / ".git").mkdir(parents=True)
    return repo


def _dispatch_yaml(*, dependency_gating: bool = True, lane_limit: int = 1) -> str:
    gate = "true" if dependency_gating else "false"
    return textwrap.dedent(
        f"""\
        queue_store:
          path: .builder/dispatch-queue
        lanes:
          - name: claude
            provider: claude-code-cli
            max_concurrency: {lane_limit}
          - name: codex
            provider: codex-cli
            max_concurrency: {lane_limit}
        routing_policy:
          default: ordered
        cooldown_policy:
          default_seconds: 120
        retry_policy:
          max_attempts: 3
          initial_seconds: 10
          max_seconds: 120
          jitter_seconds: 0
        pipeline:
          dependency_gating: {gate}
        """
    )


def _policy_yaml(*, enabled: bool, drain_repos: list[str], claude: int = 2, codex: int = 2) -> str:
    flag = "true" if enabled else "false"
    lines = [
        "schema_version: 1",
        "governor:",
        f"  enabled: {flag}",
        "  drain_repos:",
    ]
    if drain_repos:
        lines.extend(f"    - {repo_id}" for repo_id in drain_repos)
    else:
        lines.append("    []")
    lines.extend(
        [
            "providers:",
            "  claude-code-cli:",
            f"    max_sessions: {claude}",
            "    quota_cooldown:",
            "      initial_seconds: 300",
            "      max_seconds: 3600",
            "  codex-cli:",
            f"    max_sessions: {codex}",
            "    quota_cooldown:",
            "      initial_seconds: 300",
            "      max_seconds: 3600",
            "allocation:",
            "  policy: equal-weight-fair-share",
            "  project_weight: 1",
            "scheduler:",
            "  poll_seconds: 2",
            "  heartbeat_seconds: 5",
            "  stale_daemon_seconds: 30",
        ]
    )
    return "\n".join(lines) + "\n"


def _seed_home(tmp_path: Path, *, drain_repos: list[str], enabled: bool = True, lane_limit: int = 1):
    home = tmp_path / ".builder-home"
    alpha = _repo(tmp_path, "alpha-repo")
    beta = _repo(tmp_path, "beta-repo")
    locked = _repo(tmp_path, "locked-repo")
    broken = _repo(tmp_path, "broken-repo")
    for repo in (alpha, beta, locked):
        _write(runtime_dir(repo) / "dispatch.yaml", _dispatch_yaml(lane_limit=lane_limit))
        QueueStore(runtime_dir(repo) / "dispatch-queue")
    _write(home / "builder.yaml", textwrap.dedent(
        """\
        schema_version: 1
        home_id: phase4
        repositories: repositories.yaml
        policy: policy.yaml
        projects:
          - id: portfolio
            manifest: projects/portfolio/product.yaml
        """
    ))
    _write(home / "repositories.yaml", textwrap.dedent(
        """\
        schema_version: 1
        repos:
          - id: alpha-repo
            path: ../alpha-repo
          - id: beta-repo
            path: ../beta-repo
          - id: locked-repo
            path: ../locked-repo
          - id: broken-repo
            path: ../broken-repo
        """
    ))
    _write(home / "policy.yaml", _policy_yaml(enabled=enabled, drain_repos=drain_repos))
    _write(home / "projects" / "portfolio" / "product.yaml", textwrap.dedent(
        """\
        schema_version: 1
        product: portfolio
        title: Portfolio
        description: Shared synthetic portfolio
        default_repo: alpha-repo
        repos:
          - alias: alpha
            repo_id: alpha-repo
          - alias: beta
            repo_id: beta-repo
        backlog: []
        releases:
          - name: wave-1
            manifest: releases/wave-1.yaml
        """
    ))
    _write(runtime_dir(alpha) / "intents" / "wave-1-work" / "intent.yaml", textwrap.dedent(
        """\
        artifact: intent-object
        intent: wave-1-work
        title: Wave 1 work
        status: accepted
        problem: p
        why: w
        success_criteria:
          - id: sc-1
            statement: s
        non_goals:
          - n
        ssot_delta:
          capabilities: []
          behaviors: []
          journeys: []
        specs:
          - alpha/core
          - beta/shared
        """
    ))
    _write(home / "projects" / "portfolio" / "releases" / "wave-1.yaml", textwrap.dedent(
        """\
        schema_version: 1
        name: wave-1
        description: Synthetic release
        status: active
        intents:
          - wave-1-work
        """
    ))
    return load_builder_home(home), alpha, beta, locked, broken


def _seed_spec(repo: Path, spec_id: str, *, status: str = "implementing", deps: str | None = None):
    spec_dir = runtime_dir(repo) / "specs" / spec_id
    spec_dir.mkdir(parents=True, exist_ok=True)
    _write(spec_dir / "spec.yaml", f"status: {status}\n")
    if deps is not None:
        _write(spec_dir / "dependencies.yaml", deps)
    return spec_dir


def _enqueue(repo: Path, spec_id: str, *, priority: int = 10, lane: str = "claude"):
    store = QueueStore(runtime_dir(repo) / "dispatch-queue")
    return store.enqueue(
        task_ref={"kind": "builder-phase-batch", "runner_task_ref": f".builder/specs/{spec_id}/runs/phase-3-review.yaml", "spec_id": spec_id},
        priority=priority,
        lane=lane,
    )


def _provider_script(tmp_path: Path) -> Path:
    script = tmp_path / "fake_provider.py"
    _write(
        script,
        textwrap.dedent(
            """\
            import argparse
            import json
            import os
            import time
            from pathlib import Path

            parser = argparse.ArgumentParser()
            parser.add_argument("--ready", required=True)
            parser.add_argument("--exit-flag", required=True)
            args = parser.parse_args()

            Path(args.ready).write_text(json.dumps({"pid": os.getpid(), "pgid": os.getpgrp()}), encoding="utf-8")
            while not Path(args.exit_flag).exists():
                time.sleep(0.05)
            """
        ),
    )
    return script


def _provider_command(tmp_path: Path) -> tuple[list[str], Path, Path]:
    ready = tmp_path / "provider-ready.json"
    exit_flag = tmp_path / "provider-exit"
    command = [sys.executable, str(_provider_script(tmp_path)), "--ready", str(ready), "--exit-flag", str(exit_flag)]
    return command, ready, exit_flag


def _git(*, branch_exists=False, is_ancestor_rc=1):
    class Result:
        def __init__(self, code: int, stdout: str):
            self.returncode = code
            self.stdout = stdout

    def run(args, cwd):
        if args[0] == "ls-remote":
            return Result(0, "abc\trefs/heads/x\n" if branch_exists else "")
        if args[0] == "symbolic-ref":
            return Result(0, "refs/remotes/origin/main\n")
        if args[0] == "merge-base":
            return Result(is_ancestor_rc, "")
        return Result(0, "")

    return run


def test_controller_isolation_keeps_healthy_repo_candidates_when_a_sibling_repo_is_broken(tmp_path):
    home, alpha, _beta, _locked, broken = _seed_home(
        tmp_path,
        drain_repos=["broken-repo", "alpha-repo"],
    )
    _seed_spec(alpha, "core")
    item = _enqueue(alpha, "core")
    shutil.rmtree(runtime_dir(broken), ignore_errors=True)

    drainer = FederatedDrainer(home, owner_id="central-1")
    findings = drainer.start()
    try:
        assert any(
            finding.startswith("broken-repo: controller unavailable:")
            for finding in findings
        )
        assert set(drainer.controllers) == {"alpha-repo"}
        status = drainer.controllers["alpha-repo"].current_candidates()
        assert [row.work_id for row in status.candidates] == [item.id]
    finally:
        drainer.stop()


def test_readiness_blocks_cross_repo_until_upstream_is_merged_and_host_verified_alone_is_insufficient(tmp_path):
    home, alpha, beta, _locked, _broken = _seed_home(tmp_path, drain_repos=["alpha-repo", "beta-repo"])
    _seed_spec(beta, "shared", status="verified")
    _seed_spec(alpha, "core", deps="dependencies:\n  - spec: beta/shared\n    kind: required\n")

    blocked = evaluate_cross_repo_dependencies(home=home, repo_id="alpha-repo", repo_root=alpha, spec_id="core", git_runner=_git())
    assert len(blocked) == 1
    assert blocked[0].required == "merged"

    _write(runtime_dir(beta) / "specs" / "shared" / "delivery.yaml", "recorded_by: builder-delivery\nbranch: x\ncommit: deadbeef\n")
    merged = evaluate_cross_repo_dependencies(
        home=home,
        repo_id="alpha-repo",
        repo_root=alpha,
        spec_id="core",
        git_runner=_git(branch_exists=True, is_ancestor_rc=0),
    )
    assert merged == []


def test_admission_receipts_dedup_physical_specs_and_preserve_first_project_attribution(tmp_path):
    home, alpha, _beta, _locked, _broken = _seed_home(tmp_path, drain_repos=["alpha-repo"])
    _seed_spec(alpha, "core")
    item = _enqueue(alpha, "core")

    assert should_enqueue_physical(home_root=home.root, repo_id="alpha-repo", repo_root=alpha, queue_root=runtime_dir(alpha) / "dispatch-queue", spec_id="core") is False

    store = QueueStore(runtime_dir(alpha) / "dispatch-queue")
    queued = store.get_item(item.id)
    assert queued is not None
    queued.state = queued.state.CANCELLED
    store.save_item(queued)

    receipts = AdmissionStore(home.root)
    first = receipts.write_receipt(
        admission_id="adm-1",
        repo_id="alpha-repo",
        spec_id="core",
        project_id="project-a",
        release_name="wave-1",
        roadmap_index=0,
        work_id="work-1",
        attempt_id=None,
        membership=Membership(project_id="project-a", release_name="wave-1", roadmap_index=0),
    )
    second = receipts.write_receipt(
        admission_id="adm-2",
        repo_id="alpha-repo",
        spec_id="core",
        project_id="project-b",
        release_name="wave-2",
        roadmap_index=1,
        work_id="work-1",
        attempt_id=None,
        membership=Membership(project_id="project-b", release_name="wave-2", roadmap_index=1),
    )

    assert first.admission_id == second.admission_id
    active = receipt_for_work(home.root, repo_id="alpha-repo", spec_id="core")
    assert active is not None
    assert active.project_id == "project-a"
    assert len(active.memberships) == 2


def test_admission_receipts_persist_under_home_state_admissions(tmp_path):
    home, alpha, _beta, _locked, _broken = _seed_home(tmp_path, drain_repos=["alpha-repo"])
    _seed_spec(alpha, "core")
    receipts = AdmissionStore(home.root)

    receipt = receipts.write_receipt(
        admission_id="adm-durable",
        repo_id="alpha-repo",
        spec_id="core",
        project_id="project-a",
        release_name="wave-1",
        roadmap_index=0,
        work_id="work-1",
        attempt_id="attempt-1",
        membership=Membership(project_id="project-a", release_name="wave-1", roadmap_index=0),
    )

    path = home.root / "state" / "admissions" / "adm-durable.json"
    assert path.is_file()
    assert json.loads(path.read_text(encoding="utf-8"))["project_id"] == "project-a"
    assert receipts.first_active("alpha-repo", "core").admission_id == receipt.admission_id


def test_fair_share_is_deterministic_work_conserving_and_persists_cursor(tmp_path):
    home, _alpha, _beta, _locked, _broken = _seed_home(tmp_path, drain_repos=[])
    first = choose_candidate(
        home_root=home.root,
        provider="claude-code-cli",
        candidates=[
            FairShareCandidate("claude-code-cli", "project-a", "alpha-repo", "work-a2", "claude", 5, 2, "2026-07-17T00:00:02Z"),
            FairShareCandidate("claude-code-cli", "project-a", "alpha-repo", "work-a1", "claude", 5, 1, "2026-07-17T00:00:01Z"),
            FairShareCandidate("claude-code-cli", "project-b", "beta-repo", "work-b1", "claude", 4, 0, "2026-07-17T00:00:03Z"),
        ],
    )
    assert first is not None and first.work_id == "work-a1"
    second = choose_candidate(
        home_root=home.root,
        provider="claude-code-cli",
        candidates=[
            FairShareCandidate("claude-code-cli", "project-a", "alpha-repo", "work-a1", "claude", 5, 1, "2026-07-17T00:00:01Z"),
            FairShareCandidate("claude-code-cli", "project-b", "beta-repo", "work-b1", "claude", 4, 0, "2026-07-17T00:00:03Z"),
        ],
    )
    assert second is not None and second.project_id == "project-b"
    allocation = BuilderGovernor(home).store.read_allocation()
    assert allocation["providers"]["claude-code-cli"]["cursor_project_id"] == "project-b"


def test_fair_share_uses_the_complete_sort_key_and_skips_absent_groups(tmp_path):
    home, _alpha, _beta, _locked, _broken = _seed_home(tmp_path, drain_repos=[])
    candidates = [
        FairShareCandidate("claude-code-cli", "project-a", "alpha-repo", "work-low-priority", "claude", 4, 0, "2026-07-17T00:00:00Z"),
        FairShareCandidate("claude-code-cli", "project-a", "alpha-repo", "work-late-roadmap", "claude", 5, 2, "2026-07-17T00:00:00Z"),
        FairShareCandidate("claude-code-cli", "project-a", "alpha-repo", "work-late-time", "claude", 5, 1, "2026-07-17T00:00:02Z"),
        FairShareCandidate("claude-code-cli", "project-a", "alpha-repo", "work-b", "claude", 5, 1, "2026-07-17T00:00:01Z"),
        FairShareCandidate("claude-code-cli", "project-a", "alpha-repo", "work-a", "claude", 5, 1, "2026-07-17T00:00:01Z"),
    ]
    assert [row.work_id for row in sort_group(candidates)] == [
        "work-a",
        "work-b",
        "work-late-time",
        "work-late-roadmap",
        "work-low-priority",
    ]

    governor = BuilderGovernor(home)
    governor.store.write_allocation(
        {
            "schema_version": 1,
            "providers": {
                "claude-code-cli": {"cursor_project_id": "blocked-project", "launch_count": 9},
            },
        }
    )
    chosen = choose_candidate(home_root=home.root, provider="claude-code-cli", candidates=[candidates[-1]])
    assert chosen is not None and chosen.project_id == "project-a"
    allocation = governor.store.read_allocation()["providers"]["claude-code-cli"]
    assert allocation == {"cursor_project_id": "project-a", "launch_count": 10}


def test_draining_only_launches_allow_listed_repos_and_refuses_live_legacy_owner_loudly(tmp_path):
    home, alpha, beta, locked, _broken = _seed_home(tmp_path, drain_repos=["alpha-repo", "locked-repo"])
    _seed_spec(alpha, "core")
    _seed_spec(beta, "shared")
    _seed_spec(locked, "legacy")
    alpha_item = _enqueue(alpha, "core", priority=20)
    _enqueue(beta, "shared", priority=10)
    _enqueue(locked, "legacy", priority=30)

    (runtime_dir(locked) / "dispatch-queue" / "queue" / ".scheduler.lock").write_text(f"dispatch-{os.getpid()}\n", encoding="utf-8")
    command, ready, exit_flag = _provider_command(tmp_path)
    governor = BuilderGovernor(home)
    drainer = FederatedDrainer(home, owner_id="central-1", governor=governor, command_builder=lambda _candidate: command)

    findings = drainer.start()
    try:
        assert any("locked-repo: loud refusal" in finding for finding in findings)
        launches = drainer.drain_once()
        assert [row.repo_id for row in launches] == ["alpha-repo"]
        assert launches[0].work_id == alpha_item.id
        _wait_for_path(ready)
        assert governor.store.capacity_remaining("claude-code-cli", max_sessions=2) == 1
    finally:
        exit_flag.write_text("done\n", encoding="utf-8")
        time.sleep(0.2)
        for record in governor.store.list_sessions():
            governor.reap_session(record["slot_id"], terminate=True)
        drainer.stop()


def test_drain_launch_boundary_rechecks_both_ceilings_and_reserves_before_spawn_under_lock(tmp_path):
    home, alpha, _beta, _locked, _broken = _seed_home(tmp_path, drain_repos=["alpha-repo"], lane_limit=1)
    _seed_spec(alpha, "core")
    item = _enqueue(alpha, "core")
    command, ready, exit_flag = _provider_command(tmp_path)
    governor = BuilderGovernor(home)
    drainer = FederatedDrainer(home, owner_id="central-1", governor=governor, command_builder=lambda _candidate: command)
    events: list[tuple[str, bool]] = []

    drainer.start()
    controller = drainer.controllers["alpha-repo"]
    original_recheck = controller.recheck_candidate
    original_lane_available = controller.lane_available
    original_capacity_remaining = governor.store.capacity_remaining
    original_reserve = governor.reserve_slot
    original_launch = governor.launch

    def checked_recheck(work_id):
        events.append(("eligibility", drainer._launch_lock.locked()))
        return original_recheck(work_id)

    def checked_lane_available(lane_name):
        events.append(("lane-ceiling", drainer._launch_lock.locked()))
        return original_lane_available(lane_name)

    def checked_capacity_remaining(provider, *, max_sessions):
        events.append(("provider-cap", drainer._launch_lock.locked()))
        return original_capacity_remaining(provider, max_sessions=max_sessions)

    def checked_reserve(**kwargs):
        events.append(("reserve", drainer._launch_lock.locked()))
        return original_reserve(**kwargs)

    def checked_launch(reservation, *, command, crash_phase=None):
        assert governor.store.load_session(reservation.slot_id)["state"] == "starting"
        events.append(("spawn", drainer._launch_lock.locked()))
        return original_launch(reservation, command=command, crash_phase=crash_phase)

    controller.recheck_candidate = checked_recheck
    controller.lane_available = checked_lane_available
    governor.store.capacity_remaining = checked_capacity_remaining
    governor.reserve_slot = checked_reserve
    governor.launch = checked_launch
    try:
        launches = drainer.drain_once()
        assert [row.work_id for row in launches] == [item.id]
        _wait_for_path(ready)
        assert ("provider-cap", False) in events
        assert ("provider-cap", True) in events
        assert events.index(("eligibility", True)) < events.index(("lane-ceiling", True))
        assert events.index(("lane-ceiling", True)) < events.index(("reserve", True))
        assert events.index(("reserve", True)) < events.index(("spawn", True))
    finally:
        exit_flag.write_text("done\n", encoding="utf-8")
        time.sleep(0.2)
        for record in governor.store.list_sessions():
            governor.reap_session(record["slot_id"], terminate=True)
        drainer.stop()


def test_draining_enforces_repo_lane_ceiling_and_supports_synthetic_stop_reconcile_restart(tmp_path):
    home, alpha, _beta, _locked, _broken = _seed_home(tmp_path, drain_repos=["alpha-repo"], lane_limit=1)
    _seed_spec(alpha, "core")
    _seed_spec(alpha, "followup")
    first = _enqueue(alpha, "core", priority=20)
    second = _enqueue(alpha, "followup", priority=10)
    command, ready, exit_flag = _provider_command(tmp_path)
    governor = BuilderGovernor(home)
    drainer = FederatedDrainer(home, owner_id="central-1", governor=governor, command_builder=lambda _candidate: command)

    drainer.start()
    try:
        launches = drainer.drain_once()
        assert [row.work_id for row in launches] == [first.id]
        _wait_for_path(ready)
        again = drainer.drain_once()
        assert again == []

        drainer.stop()
        for record in governor.store.list_sessions():
            record["owner_pid"] = 999999
            record["owner_pid_start_ticks"] = 1
            governor.store.write_session(record)
        exit_flag.write_text("done\n", encoding="utf-8")
        time.sleep(0.2)
        actions = governor.reconcile_startup()
        assert actions
        store = QueueStore(runtime_dir(alpha) / "dispatch-queue")
        first_item = store.get_item(first.id)
        assert first_item is not None
        first_item.state = first_item.state.SUCCEEDED
        first_item.lease = {}
        store.save_item(first_item)
        restarted = FederatedDrainer(home, owner_id="central-2", governor=BuilderGovernor(home), command_builder=lambda _candidate: command)
        restarted.start()
        try:
            post = restarted.drain_once()
            assert [row.work_id for row in post] == [second.id]
        finally:
            restarted.stop()
    finally:
        for record in governor.store.list_sessions():
            governor.reap_session(record["slot_id"], terminate=True)
        if ready.exists():
            try:
                pgid = json.loads(ready.read_text(encoding="utf-8"))["pgid"]
                if _pgid_group_alive(pgid):
                    _reap_group(pgid)
            except Exception:
                pass
