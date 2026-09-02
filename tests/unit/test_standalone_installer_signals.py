"""An interrupted STANDALONE install must abort, not finish and claim success.

`install.sh` was fixed for this and is pinned by `tests/test_installer_signals.sh`. The standalone installer is its twin -- the path
`README.md` recommends to proxy-blocked users -- and it kept the original defect verbatim:

    trap 'cleanup' EXIT INT TERM

A trap handler that does not exit RESUMES the script, so the signal was swallowed. Measured on
the shipped v0.3.1 bundle, `kill -INT`/`kill -TERM` sent to the installer shell gave rc=0 with
all 109 files written and the success banner printed -- 6 runs out of 6, identical to a clean
install. Pressing Ctrl-C did nothing whatsoever.

Two delivery modes, because they exercise different halves of the mechanism and only one of
them was ever broken:

* PROCESS GROUP -- a real Ctrl-C at a terminal. The delegated `install.sh` gets the signal too,
  dies, and returns non-zero, so the outer script aborts on its exit status. This half already
  worked; it is pinned so a future change cannot quietly lose it.
* SHELL PID ONLY -- `kill <pid>`, or a CI `timeout`. Nothing but the outer shell is signalled,
  so the trap is the only thing standing between the user and a fabricated success. This is the
  half that was broken.

In the pid-only case the delegated install has usually already completed by the time a POSIX
shell runs the deferred trap, so the success banner may legitimately appear -- the install did
happen. What must never happen is `rc=0`: that reports the signal had no effect at all.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path
from unittest import SkipTest

ROOT = Path(__file__).resolve().parents[2]
BUNDLE = ROOT / "standalone-installer.sh.txt"

_BANNER = b"installed for"


def _target(tmp_path: Path, name: str) -> Path:
    target = tmp_path / name
    target.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "."], cwd=target, check=True)
    return target


def _interrupted_install(target: Path, sig: int, group: bool):
    """Start a standalone install, signal it once it is demonstrably underway."""
    log = target / "install.log"
    with log.open("wb") as handle:
        proc = subprocess.Popen(
            ["sh", str(BUNDLE), "--target", str(target), "--yes"],
            cwd=str(target), stdout=handle, stderr=handle, start_new_session=True,
        )
        # Signal only once the install is actually writing, so a slow machine cannot turn this
        # into a race that signals a process which has not started copying yet.
        deadline = time.monotonic() + 60.0
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                break
            if (target / ".builder").is_dir() and any((target / ".builder").rglob("*")):
                break
            time.sleep(0.05)
        if proc.poll() is not None:
            raise SkipTest(
                "the standalone install finished before it could be signalled; this machine is "
                "too fast for the probe to land mid-install"
            )
        try:
            os.killpg(proc.pid, sig) if group else os.kill(proc.pid, sig)
        except ProcessLookupError:
            pass
        rc = proc.wait(timeout=180)
    # A pid-only signal orphans the delegated install; let it finish before reading the tree.
    time.sleep(0.5)
    return rc, log.read_bytes()


def test_a_real_ctrl_c_aborts_the_standalone_install_without_claiming_success(tmp_path):
    for signame, sig in (("SIGINT", signal.SIGINT), ("SIGTERM", signal.SIGTERM)):
        rc, out = _interrupted_install(_target(tmp_path, "grp-" + signame), sig, group=True)
        assert rc != 0, (
            signame + " to the process group left rc=0: the signal was swallowed and the "
            "installer ran to completion"
        )
        assert _BANNER not in out, (
            signame + " to the process group still printed the success banner"
        )


def test_a_signal_to_the_installer_shell_alone_is_not_swallowed(tmp_path):
    # The half that was broken. `kill <pid>` and CI timeouts signal only the outer shell, and
    # before the fix that produced rc=0 plus a full install plus the banner -- indistinguishable
    # from never having sent the signal.
    for signame, sig in (("SIGINT", signal.SIGINT), ("SIGTERM", signal.SIGTERM)):
        rc, _ = _interrupted_install(_target(tmp_path, "pid-" + signame), sig, group=False)
        assert rc != 0, (
            signame + " sent to the installer shell alone left rc=0, so cancelling the install "
            "had no observable effect at all"
        )


def test_the_signal_traps_did_not_break_an_ordinary_install(tmp_path):
    target = _target(tmp_path, "clean")
    done = subprocess.run(
        ["sh", str(BUNDLE), "--target", str(target), "--yes"],
        cwd=str(target), capture_output=True, timeout=300,
    )
    assert done.returncode == 0, "an uninterrupted standalone install no longer succeeds"
    assert _BANNER in done.stdout, "an uninterrupted standalone install printed no success banner"
    assert (target / ".builder" / "standards" / "builder-contract.md").is_file()
