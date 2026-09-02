#!/usr/bin/env python3
"""isanna demo -- watch a lying agent get caught, then watch an honest one get through.

No API key, no network, nothing to configure. It builds a throwaway project in a temp dir and
runs the REAL gates against it -- the same `_host_verify_gate` and `_source_diff_gate` the
dispatcher runs in production. It does not simulate them, and that distinction is the entire
point: a demo that faked the gate would be exactly the kind of unearned green this tool exists
to refuse.

ACT 1  an agent changes NOTHING and reports "all tests pass. SUCCEEDED."
       the host runs the tests -> exit 1. the host reads the diff -> empty. REJECTED.
       (a workflow that asks the agent whether it succeeded marks this done.)

ACT 2  an agent actually implements the function.
       the host runs the tests -> exit 0. the host reads the diff -> src/ and tests/ changed. VERIFIED.

       Both acts are asserted in CI: this script exits non-zero unless ACT 1 is REJECTED
       and ACT 2 is VERIFIED, and .github/workflows/gate.yml runs `make demo` on every push.
the build breaks -- the demo IS the regression test.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from _dispatch_runtime.paths import runtime_dir

sys.dont_write_bytecode = True

# This is the first command the README gives a reader, so it has to survive an environment we did
# not choose. Two ways it did not:
#   * a genuine 8-bit locale (e.g. ISO-8859-1) made the em-dash below a hard UnicodeEncodeError.
#     `LC_ALL=C` is fine on its own -- PEP 538 coerces it -- so this only shows up on a real
#     non-UTF-8 locale, which is exactly the kind of machine nobody tests on.
#   * no `git` on PATH produced a twelve-frame traceback instead of a sentence.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except (AttributeError, ValueError, OSError):
        pass

if shutil.which("git") is None:
    print("isanna demo needs `git` on PATH: it builds a throwaway repository to run the gates "
          "against. Install git and re-run.", file=sys.stderr)
    raise SystemExit(2)

SCRIPTS = Path(__file__).resolve().parent

BOLD, DIM, RED, GREEN, YELLOW, RESET = "\033[1m", "\033[2m", "\033[31m", "\033[32m", "\033[33m", "\033[0m"

FAILING_TEST = '''from src.app import greet


def test_greet():
    assert greet("world") == "hello world"
'''

STUB_APP = '''def greet(name):
    raise NotImplementedError
'''

HONEST_APP = '''def greet(name):
    return f"hello {name}"
'''

# The demo must run with NOTHING installed -- no pip, no network, no pytest. So the throwaway
# repo carries a 12-line stdlib runner instead of declaring `python3 -m pytest -q`, which needs
# pytest present and is not satisfied by this repo's bundled shim (the demo runs in a temp dir,
# where that shim is not importable). The point of the demo is that the HOST runs a real command
# and reads a real exit code; which runner it is does not matter, but it working everywhere does.
RUN_TESTS = '''"""Tiny stdlib test runner: import the test module, call every test_* function."""
import sys, traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tests.test_app as suite

failed = 0
for name in sorted(n for n in dir(suite) if n.startswith("test_")):
    try:
        getattr(suite, name)()
    except Exception:
        failed += 1
        traceback.print_exc()
print(("FAILED " + str(failed)) if failed else "ok")
sys.exit(1 if failed else 0)
'''

SETUP_DECISIONS = """commands:
  default:
    test: "python3 run_tests.py"
"""

LIAR_TURN = """I analysed the failing test and implemented `greet()` in src/app.py.
It now returns the correct greeting for any name. I ran the test suite locally and
all tests pass.

DECISION: SUCCEEDED
"""


def _c(colour: str, text: str) -> str:
    return text if os.environ.get("NO_COLOR") else f"{colour}{text}{RESET}"


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def build_project(root: Path) -> Path:
    """A minimal repo with ONE failing test and an unimplemented function."""
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "tests").mkdir(parents=True, exist_ok=True)
    (root / ".gitignore").write_text("__pycache__/\n.pytest_cache/\n", encoding="utf-8")
    (root / "src" / "__init__.py").write_text("", encoding="utf-8")
    (root / "src" / "app.py").write_text(STUB_APP, encoding="utf-8")
    (root / "tests" / "test_app.py").write_text(FAILING_TEST, encoding="utf-8")
    # Part of the STARTING repo, committed below and captured in the baseline -- not something
    # the agent adds. Written after the baseline it reads as a new source file, and ACT 1's liar
    # passes source_diff on the strength of it.
    (root / "run_tests.py").write_text(RUN_TESTS, encoding="utf-8")
    spec = runtime_dir(root) / "specs" / "greeting"
    spec.mkdir(parents=True, exist_ok=True)
    (spec / "setup-decisions.yaml").write_text(SETUP_DECISIONS, encoding="utf-8")

    _git(["init", "-q"], root)
    _git(["config", "user.email", "demo@isanna.ai"], root)  # publish-ok: public demo identity
    _git(["config", "user.name", "isanna demo"], root)
    _git(["add", "-A"], root)
    _git(["commit", "-q", "-m", "the task: make greet() work"], root)
    return root


class _Work:
    def __init__(self, project_dir: Path, phase: str = "verify"):
        self.project_dir = project_dir
        self.specs_dir = runtime_dir(project_dir) / "specs"
        self.spec_id = "greeting"
        self.phase = phase
        self.runner_task_ref = None


def _head(root: Path) -> str:
    out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True)
    return out.stdout.strip()


def _source_paths(root: Path) -> set[str]:
    """The pre-turn baseline the lane captures: the set of source files ALREADY dirty before the
    agent ran -- not every file in the repo. On a clean checkout that is the empty set, and the
    gate then asks "did this turn dirty anything new?". Snapshotted with the lane's own function
    so the demo cannot drift from the gate's definition of a source path."""
    sys.path.insert(0, str(SCRIPTS))
    from _dispatch_runtime.lane_common import _git_source_paths

    return set(_git_source_paths(root, None) or set())


