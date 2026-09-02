"""Regression: the central-daemon watchdog must survive transient snapshot
errors instead of dying permanently with an uncaught traceback.

Before this fix, `CentralSupervisor.ensure_once()` raising (e.g. a missing/
invalid builder.yaml under --home) propagated straight out of the top-level
`while True` loop in scripts/central-daemon-watchdog.py, killing the watchdog
for good. It must instead print an `error:` line and, for --once, exit 2
without a traceback on stderr.
"""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WATCHDOG = ROOT / "scripts" / "central-daemon-watchdog.py"


def test_watchdog_once_survives_missing_home_snapshot(tmp_path: Path) -> None:
    # No builder.yaml at all under this --home -> ensure_once() raises inside
    # load_builder_home()/load_valid_snapshot() rather than returning cleanly.
    home = tmp_path / ".builder-home-no-builder-yaml"
    home.mkdir()

    result = subprocess.run(
        [sys.executable, str(WATCHDOG), "--home", str(home), "--once"],
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 2, (result.stdout, result.stderr)
    assert "error:" in result.stdout
    assert "Traceback" not in result.stderr
