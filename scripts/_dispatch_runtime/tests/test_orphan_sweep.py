"""R12: startup orphan sweep kills agent groups left alive by a SIGKILLed predecessor
daemon — but ONLY a live process whose recorded identity still matches and looks like an
agent, so it never kills the daemon's own group (pgid<=1), a pid-reuse victim, or an
unrelated process. OPT-IN (default OFF). Shim-safe (no pytest.raises/monkeypatch)."""

from __future__ import annotations

import json
import os
import signal
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from _dispatch_runtime.lane_common import sweep_orphan_pgids

_ENV = "BUILDER_ORPHAN_SWEEP"


class _Killer:
    """Records SIGKILL targets; a real os.killpg would be called with (pgid, SIGKILL)."""

    def __init__(self):
        self.killed: list[int] = []

    def __call__(self, pid, sig):
        if sig == signal.SIGKILL:
            self.killed.append(pid)


def _identity(mapping):
    """identity(pgid) -> (starttime, cmdline) for live pgids in `mapping`, else None."""
    return lambda pgid: mapping.get(pgid)


def _seed(pgid_dir, records):
    pgid_dir.mkdir(parents=True, exist_ok=True)
    for name, rec in records.items():
        (pgid_dir / name).write_text("" if rec is None else json.dumps(rec), encoding="utf-8")


def _sweep(pgid_dir, *, on, **kw):
    saved = os.environ.get(_ENV)
    if on:
        os.environ[_ENV] = "1"
    else:
        os.environ.pop(_ENV, None)
    try:
        return sweep_orphan_pgids(pgid_dir, **kw)
    finally:
        if saved is None:
            os.environ.pop(_ENV, None)
        else:
            os.environ[_ENV] = saved


def test_sweep_kills_verified_orphan(tmp_path):
    d = tmp_path / "live-pgids"
    _seed(d, {"100": {"pgid": 100, "starttime": "555", "cmdline": "claude -p goal"}})
    killer = _Killer()
    killed = _sweep(d, on=True, killer=killer, identity=_identity({100: ("555", "claude -p goal")}))
    assert killed == [100]
    assert killer.killed == [100]
    assert not (d / "100").exists()  # registry cleared


def test_sweep_skips_pid_reuse_on_starttime_mismatch(tmp_path):
    d = tmp_path / "live-pgids"
    _seed(d, {"100": {"pgid": 100, "starttime": "555", "cmdline": "claude"}})
    killer = _Killer()
    # Live pid 100 exists but with a DIFFERENT start-time -> the pid was recycled.
    killed = _sweep(d, on=True, killer=killer, identity=_identity({100: ("999", "claude")}))
    assert killed == []
    assert killer.killed == []
    assert not (d / "100").exists()  # stale entry still cleaned


def test_sweep_skips_non_agent_process(tmp_path):
    d = tmp_path / "live-pgids"
    _seed(d, {"100": {"pgid": 100, "starttime": "555", "cmdline": "claude"}})
    killer = _Killer()
    killed = _sweep(d, on=True, killer=killer, identity=_identity({100: ("555", "/usr/bin/postgres")}))
    assert killed == []  # start matches but cmdline is not an agent


def test_sweep_skips_dead_group(tmp_path):
    d = tmp_path / "live-pgids"
    _seed(d, {"200": {"pgid": 200, "starttime": "1", "cmdline": "codex"}})
    killer = _Killer()
    killed = _sweep(d, on=True, killer=killer, identity=_identity({}))  # 200 is gone
    assert killed == []
    assert not (d / "200").exists()


def test_sweep_never_targets_pgid_le_1(tmp_path):
    d = tmp_path / "live-pgids"
    _seed(d, {
        "0": {"pgid": 0, "starttime": "1", "cmdline": "claude"},
        "1": {"pgid": 1, "starttime": "1", "cmdline": "claude"},
        "junk": None,
    })
    killer = _Killer()
    killed = _sweep(d, on=True, killer=killer, identity=_identity({0: ("1", "claude"), 1: ("1", "claude")}))
    assert killed == []           # killpg(0/1) would hit the caller / broadcast — never
    assert killer.killed == []


def test_sweep_disabled_by_default(tmp_path):
    d = tmp_path / "live-pgids"
    _seed(d, {"100": {"pgid": 100, "starttime": "555", "cmdline": "claude"}})
    killer = _Killer()
    killed = _sweep(d, on=False, killer=killer, identity=_identity({100: ("555", "claude")}))
    assert killed == []
    assert killer.killed == []
    assert (d / "100").exists()   # untouched when disabled


def test_sweep_fails_closed_on_empty_recorded_starttime(tmp_path):
    # An identity-less legacy record must NOT authorize a kill (fail closed).
    d = tmp_path / "live-pgids"
    _seed(d, {"100": {"pgid": 100, "starttime": "", "cmdline": "claude"}})
    killer = _Killer()
    killed = _sweep(d, on=True, killer=killer, identity=_identity({100: ("555", "claude")}))
    assert killed == []
    assert killer.killed == []


def test_sweep_ignores_non_mapping_record(tmp_path):
    # A corrupt (non-mapping) record must be ignored, not crash the sweep.
    d = tmp_path / "live-pgids"
    d.mkdir(parents=True, exist_ok=True)
    (d / "300").write_text("[]", encoding="utf-8")  # JSON list, not an object
    killer = _Killer()
    killed = _sweep(d, on=True, killer=killer, identity=_identity({300: ("1", "claude")}))
    assert killed == []
    assert not (d / "300").exists()


def test_sweep_noop_on_missing_dir(tmp_path):
    assert _sweep(tmp_path / "does-not-exist", on=True) == []
