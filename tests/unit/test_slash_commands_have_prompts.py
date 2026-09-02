"""A shipped asset must not name a slash command that has no prompt file behind it.

This defect class has now shipped three times, in three different shapes:

  1. `/isanna-explore` -- offered by one skill, denied by the help prompt, implemented nowhere.
  2. `skills/builder/SKILL.md` listed `/isanna-list`, `/isanna-validate` and `/isanna-telemetry`
     in the same flat `## Command Map` as twelve real commands. That file installs into
     `$CODEX_HOME/skills/builder/`, so a Codex user typed `/isanna-list` on day one and got
     nothing.
  3. `prompts/builder-handoff-template.prompt.md` named the same three again -- in inline prose
     and in a fully rendered worked example (`Command : /isanna-validate`).

The guard written for (2) matched only command-map BULLETS, so it passed on (3) while its own
docstring claimed the class was covered. A rule that sees one syntax is not a rule about the
defect; it is a rule about a paragraph shape. This reads every ``/isanna-...`` token in every
shipped prompt, skill and standard, wherever it appears.

What legitimately resolves: a prompt file under `prompts/`, OR a skill directory under `skills/`
(skills are invocable by name too, so `/isanna-builder-roadmap` is real).

Prose that says a name is NOT a command is exempt, because saying so is the fix. The exemption is
mechanical and narrow -- a disclaiming phrase on the line itself or either neighbour, or the token
directly negated as "no `/isanna-x`". "Appears somewhere near a caveat" does not count.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PROMPTS = ROOT / "prompts"

_TOKEN = re.compile(r"(?:((?i:no))\s+)?`?(/isanna-[a-z0-9-]+)`?")

_DISCLAIMERS = (
    "does not exist", "do not exist", "no prompt file",
    "not a slash command", "NOT slash commands", "not commands",
)


def _sources() -> list[Path]:
    return (
        sorted(ROOT.glob("skills/*/SKILL.md"))
        + sorted(PROMPTS.glob("*.prompt.md"))
        + sorted((ROOT / "standards").glob("*.md"))
    )


def _resolvable() -> set[str]:
    commands = {"/" + p.name[: -len(".prompt.md")] for p in PROMPTS.glob("*.prompt.md")}
    commands |= {"/" + d.name for d in (ROOT / "skills").iterdir() if d.is_dir()}
    return commands


def _offered() -> dict[str, list[str]]:
    """command -> "file:line" for each place it is named as though it were real."""
    real = _resolvable()
    offered: dict[str, list[str]] = {}
    for path in _sources():
        lines = path.read_text(encoding="utf-8").splitlines()
        for i, line in enumerate(lines, 1):
            neighbourhood = lines[max(0, i - 2): i + 1]
            if any(d in text for text in neighbourhood for d in _DISCLAIMERS):
                continue
            for negated, command in _TOKEN.findall(line):
                if negated:            # "no `/isanna-explore` command"
                    continue
                if command not in real:
                    offered.setdefault(command, []).append(f"{path.relative_to(ROOT)}:{i}")
    return offered


def test_the_scan_sees_the_real_commands_at_all():
    # Guard the guard: if the token regex or the source list breaks, the assertion below
    # passes vacuously.
    real = _resolvable()
    assert "/isanna-1-specify" in real, "the prompt-file scan found nothing recognisable"
    assert len(real) >= 15, f"expected prompts + skills, found {len(real)}"
    assert len(_sources()) >= 20, "the shipped-asset list collapsed"


def test_no_shipped_asset_names_a_command_that_does_not_exist():
    missing = _offered()
    assert not missing, (
        "these are named as slash commands but nothing implements them, so typing one does "
        f"nothing: {missing}"
    )
