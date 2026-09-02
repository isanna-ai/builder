from __future__ import annotations

import json
import os
import signal
import sys
import textwrap
import time
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
FIXTURES = ROOT / "tests" / "fixtures" / "builder_project_model" / "declarations" / "v1"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from _builder_project_model.governor import BuilderGovernor, current_pid_start_ticks, governor_enabled
from _builder_project_model.home import load_builder_home
from _builder_project_model.launcher_shim import command_digest
from _builder_project_model.session_store import SessionStore
from _dispatch_runtime.lane_common import _pgid_group_alive, _reap_group


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _repo(root: Path, name: str) -> Path:
    repo = root / name
    (repo / ".git").mkdir(parents=True)
    return repo


def _policy_yaml(*, enabled: bool, claude: int = 1, codex: int = 1) -> str:
    flag = "true" if enabled else "false"
    return textwrap.dedent(
        f"""\
        schema_version: 1
        governor:
          enabled: {flag}
        providers:
          claude-code-cli:
            max_sessions: {claude}
            quota_cooldown:
              initial_seconds: 300
              max_seconds: 3600
          codex-cli:
            max_sessions: {codex}
            quota_cooldown:
              initial_seconds: 300
              max_seconds: 3600
        allocation:
          policy: equal-weight-fair-share
          project_weight: 1
        scheduler:
          poll_seconds: 2
          heartbeat_seconds: 5
          stale_daemon_seconds: 30
        """
    )


