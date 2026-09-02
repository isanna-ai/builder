from __future__ import annotations

import subprocess
import sys
import os
from pathlib import Path

from _dispatch_runtime.config import ConfigError, load_dispatch_config
from _dispatch_runtime.cli import build_parser


def write_config(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def valid_config_text() -> str:
    return """
queue_store:
  path: .builder/dispatch
lanes:
  - name: codex-cli
    provider: codex-cli
    secrets:
      api_key: ${CODEX_API_KEY}
  - name: claude-code-cli
    provider: claude-code-cli
routing_policy:
  default: ordered
cooldown_policy:
  default_seconds: 60
retry_policy:
  max_attempts: 3
"""


def assert_raises_config_error(expected: str, func) -> None:
    try:
        func()
    except ConfigError as exc:
        assert expected in str(exc)
        return
    raise AssertionError("ConfigError was not raised")


def test_config_loader_rejects_missing_config_file(tmp_path):
    def load_missing() -> None:
        load_dispatch_config(tmp_path / "missing.yaml")

    assert_raises_config_error("missing dispatch config", load_missing)


def test_config_loader_rejects_inline_secret_values(tmp_path):
    os.environ["CODEX_API_KEY"] = "resolved"
    config_path = write_config(
        tmp_path / "dispatch.yaml",
        valid_config_text().replace("${CODEX_API_KEY}", "sk-live-inline"),
    )

    def load_inline_secret() -> None:
        load_dispatch_config(config_path)

    assert_raises_config_error("inline secret", load_inline_secret)


def test_config_loader_errors_on_unset_env_reference(tmp_path):
    os.environ.pop("CODEX_API_KEY", None)
    config_path = write_config(tmp_path / "dispatch.yaml", valid_config_text())

    def load_unset_env() -> None:
        load_dispatch_config(config_path)

    assert_raises_config_error("CODEX_API_KEY", load_unset_env)


def test_config_loader_applies_defaults_and_keeps_secret_references(tmp_path):
    os.environ["CODEX_API_KEY"] = "resolved"
    config_path = write_config(tmp_path / "dispatch.yaml", valid_config_text())

    config = load_dispatch_config(config_path)

    assert config.queue_store_path == config_path.resolve().parent / ".builder/dispatch"
    assert config.lanes["codex-cli"].max_concurrency == 1
    assert config.cooldown_policy["default_seconds"] == 60
    assert config.retry_policy["initial_seconds"] == 30
    assert config.lanes["codex-cli"].secrets["api_key"].env_var == "CODEX_API_KEY"
    assert config.lanes["codex-cli"].secrets["api_key"].value == "resolved"


def test_config_loader_keeps_absolute_queue_store_path_unchanged(tmp_path):
    os.environ["CODEX_API_KEY"] = "resolved"
    queue_path = tmp_path / "queue"
    config_path = write_config(
        tmp_path / "dispatch.yaml",
        valid_config_text().replace(".builder/dispatch", str(queue_path)),
    )

    config = load_dispatch_config(config_path)

    assert config.queue_store_path == queue_path


def test_cli_help_exposes_operator_commands():
    help_text = build_parser().format_help()

    for command in ("enqueue", "status", "lanes", "cancel", "drain"):
        assert command in help_text


def test_script_help_exposes_operator_commands():
    result = subprocess.run(
        [sys.executable, "scripts/builder-dispatch.py", "--help"],
        cwd=Path(__file__).resolve().parents[2],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    for command in ("enqueue", "status", "lanes", "cancel", "drain"):
        assert command in result.stdout
