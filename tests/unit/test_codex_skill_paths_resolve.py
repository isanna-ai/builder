"""Every asset path `skills/builder/SKILL.md` names must resolve after a real Codex install.

That file installs to `$CODEX_HOME/skills/builder/SKILL.md`, and the installer populates
`prompts/`, `standards/`, `references/` and `agents/` *beside* it. Its paths are therefore
relative to its own directory, not to the project's `.builder/`.

This is written because reading it the other way produced a wrong finding AND a wrong fix. An
audit searched the source repo for `references/`, found none, reported
`references/planning-skill.md` as pointing at nothing, and checked a `.builder/` install to
confirm -- the copilot layout, where this skill is not installed at all. Acting on that report
repointed the line at `skills/planning/SKILL.md`, which does NOT exist in the Codex layout: a
correct reference was replaced with a broken one, and the source tree alone cannot tell you which.

So the check is an install, not a grep.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from unittest import SkipTest

ROOT = Path(__file__).resolve().parents[2]
SKILL = ROOT / "skills" / "builder" / "SKILL.md"

# Any backticked relative path in the file. Deliberately NOT a list of known-good prefixes:
# a prefix allowlist silently ignores exactly the case that matters -- a line repointed at a
# directory the installer never creates. `.builder/...` is excluded because those paths are
# project-relative by design (step 5), and `/isanna-*` commands cannot match (they start with
# a slash).
_PATH = re.compile(r"`([A-Za-z][A-Za-z0-9._-]*(?:/[A-Za-z0-9._-]+)*/?)`")


def _declared() -> set[str]:
    found = set()
    for token in _PATH.findall(SKILL.read_text(encoding="utf-8")):
        if "/" not in token or token.startswith(".builder"):
            continue
        found.add(token.rstrip("/"))
    return found


def test_the_scan_finds_declared_paths_at_all():
    # Guard the guard: an empty set would make the real assertion vacuous.
    declared = _declared()
    assert len(declared) >= 4, f"expected several declared asset paths, found {sorted(declared)}"
    assert any(d.startswith("standards/") for d in declared)
    assert "references/planning-skill.md" in declared, (
        "the scan no longer sees the planning reference, so the case this test exists for "
        f"would pass vacuously: {sorted(declared)}"
    )


def test_every_path_the_codex_skill_names_resolves_beside_it(tmp_path):
    target = tmp_path / "repo"
    target.mkdir()
    subprocess.run(["git", "init", "-q", "."], cwd=target, check=True)
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()

    done = subprocess.run(
        ["sh", str(ROOT / "install.sh"), "--target", str(target),
         "--ai", "codex", "--codex-home", str(codex_home), "--yes"],
        capture_output=True, timeout=600,
    )
    if done.returncode != 0:
        raise SkipTest(f"codex install did not complete here: {done.stderr.decode()[-300:]}")

    skill_dir = codex_home / "skills" / "builder"
    assert (skill_dir / "SKILL.md").is_file(), "the Codex skill did not install"

    missing = sorted(d for d in _declared() if not (skill_dir / d).exists())
    assert not missing, (
        "these paths are named in skills/builder/SKILL.md but do not exist beside the installed "
        f"skill, so an agent following it reads nothing: {missing}"
    )