def _seed_home(tmp_path: Path, *, enabled: bool, claude: int = 1, codex: int = 1):
    home = tmp_path / ".builder-home"
    repo = _repo(tmp_path, "hivemind-cloud")
    _repo(tmp_path, "sharedlib")
    queue_root = repo / ".builder" / "dispatch-queue"
    (queue_root / "queue").mkdir(parents=True, exist_ok=True)
    _write(home / "builder.yaml", _fixture("builder-good.yaml"))
    _write(home / "repositories.yaml", _fixture("repositories-good.yaml"))
    _write(home / "policy.yaml", _policy_yaml(enabled=enabled, claude=claude, codex=codex))
    _write(home / "projects" / "bia" / "product.yaml", _fixture("product-good.yaml"))
    _write(home / "projects" / "bia" / "releases" / "bia-audit-remediation.yaml", _fixture("release-good.yaml"))
    return load_builder_home(home), repo, queue_root


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
            parser.add_argument("--child-ready", required=True)
            args = parser.parse_args()

            child = os.fork()
            if child == 0:
                Path(args.child_ready).write_text(json.dumps({"pid": os.getpid(), "pgid": os.getpgrp()}), encoding="utf-8")
                while not Path(args.exit_flag).exists():
                    time.sleep(0.05)
                raise SystemExit(0)

            Path(args.ready).write_text(json.dumps({"pid": os.getpid(), "pgid": os.getpgrp(), "child": child}), encoding="utf-8")
            while not Path(args.exit_flag).exists():
                time.sleep(0.05)
            os.waitpid(child, 0)
            """
        ),
    )
    return script


def _provider_command(tmp_path: Path) -> tuple[list[str], Path, Path, Path]:
    ready = tmp_path / "provider-ready.json"
    exit_flag = tmp_path / "provider-exit"
    child_ready = tmp_path / "provider-child-ready.json"
    command = [
        sys.executable,
        str(_provider_script(tmp_path)),
        "--ready",
        str(ready),
        "--exit-flag",
        str(exit_flag),
        "--child-ready",
        str(child_ready),
    ]
    return command, ready, exit_flag, child_ready


def _reserve(governor: BuilderGovernor, *, provider: str = "claude-code-cli"):
    return governor.reserve_slot(
        provider=provider,
        repo_id="hivemind-cloud",
        queue_root=Path("/tmp/queue-root"),
        work_id=f"work-{provider}",
        attempt_id=f"attempt-{provider}",
        lane="claude" if provider == "claude-code-cli" else "codex",
        project_attribution="bia",
        release_name="bia-audit-remediation",
    )


def _mark_owner_dead(store: SessionStore, slot_id: str) -> None:
    record = store.load_session(slot_id)
    record["owner_pid"] = 999999
    record["owner_pid_start_ticks"] = 1
    store.write_session(record)


def _eventually_close(governor: BuilderGovernor, slot_id: str, *, timeout_seconds: float = 5.0):
    deadline = time.monotonic() + timeout_seconds
    last = None
    while time.monotonic() < deadline:
        last = governor.reap_session(slot_id, terminate=False)
        if last.action == "closed":
            return last
        time.sleep(0.05)
    return last


def test_policy_activation_requires_home_and_explicit_flag(tmp_path):
    assert governor_enabled(None) is False

    home, _repo_root, _queue_root = _seed_home(tmp_path / "off", enabled=False)
    assert governor_enabled(home) is False

    home, _repo_root, _queue_root = _seed_home(tmp_path / "on", enabled=True)
    assert governor_enabled(home) is True


def test_reservation_crash_before_write_does_not_consume_capacity_or_oversubscribe(tmp_path):
    home, _repo_root, _queue_root = _seed_home(tmp_path, enabled=True, claude=1, codex=1)
    governor = BuilderGovernor(home)
    store = governor.store

    def crash(phase, _record):
        if phase == "before_reservation_write":
            raise RuntimeError("boom")

    try:
        _reserve(governor)
    except Exception as exc:  # pragma: no cover - guard against helper regression
        raise AssertionError(f"unexpected reservation failure: {exc}") from exc

    assert len(store.consuming_sessions("claude-code-cli")) == 1
    store.close_session(store.list_sessions()[0]["slot_id"])

    try:
        governor.reserve_slot(
            provider="claude-code-cli",
            repo_id="hivemind-cloud",
            queue_root=Path("/tmp/queue-root"),
            work_id="work-crash",
            attempt_id="attempt-crash",
            lane="claude",
            project_attribution="bia",
            release_name="bia-audit-remediation",
            crash_hook=crash,
        )
    except RuntimeError:
        pass
    else:
        raise AssertionError("expected injected crash")

    assert store.consuming_sessions("claude-code-cli") == []
    replacement = _reserve(governor)
    assert replacement.provider == "claude-code-cli"


def test_starting_reservation_exists_before_launcher_spawn(tmp_path):
    home, _repo_root, _queue_root = _seed_home(tmp_path, enabled=True)
    governor = BuilderGovernor(home)
    reservation = _reserve(governor)
    observed = []

    class _RunningLauncher:
        def poll(self):
            return None

    def observe_spawn(*, home_root, slot_id, command, crash_phase):
        record = governor.store.load_session(slot_id)
        observed.append((home_root, slot_id, command, crash_phase, record["state"]))
        governor.store.update_state(
            slot_id,
            state="active",
            previous_state="starting",
            pgid=1234,
            pgid_leader_start_ticks=1,
            executable=command[0],
            command_digest=command_digest(command),
        )
        return _RunningLauncher()

    command = ["/synthetic/provider", "--once"]
    with patch("_builder_project_model.governor.spawn_launcher", side_effect=observe_spawn):
        governor.launch(reservation, command=command)

    assert observed == [(home.root, reservation.slot_id, command, None, "starting")]


def test_provider_caps_are_independent_and_starting_active_reaping_all_consume_slots(tmp_path):
    home, _repo_root, _queue_root = _seed_home(tmp_path, enabled=True, claude=1, codex=1)
    governor = BuilderGovernor(home)
    store = governor.store

    reservation = _reserve(governor, provider="claude-code-cli")
    assert store.capacity_remaining("claude-code-cli", max_sessions=1) == 0
    assert store.capacity_remaining("codex-cli", max_sessions=1) == 1

    try:
        _reserve(governor, provider="claude-code-cli")
    except RuntimeError:
        pass
    else:
        raise AssertionError("expected claude cap exhaustion")

    codex = _reserve(governor, provider="codex-cli")
    assert codex.provider == "codex-cli"

    store.update_state(reservation.slot_id, state="active", previous_state="starting", pgid=1234, pgid_leader_start_ticks=1)
    assert store.capacity_remaining("claude-code-cli", max_sessions=1) == 0
    store.update_state(reservation.slot_id, state="reaping", previous_state="active", pgid=1234, pgid_leader_start_ticks=1)
    assert store.capacity_remaining("claude-code-cli", max_sessions=1) == 0


def test_launcher_shim_before_pgid_write_closes_only_after_launcher_proves_no_child(tmp_path):
    home, _repo_root, _queue_root = _seed_home(tmp_path, enabled=True)
    governor = BuilderGovernor(home)
    reservation = _reserve(governor)
    command, _ready, _exit_flag, _child_ready = _provider_command(tmp_path)

    proc = governor.launch(reservation, command=command, crash_phase="before-pgid-write", timeout_seconds=0.5)
    proc.wait(timeout=5)
    _mark_owner_dead(governor.store, reservation.slot_id)

    actions = governor.reconcile_startup()
    assert [(row.action, row.detail) for row in actions] == [("closed", "starting-no-pgid-proven-no-child")]
    assert governor.store.list_sessions() == []


def test_launcher_shim_after_pgid_write_closes_when_recorded_group_is_already_gone(tmp_path):
    home, _repo_root, _queue_root = _seed_home(tmp_path, enabled=True)
    governor = BuilderGovernor(home)
    reservation = _reserve(governor)
    command, _ready, _exit_flag, _child_ready = _provider_command(tmp_path)

    proc = governor.launch(reservation, command=command, crash_phase="after-pgid-write", timeout_seconds=2.0)
    proc.wait(timeout=5)
    _mark_owner_dead(governor.store, reservation.slot_id)

    actions = governor.reconcile_startup()
    assert [(row.action, row.detail) for row in actions] == [("closed", "recorded-group-already-gone")]
    assert governor.store.list_sessions() == []


def test_launcher_shim_persists_complete_group_identity_before_exec(tmp_path):
    home, _repo_root, _queue_root = _seed_home(tmp_path, enabled=True)
    governor = BuilderGovernor(home)
    reservation = _reserve(governor)
    command, _ready, _exit_flag, _child_ready = _provider_command(tmp_path)

    proc = governor.launch(reservation, command=command, crash_phase="after-pgid-write", timeout_seconds=2.0)
    assert proc.wait(timeout=5) == 93

    record = governor.store.load_session(reservation.slot_id)
    assert record["state"] == "active"
    assert record["pgid"] == proc.pid
    assert record["pgid_leader_start_ticks"] > 0
    assert record["executable"] == command[0]
    assert record["command_digest"] == command_digest(command)
    assert governor.store.read_launcher_state(reservation.slot_id)["phase"] == "pgid-recorded"


def test_reaping_consumes_slot_until_group_is_gone(tmp_path):
    home, _repo_root, _queue_root = _seed_home(tmp_path, enabled=True, claude=1)
    governor = BuilderGovernor(home)
    reservation = _reserve(governor)
    command, ready, exit_flag, child_ready = _provider_command(tmp_path)

    proc = governor.launch(reservation, command=command)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and (not ready.exists() or not child_ready.exists()):
        time.sleep(0.05)
    assert ready.exists()
    assert child_ready.exists()
    assert governor.store.capacity_remaining("claude-code-cli", max_sessions=1) == 0

    governor.begin_reaping(reservation.slot_id)
    assert governor.store.capacity_remaining("claude-code-cli", max_sessions=1) == 0

    exit_flag.write_text("done\n", encoding="utf-8")
    result = governor.reap_session(reservation.slot_id, terminate=False)
    assert result.action == "reaping"

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and _pgid_group_alive(proc.pid):
        time.sleep(0.05)
    result = _eventually_close(governor, reservation.slot_id)
    assert (result.action, result.detail) == ("closed", "group-already-gone")
    assert governor.store.capacity_remaining("claude-code-cli", max_sessions=1) == 1


def test_reconciliation_refuses_live_dual_owner_for_starting_session(tmp_path):
    home, _repo_root, _queue_root = _seed_home(tmp_path, enabled=True)
    governor = BuilderGovernor(home, owner_pid=os.getpid())
    reservation = _reserve(governor)

    actions = governor.reconcile_startup()
    assert [(row.slot_id, row.action) for row in actions] == [(reservation.slot_id, "dual-owner-refused")]
    assert governor.store.load_session(reservation.slot_id)["state"] == "starting"


def test_reconciliation_quarantines_unproven_starting_session_and_live_identity_mismatch(tmp_path):
    home, _repo_root, _queue_root = _seed_home(tmp_path, enabled=True, claude=2)
    governor = BuilderGovernor(home)
    reservation = _reserve(governor)
    _mark_owner_dead(governor.store, reservation.slot_id)
    actions = governor.reconcile_startup()
    assert [(row.action, row.detail) for row in actions] == [("quarantine", "starting-without-launcher-proof")]

    reservation = _reserve(governor)
    command, ready, _exit_flag, child_ready = _provider_command(tmp_path / "mismatch")
    proc = governor.launch(reservation, command=command)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and (not ready.exists() or not child_ready.exists()):
        time.sleep(0.05)
    record = governor.store.load_session(reservation.slot_id)
    record["command_digest"] = "sha256:bad"
    record["owner_pid"] = 999999
    record["owner_pid_start_ticks"] = 1
    governor.store.write_session(record)
    try:
        actions = governor.reconcile_startup()
        assert any(row.action == "quarantine" and row.detail == "pgid-identity-mismatch" for row in actions)
        assert governor.store.load_session(reservation.slot_id)["slot_id"] == reservation.slot_id
    finally:
        _reap_group(proc.pid)
        governor.store.close_session(reservation.slot_id)


def test_reconciliation_never_expires_stale_live_slot_and_unsafe_pgid_is_quarantined(tmp_path):
    home, _repo_root, _queue_root = _seed_home(tmp_path, enabled=True)
    governor = BuilderGovernor(home, owner_pid=os.getpid())
    reservation = _reserve(governor)
    record = governor.store.load_session(reservation.slot_id)
    record["reserved_at"] = "2000-01-01T00:00:00Z"
    record["updated_at"] = "2000-01-01T00:00:00Z"
    governor.store.write_session(record)

    actions = governor.reconcile_startup()
    assert [(row.action, row.detail) for row in actions] == [("dual-owner-refused", "recorded-daemon-instance-live")]
    assert governor.store.load_session(reservation.slot_id)["state"] == "starting"

    unsafe = dict(record, pgid=1)
    with patch.object(governor.store, "load_session", return_value=unsafe):
        result = governor.reap_session(reservation.slot_id)
    assert (result.action, result.detail) == ("quarantine", "missing-or-unsafe-pgid")


def test_reconciliation_reaps_matching_live_recorded_group(tmp_path):
    home, _repo_root, _queue_root = _seed_home(tmp_path, enabled=True)
    governor = BuilderGovernor(home)
    reservation = _reserve(governor)
    command, ready, _exit_flag, child_ready = _provider_command(tmp_path)
    proc = governor.launch(reservation, command=command)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and (not ready.exists() or not child_ready.exists()):
        time.sleep(0.05)
    _mark_owner_dead(governor.store, reservation.slot_id)

    actions = governor.reconcile_startup()
    assert actions[0].action in {"closed", "reaping"}
    if actions[0].action == "reaping":
        proc.wait(timeout=5)
        result = _eventually_close(governor, reservation.slot_id)
        assert result.action == "closed"
    else:
        assert actions[0].detail == "group-reaped"
    assert not _pgid_group_alive(proc.pid)
    assert governor.store.list_sessions() == []


def test_only_quota_style_failures_open_provider_global_cooldown(tmp_path):
    home, _repo_root, _queue_root = _seed_home(tmp_path, enabled=True)
    governor = BuilderGovernor(home)
    store = governor.store

    opened = governor.open_provider_cooldown(
        "claude-code-cli",
        reason_class="quota",
        source_repo_id="hivemind-cloud",
        source_attempt_id="attempt-1",
        cooldown_until="2026-07-17T01:00:00Z",
    )
    assert opened is True
    record = store.read_provider("claude-code-cli")
    assert record["reason_class"] == "quota"

    opened = governor.open_provider_cooldown(
        "claude-code-cli",
        reason_class="auth",
        source_repo_id="hivemind-cloud",
        source_attempt_id="attempt-2",
        cooldown_until="2026-07-17T02:00:00Z",
    )
    assert opened is False
    record = store.read_provider("claude-code-cli")
    assert record["source_attempt_id"] == "attempt-1"


def test_subscription_quota_and_rate_limit_are_the_only_provider_global_cooldown_classes(tmp_path):
    home, _repo_root, _queue_root = _seed_home(tmp_path, enabled=True)
    governor = BuilderGovernor(home)

    for reason_class in ("subscription", "quota", "rate-limit"):
        assert governor.open_provider_cooldown(
            "claude-code-cli",
            reason_class=reason_class,
            source_repo_id="hivemind-cloud",
            source_attempt_id=f"attempt-{reason_class}",
            cooldown_until="2026-07-17T01:00:00Z",
        ) is True
        assert governor.store.read_provider("claude-code-cli")["reason_class"] == reason_class

    before = governor.store.read_provider("claude-code-cli")
    for reason_class in ("auth", "prompt", "tool", "test", "malformed-artifact", "delivery", "unknown"):
        assert governor.open_provider_cooldown(
            "claude-code-cli",
            reason_class=reason_class,
            source_repo_id="sharedlib",
            source_attempt_id=f"attempt-{reason_class}",
            cooldown_until="2026-07-17T02:00:00Z",
        ) is False
        assert governor.store.read_provider("claude-code-cli") == before


def test_current_pid_start_ticks_matches_proc_identity_contract():
    with patch(
        "_builder_project_model.governor._proc_identity",
        return_value=(424242, "/synthetic/provider --once"),
    ) as proc_identity:
        assert current_pid_start_ticks(12345) == 424242
    proc_identity.assert_called_once_with(12345)

    with patch("_builder_project_model.governor._proc_identity", return_value=None):
        try:
            current_pid_start_ticks(12345)
        except RuntimeError as exc:
            assert str(exc) == "unable to read process identity for pid 12345"
        else:
            raise AssertionError("missing process identity must fail closed")
