from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import shlex
import sys
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

backlog_spec = importlib.util.spec_from_file_location("isanna_backlog_tending", SCRIPTS / "isanna_backlog.py")
backlog = importlib.util.module_from_spec(backlog_spec)
sys.modules["isanna_backlog_tending"] = backlog
backlog_spec.loader.exec_module(backlog)

isanna_spec = importlib.util.spec_from_file_location("isanna_backlog_tending_cli", SCRIPTS / "isanna.py")
isanna = importlib.util.module_from_spec(isanna_spec)
sys.modules["isanna_backlog_tending_cli"] = isanna
isanna_spec.loader.exec_module(isanna)

DAY = 86400


def _intent(
    root: Path,
    intent_id: str,
    *,
    status: str = "proposed",
    capabilities: list[tuple[str, str]] | None = None,
) -> Path:
    path = root / ".builder" / "intents" / intent_id / "intent.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    if capabilities:
        cap_block = "  capabilities:\n" + "".join(
            f"    - target: {target}\n      change: {change}\n" for target, change in capabilities
        )
    else:
        cap_block = "  capabilities: []\n"
    reason_line = "reason: not now\n" if status in {"rejected", "superseded"} else ""
    path.write_text(
        "artifact: intent-object\n"
        f"intent: {intent_id}\n"
        f"title: {intent_id} title\n"
        f"status: {status}\n"
        "problem: p\n"
        "why: w\n"
        f"{reason_line}"
        "success_criteria:\n"
        "  - id: sc-1\n"
        "    statement: s\n"
        "non_goals:\n"
        "  - n\n"
        "ssot_delta:\n"
        f"{cap_block}"
        "  behaviors: []\n"
        "  journeys: []\n"
        "specs: []\n",
        encoding="utf-8",
    )
    return path


def _release(root: Path, release_id: str, *, status: str, intents: list[str]) -> None:
    path = root / ".builder" / "releases" / f"{release_id}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    intent_lines = "".join(f"  - {intent_id}\n" for intent_id in intents)
    path.write_text(
        f"release: {release_id}\nproduct: demo\ntitle: {release_id}\nstatus: {status}\n"
        f"intents:\n{intent_lines}",
        encoding="utf-8",
    )


def _run(argv):
    out = io.StringIO()
    err = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = isanna.main(argv)
    return code, out.getvalue(), err.getvalue()


class _FakeControllingTTY:
    def __init__(self, response: str, *, is_tty: bool = True):
        self.response = response
        self.is_tty = is_tty

    def isatty(self):
        return self.is_tty

    def write(self, _text):
        return None

    def flush(self):
        return None

    def readline(self):
        return self.response + "\n"

    def close(self):
        return None


# --------------------------------------------------------------------------- T1: core module


def test_visible_backlog_orders_by_rank_then_id(tmp_path):
    repo = tmp_path / "repo"
    _intent(repo, "first")
    _intent(repo, "second")
    _intent(repo, "third")
    backlog.save_rank(repo, ["second", "first"])

    rows = backlog.visible_backlog(repo)

    assert [row.intent_id for row in rows] == ["second", "first", "third"]
    assert all(row.visible_state == "proposed" for row in rows)
    assert [row.rank for row in rows] == [1, 2, 3]


def test_rank_intent_writes_sidecar_only(tmp_path):
    repo = tmp_path / "repo"
    paths = {
        "alpha": _intent(repo, "alpha"),
        "first": _intent(repo, "first"),
        "zulu": _intent(repo, "zulu"),
    }
    originals = {intent_id: path.read_bytes() for intent_id, path in paths.items()}

    order = backlog.rank_intent(repo, "first", position=1)

    assert order == ["first", "alpha", "zulu"]
    assert backlog.load_rank(repo) == ["first", "alpha", "zulu"]
    for intent_id, path in paths.items():
        assert path.read_bytes() == originals[intent_id]


def test_rank_intent_refuses_unknown_id(tmp_path):
    repo = tmp_path / "repo"
    _intent(repo, "first")
    rank_path = repo / ".builder" / "intents" / "backlog-rank.yaml"
    assert not rank_path.is_file()

    try:
        backlog.rank_intent(repo, "nope", position=1)
    except backlog.BacklogRefusal:
        pass
    else:
        raise AssertionError("expected BacklogRefusal for unknown intent id")
    assert not rank_path.is_file()

    try:
        backlog.rank_intent(repo, "first", position=5)
    except backlog.BacklogRefusal:
        pass
    else:
        raise AssertionError("expected BacklogRefusal for out-of-range position")
    assert not rank_path.is_file()