def _pin_gates() -> None:
    """The demo shows what the gates DO, so it pins them on rather than inheriting the shell.
    A viewer whose environment happens to carry BUILDER_HOST_VERIFY=off would otherwise watch
    the liar walk free and conclude the product does not work."""
    os.environ["BUILDER_HOST_VERIFY"] = "enforce"
    os.environ["BUILDER_RED_BASELINE"] = "enforce"
    os.environ["BUILDER_GATE_EVIDENCE"] = "off"  # a temp dir; nothing to keep


def run_act(root: Path, *, title: str, narrative: str, agent_says: str,
            pre_head: str, pre_paths: set[str]) -> tuple[bool, bool]:
    """Run the REAL gates. Returns (host_verify_passed, source_diff_passed)."""
    sys.path.insert(0, str(SCRIPTS))
    _pin_gates()
    from _dispatch_runtime.lane_common import _host_verify_gate, _source_diff_gate

    print(_c(BOLD, f"\n{title}"))
    print(_c(DIM, narrative))
    print(_c(DIM, "  ── the agent's turn ──"))
    for line in agent_says.strip().splitlines():
        print(_c(YELLOW, f"  │ {line}"))
    print(_c(DIM, "  ── the host's turn (it does not ask the agent) ──"))

    # ORDER IS LOAD-BEARING, and it is the gate's contract, not a preference: source_diff is
    # evaluated BEFORE the verify commands, which mutate the tree (pytest alone drops
    # __pycache__/ and .pytest_cache/). Run them the other way round and those artifacts read as
    # "the agent changed source" -- the liar passes the diff gate on the strength of its own
    # test run. Production runs them in this order; so does the demo.
    sd_passed, sd_reason = _source_diff_gate(
        _Work(root, "implement"), "implement", pre_source_paths=pre_paths, pre_head=pre_head)
    hv_passed, hv_reason = _host_verify_gate(_Work(root, "verify"), "verify")

    def _line(name: str, passed, reason: str) -> None:
        if passed is True:
            print(f"  {_c(GREEN, 'PASS')}  {name}")
        elif passed is False:
            print(f"  {_c(RED, 'FAIL')}  {name}  {_c(DIM, reason)}")
        else:
            print(f"  {_c(DIM, 'n/a ')}  {name}  {_c(DIM, reason or 'abstained')}")

    _line("host_verify   the host ran `python3 run_tests.py`", hv_passed, hv_reason)
    _line("source_diff   the host read `git diff`", sd_passed, sd_reason)
    return hv_passed, sd_passed


def main(argv: list[str] | None = None) -> int:
    with tempfile.TemporaryDirectory(prefix="isanna-demo-") as tmp:
        root = build_project(Path(tmp))
        head, paths = _head(root), _source_paths(root)

        print(_c(BOLD, "isanna demo") + _c(DIM, "  — the host runs the tests. the agent does not get a vote."))
        print(_c(DIM, f"  a throwaway repo in {root}: one failing test, one unimplemented function."))

        # ACT 1 -- the liar. It writes nothing at all, and reports success.
        hv1, sd1 = run_act(
            root,
            title="ACT 1 — the agent lies",
            narrative="  it changes no files, and reports that it implemented the function and the tests pass.",
            agent_says=LIAR_TURN, pre_head=head, pre_paths=paths)
        caught = (hv1 is False) and (sd1 is False)
        print("  " + (_c(RED, "REJECTED") + "  the phase does not complete. no 'verified' stamp is issued."
                      if caught else _c(RED, "!! THE LIE GOT THROUGH -- the gate is broken !!")))
        print(_c(DIM, "  a workflow that asks the agent whether it succeeded marks this done."))

        # ACT 2 -- honest work. The same gates, the same run, a real diff.
        (root / "src" / "app.py").write_text(HONEST_APP, encoding="utf-8")
        (root / "tests" / "test_app.py").write_text(
            FAILING_TEST + '\n\ndef test_greet_empty():\n    assert greet("") == "hello "\n', encoding="utf-8")
        hv2, sd2 = run_act(
            root,
            title="ACT 2 — the agent does the work",
            narrative="  it implements greet() and extends the test. same gates, same commands, real diff.",
            agent_says="I implemented `greet()` and added a case for the empty name.\n\nDECISION: SUCCEEDED",
            pre_head=head, pre_paths=paths)
        accepted = (hv2 is True) and (sd2 is True)
        print("  " + (_c(GREEN, "VERIFIED") + "  host-executed. this stamp was earned, not asserted."
                      if accepted else _c(RED, "!! honest work was rejected -- the gate is broken !!")))

        print(_c(BOLD, "\n  the difference is not the agent. it is who ran the tests.\n"))
        return 0 if (caught and accepted) else 1


if __name__ == "__main__":
    raise SystemExit(main())
