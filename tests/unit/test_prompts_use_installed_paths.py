"""No shipped prompt may name a path that does not exist in an installed project.

Written because the same defect was fixed twice by hand and survived both times. One prompt was
corrected, then five more by searching for the literal `python3 scripts/...` -- and a seventh
still survived, because that search could not match it: it
had no `python3` prefix and sat inside backticks:

    2. Re-render canonical artifacts with `scripts/render-spec-artifacts.py`.

Each fix removed the instances it could see. None of them made the class impossible, and the
verification step reported "already correct" for a file that was not. This test is the check that
should have existed after the first one: it derives the rule from the shipped prompts themselves
rather than from a list of known-bad strings.

An installed project has NO top-level `scripts/`. The installer places them under
`.builder/scripts/`, so any prompt telling an agent to run `scripts/<something>.py` produces
ENOENT for every user.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PROMPTS = ROOT / "prompts"

# A `scripts/<name>.py` reference NOT preceded by `.builder/`. Deliberately matches the bare path
# wherever it appears -- in a command, in prose, inside backticks -- because the agent reading the
# prompt does not care which of those it was.
_BARE = re.compile(r"(?<!\.builder/)(?<![\w./])scripts/[A-Za-z0-9_-]+\.py")


def test_no_shipped_prompt_references_a_source_tree_only_script_path():
    offenders: list[str] = []
    for prompt in sorted(PROMPTS.glob("*.prompt.md")):
        for n, line in enumerate(prompt.read_text(encoding="utf-8").splitlines(), 1):
            for m in _BARE.finditer(line):
                offenders.append(f"{prompt.name}:{n}: {m.group(0)}  ({line.strip()[:80]})")
    assert offenders == [], (
        "these prompts name a path that does not exist in an installed project -- "
        "installed scripts live under .builder/scripts/:\n  " + "\n  ".join(offenders)
    )


def test_the_detector_actually_matches_the_bad_shape():
    """Guard the guard: if the regex stops matching, the test above passes vacuously."""
    assert _BARE.search("Run `scripts/render-spec-artifacts.py` now")
    assert _BARE.search("python3 scripts/validate-spec.py <spec> --root .")
    assert not _BARE.search("python3 .builder/scripts/validate-spec.py <spec>")
    assert not _BARE.search("see .builder/scripts/list-specs.py")
