"""Claims the docs make about THIS repo's own machinery must be true of it.

Written after the same defect shipped twice in a row, the second time introduced by the fix for
the first:

  * A README platform note said the installer is "exercised on Linux and macOS on every change"
    while `.github/workflows/gate.yml` ran `ubuntu-latest` and nothing else. The claim described
    a CI job that did not exist.
  * Before that, CONTRIBUTING quoted suite counts taken from an older tree.

Both are the shape this project exists to refuse -- an assurance with no gate behind it -- in a
repository whose entire pitch is that claims are backed by execution. Reviewers cannot catch this
class reliably: the sentence reads perfectly, and checking it means going and looking at a
different file. So it becomes a test.

Scope is deliberately narrow: only claims about the repo's OWN observable structure, where the
check is exact rather than a guess. Prose about behaviour belongs to the auditors.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "gate.yml"
README = ROOT / "README.md"


def _workflow_text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def _job_names() -> set[str]:
    """Top-level keys under `jobs:` -- two-space indented, not the deeper step keys."""
    text = _workflow_text()
    body = text.split("\njobs:", 1)[1]
    return set(re.findall(r"^  ([a-z][a-z0-9-]*):$", body, re.MULTILINE))


def test_the_workflow_declares_the_jobs_the_docs_reference():
    """A doc may only point at `gate.yml` for a platform it actually has a job for."""
    jobs = _job_names()
    assert jobs, "parsed no jobs from gate.yml -- the parser is stale, not the workflow"
    assert "macos-install" in jobs, (
        "README's platform note tells readers the installer is exercised on macOS on every "
        f"change and points at gate.yml; the workflow declares {sorted(jobs)}"
    )


def test_the_macos_claim_matches_a_macos_runner():
    """The job has to actually run on macOS -- a job NAMED macos that runs on ubuntu would pass
    the check above and still make the README false."""
    text = _workflow_text()
    idx = text.find("\n  macos-install:")
    assert idx != -1
    block = text[idx : idx + 400]
    assert "runs-on: macos-latest" in block, (
        "the macos-install job does not run on a macOS runner:\n" + block[:200]
    )


def test_readme_does_not_claim_the_full_suite_runs_on_macos():
    """The suite is Linux-only by design (~40 cases read /proc). Claiming otherwise would be the
    same defect pointing the other way."""
    readme = README.read_text(encoding="utf-8")
    note = readme[readme.find("**Platforms.**") : readme.find("**Platforms.**") + 600]
    assert "**Platforms.**" in readme, "the platform note is gone -- this guard is now blind"
    assert "Linux-only" in note or "Linux only" in note, (
        "the platform note must still say the suite itself is Linux-only"
    )


def test_the_skill_count_readme_states_matches_the_skills_on_disk():
    """`Seven isanna-builder-* skills` is a number a reader can check, so it must be checkable."""
    readme = README.read_text(encoding="utf-8")
    m = re.search(r"(?i)(seven|six|eight|nine)\s+`isanna-builder-\*` skills", readme)
    assert m, "the skill-count claim is gone or reworded -- update this guard with it"
    words = {"six": 6, "seven": 7, "eight": 8, "nine": 9}
    claimed = words[m.group(1).lower()]
    actual = len([p for p in (ROOT / "skills").glob("isanna-builder*") if p.is_dir()])
    assert claimed == actual, f"README claims {claimed} isanna-builder-* skills; {actual} exist"