def test_visible_backlog_refuses_malformed_intent_instead_of_hiding_it(tmp_path):
    repo = tmp_path / "repo"
    _intent(repo, "valid")
    malformed = _intent(repo, "malformed")
    malformed.write_text("artifact: intent-object\nintent: malformed\n", encoding="utf-8")

    try:
        backlog.visible_backlog(repo)
    except backlog.BacklogError as exc:
        assert "malformed" in str(exc)
    else:
        raise AssertionError("expected malformed inventory to be refused")


def test_initial_rank_write_uses_atomic_writer(tmp_path):
    repo = tmp_path / "repo"
    _intent(repo, "first")

    with patch.object(backlog, "atomic_write_bytes", wraps=backlog.atomic_write_bytes) as atomic_write:
        backlog.rank_intent(repo, "first", position=1)

    atomic_write.assert_called_once()


def test_rank_intent_refuses_malformed_sidecar_without_overwrite(tmp_path):
    repo = tmp_path / "repo"
    _intent(repo, "first")
    rank_path = repo / ".builder" / "intents" / "backlog-rank.yaml"
    rank_path.write_text("artifact: wrong\norder: first\n", encoding="utf-8")
    original = rank_path.read_bytes()

    try:
        backlog.rank_intent(repo, "first", position=1)
    except backlog.BacklogError as exc:
        assert "backlog-rank" in str(exc)
    else:
        raise AssertionError("expected malformed rank sidecar to be refused")

    assert rank_path.read_bytes() == original


# --------------------------------------------------------------------------- T2: list / rank CLI


def test_backlog_list_prints_state_and_rank(tmp_path):
    repo = tmp_path / "repo"
    _intent(repo, "first")
    _intent(repo, "second")
    backlog.save_rank(repo, ["second", "first"])

    code, out, err = _run(["backlog", "list", "--root", str(repo)])

    assert code == 0, err
    lines = out.strip().splitlines()
    assert lines == ["second proposed rank=1", "first proposed rank=2"]


def test_backlog_list_filters_by_state(tmp_path):
    repo = tmp_path / "repo"
    _intent(repo, "prop", status="proposed")
    _intent(repo, "done", status="rejected")

    code, out, err = _run(["backlog", "list", "--root", str(repo), "--state", "proposed"])

    assert code == 0, err
    assert "prop" in out
    assert "done" not in out


def test_backlog_rank_cli_writes_sidecar(tmp_path):
    repo = tmp_path / "repo"
    paths = {
        "first": _intent(repo, "first"),
        "second": _intent(repo, "second"),
    }
    originals = {intent_id: path.read_bytes() for intent_id, path in paths.items()}

    code, out, err = _run(["backlog", "rank", "--root", str(repo), "--id", "first", "--position", "1"])

    assert code == 0, err
    assert backlog.load_rank(repo)[0] == "first"
    for intent_id, path in paths.items():
        assert path.read_bytes() == originals[intent_id]


# --------------------------------------------------------------------------- T3: promote / retire


def test_promote_refuses_capability_collision(tmp_path):
    repo = tmp_path / "repo"
    _intent(repo, "first", capabilities=[("capability.shared", "create")])
    _intent(repo, "second", capabilities=[("capability.shared", "create")])
    _release(repo, "r1", status="active", intents=["first", "second"])
    original = (repo / ".builder" / "intents" / "first" / "intent.yaml").read_bytes()

    with patch.object(isanna, "cmd_intent", Mock(side_effect=AssertionError("must not reach gate"))):
        code, out, err = _run(["backlog", "promote", "--root", str(repo), "--id", "first"])

    assert code != 0
    assert "second" in err
    assert (repo / ".builder" / "intents" / "first" / "intent.yaml").read_bytes() == original


def test_promote_without_collision_reaches_gate(tmp_path):
    repo = tmp_path / "repo"
    _intent(repo, "first", capabilities=[("capability.solo", "create")])
    _release(repo, "r1", status="active", intents=["first"])

    with patch.object(isanna, "_open_controlling_tty", side_effect=OSError("no controlling terminal")):
        code, out, err = _run(["backlog", "promote", "--root", str(repo), "--id", "first"])

    assert code != 0
    assert "TTY required" in err
    assert "collides" not in err


def test_promote_refuses_when_collision_index_is_incomplete(tmp_path):
    repo = tmp_path / "repo"
    _intent(repo, "first", capabilities=[("capability.solo", "create")])
    _release(repo, "r1", status="active", intents=["first", "missing"])

    with patch.object(isanna, "cmd_intent", Mock(side_effect=AssertionError("must not reach gate"))):
        code, out, err = _run(["backlog", "promote", "--root", str(repo), "--id", "first"])

    assert code != 0
    assert "missing" in err


