"""Safety contract tests for ``isanna init``."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import sys
from pathlib import Path

from _yaml import yaml

from _dispatch_runtime.config import load_dispatch_config


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


def _load(script: str, name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


isanna = _load("isanna.py", "isanna_init_cli_under_test")


def _run(*argv: str) -> tuple[int, str]:
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        code = isanna.main(["init", *argv])
    return code, output.getvalue()


def _files(root: Path) -> dict[str, bytes]:
    return {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()}


def test_init_is_idempotent(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    assert _run("--target", str(repo))[0] == 0
    before = _files(repo)
    code, output = _run("--target", str(repo))
    assert code == 0
    assert _files(repo) == before
    assert "No changes" in output


def test_init_dry_run_writes_nothing(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    code, output = _run("--target", str(repo), "--dry-run")
    assert code == 0
    assert _files(repo) == {}
    assert "CREATE" in output and ".builder/dispatch.yaml" in output


def test_init_warns_but_proceeds_outside_a_git_repo(tmp_path: Path):
    repo = tmp_path / "not-a-repo"
    repo.mkdir()
    code, output = _run("--target", str(repo))
    assert code == 0
    assert "not a git repository; proceeding anyway" in output
    assert (repo / ".builder" / "dispatch.yaml").is_file()


def test_init_points_to_install_when_the_slash_workflow_is_absent(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    code, output = _run("--target", str(repo))
    assert code == 0
    assert "slash-command workflow is not installed" in output
    assert "install.sh --target" in output


def test_init_does_not_overwrite_files_without_force(tmp_path: Path):
    repo = tmp_path / "repo"
    dispatch = repo / ".builder" / "dispatch.yaml"
    dispatch.parent.mkdir(parents=True)
    dispatch.write_text("keep: this\n", encoding="utf-8")
    assert _run("--target", str(repo))[0] == 0
    assert dispatch.read_text(encoding="utf-8") == "keep: this\n"
    assert _run("--target", str(repo), "--force")[0] == 0
    assert "queue_store:" in dispatch.read_text(encoding="utf-8")


def test_init_never_touches_existing_dispatch_queue(tmp_path: Path):
    repo = tmp_path / "repo"
    queue = repo / ".builder" / "dispatch-queue"
    queue.mkdir(parents=True)
    state = queue / "live-state.yaml"
    state.write_text("state: live\n", encoding="utf-8")
    assert _run("--target", str(repo), "--force")[0] == 0
    assert state.read_text(encoding="utf-8") == "state: live\n"


def test_generated_dispatch_yaml_is_safe_and_uses_target_queue(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    assert _run("--target", str(repo))[0] == 0
    data = yaml.safe_load((repo / ".builder" / "dispatch.yaml").read_text(encoding="utf-8"))
    config = load_dispatch_config(repo / ".builder" / "dispatch.yaml")
    queue_path = Path(data["queue_store"]["path"])
    assert not queue_path.is_absolute()
    assert config.queue_store_path == (repo / ".builder" / "dispatch.yaml").resolve().parent / queue_path
    assert config.queue_store_path == repo.resolve() / ".builder" / "dispatch-queue"
    assert set(config.lanes) == {"claude", "codex"}
    assert config.pipeline["reviews"]["enabled"] is True
    assert data["pipeline"]["deliver"]["enabled"] is False
    assert "notify" not in data["pipeline"]


def test_init_creates_editable_versioned_gate_lane_policy(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    assert _run("--target", str(repo))[0] == 0
    policy_path = repo / ".builder" / "gate-lane-policy.yaml"
    policy = yaml.safe_load(policy_path.read_text(encoding="utf-8"))
    assert policy["version"]
    assert "migration" in policy["lane_c_surfaces"]


def test_init_only_makes_a_repo_drivable_not_autonomous(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    code, output = _run("--target", str(repo))
    assert code == 0
    assert "NOT autonomous" in output
    assert not list(repo.rglob("*.pid"))
    assert not list(repo.rglob("*.log"))
