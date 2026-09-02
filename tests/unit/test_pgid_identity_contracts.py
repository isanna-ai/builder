from __future__ import annotations

import json
import os
import signal
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
FIXTURES = ROOT / "tests" / "fixtures" / "builder_project_model" / "identity" / "v1"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from _dispatch_runtime.lane_common import sweep_orphan_pgids


class _Killer:
    def __init__(self):
        self.killed: list[int] = []

    def __call__(self, pid, sig):
        if sig == signal.SIGKILL:
            self.killed.append(pid)


def _identity(mapping):
    return lambda pgid: mapping.get(pgid)


def _seed(path: Path, fixture_name: str, *, target_name: str = "100") -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / target_name).write_text((FIXTURES / fixture_name).read_text(encoding="utf-8"), encoding="utf-8")


def _with_env(on: bool, fn):
    key = "BUILDER_ORPHAN_SWEEP"
    saved = os.environ.get(key)
    if on:
        os.environ[key] = "1"
    else:
        os.environ.pop(key, None)
    try:
        return fn()
    finally:
        if saved is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = saved


def test_identity_fixture_shape_matches_live_pgid_contract():
    good = json.loads((FIXTURES / "live-pgid-good.json").read_text(encoding="utf-8"))
    assert sorted(good) == ["cmdline", "pgid", "starttime"]
    assert good["pgid"] > 1
    assert good["starttime"] == "555"


def test_sweep_kills_only_when_recorded_identity_still_matches_and_looks_like_an_agent(tmp_path):
    d = tmp_path / "live-pgids"
    _seed(d, "live-pgid-good.json")
    killer = _Killer()

    killed = _with_env(True, lambda: sweep_orphan_pgids(d, killer=killer, identity=_identity({100: ("555", "claude -p goal")})))

    assert killed == [100]
    assert killer.killed == [100]
    assert not (d / "100").exists()


def test_sweep_fails_closed_for_empty_starttime_corrupt_records_and_non_agent_cmdlines(tmp_path):
    d = tmp_path / "live-pgids"
    killer = _Killer()

    _seed(d, "live-pgid-empty-starttime.json", target_name="100")
    _seed(d, "live-pgid-nonmapping.json", target_name="200")
    _seed(d, "live-pgid-nonagent.json", target_name="300")

    killed = _with_env(
        True,
        lambda: sweep_orphan_pgids(
            d,
            killer=killer,
            identity=_identity({
                100: ("555", "claude -p goal"),
                200: ("1", "claude -p goal"),
                300: ("555", "/usr/bin/postgres"),
            }),
        ),
    )

    assert killed == []
    assert killer.killed == []
    assert not (d / "100").exists()
    assert not (d / "200").exists()
    assert not (d / "300").exists()


def test_sweep_is_disabled_by_default(tmp_path):
    d = tmp_path / "live-pgids"
    _seed(d, "live-pgid-good.json")
    killer = _Killer()

    killed = _with_env(False, lambda: sweep_orphan_pgids(d, killer=killer, identity=_identity({100: ("555", "claude -p goal")})))

    assert killed == []
    assert killer.killed == []
    assert (d / "100").exists()
