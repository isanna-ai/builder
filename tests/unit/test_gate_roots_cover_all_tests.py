"""Every directory holding Python tests must be a `make gate` root.

`scripts/_telemetry/` held 42 passing tests that no gate root reached and no CI job ran. They
were green, and nothing kept them green -- the "green by omission" antipattern the Makefile's own
comment says this project exists to refuse. The fix added the directory. This is the part that
stops the next one.

Until now the rule lived only in that comment. The mechanical check that looks like it would
catch this -- `scripts/_validators/behaviors.py`, which parses the `gate:` block -- only verifies
directories that `docs/system-behaviors.yaml` already names a test in. `_telemetry` names none,
which is exactly why it stayed invisible; a fifth orphan directory guarding nothing declared would
be just as invisible. In a project whose thesis is that prose is not a rule, this one was prose.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_SKIP = {".git", ".builder", "node_modules", "__pycache__"}


def _dirs_with_tests() -> set[str]:
    found = set()
    for path in ROOT.rglob("test_*.py"):
        rel = path.relative_to(ROOT)
        if any(part in _SKIP for part in rel.parts):
            continue
        found.add(rel.parent.as_posix())
    return found


def _gate_roots() -> list[str]:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    m = re.search(r"python3 -m pytest ([^;\\\n]+)", makefile)
    assert m, "could not find the pytest invocation in the Makefile gate: block"
    return [tok for tok in m.group(1).split() if not tok.startswith("-")]


def test_the_scan_finds_tests_and_roots_at_all():
    # Guard the guard: either half returning empty would make the real assertion vacuous.
    dirs = _dirs_with_tests()
    roots = _gate_roots()
    assert "tests/unit" in dirs, f"the test-file scan found nothing recognisable: {sorted(dirs)}"
    assert len(dirs) >= 3, f"expected several test directories, found {sorted(dirs)}"
    assert "tests/unit" in roots, f"the Makefile gate-root parse looks wrong: {roots}"


def test_every_directory_with_tests_is_reached_by_the_gate():
    roots = _gate_roots()
    orphans = sorted(
        d for d in _dirs_with_tests()
        if not any(d == r or d.startswith(r.rstrip("/") + "/") for r in roots)
    )
    assert not orphans, (
        "these directories hold test_*.py files that `make gate` never runs, so nothing keeps "
        f"them green: {orphans}. Add them to the pytest roots in the Makefile."
    )
