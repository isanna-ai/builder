"""With no controlling terminal and no --yes, the install must REFUSE clearly, not die raw.

`read ans </dev/tty` is correct and deliberate: the documented install is
`curl -fsSL ... | sh`, where stdin is the pipe carrying the script, so the confirmation has to
come from the terminal rather than from stdin. But `/dev/tty` only exists when there IS a
controlling terminal. In CI, in a container started without a TTY, under a systemd unit or
nohup, opening it fails -- and it used to fail raw, after printing the entire file plan:

    Proceed with install? [y/N] install.sh: 288: cannot open /dev/tty: No such device or address

rc=2, nothing installed, and nothing telling the user that `--yes` is the way through.

Two things this pins that are easy to get wrong:

* The guard is NOT `[ ! -t 0 ]`. The standalone installer can afford that check because it is
  run as a file; here stdin is a PIPE on the primary documented path, so testing stdin would
  refuse the exact install everyone runs. The condition is whether `/dev/tty` opens.
* The probe runs in a SUBSHELL. `exec` is a special builtin, so a failed redirection on it is
  fatal to the shell itself in dash -- probing inline reproduces the very rc=2 death the guard
  exists to prevent. The first attempt at this fix did exactly that and still measured rc=2.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from unittest import SkipTest

ROOT = Path(__file__).resolve().parents[2]
INSTALL = ROOT / "install.sh"


def _repo(tmp_path: Path, name: str) -> Path:
    target = tmp_path / name
    target.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "."], cwd=target, check=True)
    return target


def _install_without_a_terminal(target: Path, *args: str):
    """Run the installer detached from any controlling terminal."""
    if shutil.which("setsid") is None:
        raise SkipTest("setsid unavailable, so the controlling terminal cannot be dropped")
    done = subprocess.run(
        ["setsid", "sh", str(INSTALL), "--target", str(target), *args],
        stdin=subprocess.DEVNULL, capture_output=True, timeout=600,
    )
    return done.returncode, (done.stdout + done.stderr).decode(errors="replace")


def test_no_terminal_and_no_yes_refuses_clearly_and_writes_nothing(tmp_path):
    target = _repo(tmp_path, "no-tty")
    rc, out = _install_without_a_terminal(target)

    assert rc != 0, "an install with no terminal and no --yes exited 0 without installing"
    assert "cannot open /dev/tty" not in out, (
        "the raw shell error is still reaching the user instead of a handled refusal"
    )
    assert "--yes" in out, "the refusal never tells the user that --yes is the way through"
    assert not (target / ".builder").exists(), "a refused install still wrote .builder/"
    assert not list(target.glob(".builder-staging-*")), (
        "a refused install left a staging directory behind in the user's repo"
    )


def test_the_guard_refuses_only_the_prompt_not_the_install(tmp_path):
    # Guard the guard: if this fails too, the refusal above proves nothing about terminals --
    # it would just mean the installer is broken in this environment.
    target = _repo(tmp_path, "with-yes")
    rc, out = _install_without_a_terminal(target, "--yes")

    assert rc == 0, f"--yes no longer installs without a terminal; the guard refuses too much:\n{out[-800:]}"
    assert (target / ".builder" / "standards" / "builder-contract.md").is_file()