def test_retire_requires_reason_and_gate(tmp_path):
    repo = tmp_path / "repo"
    path = _intent(repo, "first")
    original = path.read_bytes()

    with patch.object(isanna, "_open_controlling_tty", return_value=_FakeControllingTTY("first rejected")):
        code, out, err = _run(["backlog", "retire", "--root", str(repo), "--id", "first"])

    assert code != 0
    assert "--reason is required" in err
    assert path.read_bytes() == original


# --------------------------------------------------------------------------- T4: garden review


def test_garden_review_surfaces_only_stale_proposed(tmp_path):
    repo = tmp_path / "repo"
    now_ts = 1_800_000_000.0
    fresh = _intent(repo, "fresh")
    old_proposed = _intent(repo, "old-proposed")
    old_accepted = _intent(repo, "old-accepted", status="accepted")
    os.utime(fresh, (now_ts - 1 * DAY, now_ts - 1 * DAY))
    os.utime(old_proposed, (now_ts - 40 * DAY, now_ts - 40 * DAY))
    os.utime(old_accepted, (now_ts - 40 * DAY, now_ts - 40 * DAY))

    report = backlog.garden_review(repo, stale_days=30, now_ts=now_ts)

    assert [row.intent_id for row in report] == ["old-proposed"]


def test_garden_review_annotates_rank_and_collision(tmp_path):
    repo = tmp_path / "repo"
    now_ts = 1_800_000_000.0
    old = _intent(repo, "old", capabilities=[("capability.shared", "create")])
    other = _intent(repo, "other", capabilities=[("capability.shared", "create")])
    _release(repo, "r1", status="active", intents=["old", "other"])
    os.utime(old, (now_ts - 40 * DAY, now_ts - 40 * DAY))
    os.utime(other, (now_ts - 1 * DAY, now_ts - 1 * DAY))
    backlog.save_rank(repo, ["old", "other"])

    report = backlog.garden_review(repo, stale_days=30, now_ts=now_ts)

    assert len(report) == 1
    row = report[0]
    assert row.intent_id == "old"
    assert row.rank == 1
    assert row.age_days == 40
    assert row.collisions == ("other",)
    assert any("backlog promote" in cmd and "old" in cmd for cmd in row.commands)
    assert any("backlog rank" in cmd and "old" in cmd for cmd in row.commands)
    assert any("backlog retire" in cmd and "old" in cmd for cmd in row.commands)


def test_garden_review_mutates_nothing(tmp_path):
    repo = tmp_path / "repo"
    now_ts = 1_800_000_000.0
    old = _intent(repo, "old")
    fresh = _intent(repo, "fresh")
    os.utime(old, (now_ts - 40 * DAY, now_ts - 40 * DAY))
    rank_path = repo / ".builder" / "intents" / "backlog-rank.yaml"
    snapshots = {"old": old.read_bytes(), "fresh": fresh.read_bytes()}
    assert not rank_path.is_file()

    backlog.garden_review(repo, stale_days=30, now_ts=now_ts)

    assert old.read_bytes() == snapshots["old"]
    assert fresh.read_bytes() == snapshots["fresh"]
    assert not rank_path.is_file()


def test_garden_review_refuses_when_collision_index_is_incomplete(tmp_path):
    repo = tmp_path / "repo"
    now_ts = 1_800_000_000.0
    old = _intent(repo, "old", capabilities=[("capability.solo", "create")])
    _release(repo, "r1", status="active", intents=["old", "missing"])
    os.utime(old, (now_ts - 40 * DAY, now_ts - 40 * DAY))

    try:
        backlog.garden_review(repo, stale_days=30, now_ts=now_ts)
    except backlog.BacklogError as exc:
        assert "missing" in str(exc)
    else:
        raise AssertionError("expected incomplete collision index to be refused")


def test_garden_review_commands_quote_root_with_spaces(tmp_path):
    repo = tmp_path / "repo with spaces"
    now_ts = 1_800_000_000.0
    old = _intent(repo, "old")
    os.utime(old, (now_ts - 40 * DAY, now_ts - 40 * DAY))

    row = backlog.garden_review(repo, stale_days=30, now_ts=now_ts)[0]

    for command in row.commands:
        argv = shlex.split(command)
        assert argv[argv.index("--root") + 1] == str(repo)
    retire = next(command for command in row.commands if " backlog retire " in command)
    assert "--reason '<reason>'" in retire
