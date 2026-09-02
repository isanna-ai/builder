"""The RED step is where an already-shipped deliverable announces itself, and the standard used
to talk the agent out of hearing it.

`standards/builder-tdd.md` said, of a test that passes on its first run: "If it passes, your test
is wrong -- it's not testing what you think. Fix the test before proceeding." That names exactly
one cause and prescribes exactly one response. Applied to a spec whose deliverable already
exists -- shipped by another spec, built by hand, or written before the spec was -- the honest
test passes, and the standard sends the agent off to edit it until it fails. The strongest
available evidence that the work is already done gets converted into instructions to manufacture
a failure and then rebuild what is already there.

These are guard tests over prose. They exist because the rule has no runtime: nothing executes a
standard, so nothing else would notice if the branch were deleted in an edit six months from now.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TDD_STANDARD = ROOT / "standards" / "builder-tdd.md"
IMPLEMENT_PROMPT = ROOT / "prompts" / "isanna-5-implement.prompt.md"


def _read(path: Path) -> str:
    assert path.is_file(), f"missing {path}"
    return path.read_text(encoding="utf-8")


def test_the_tdd_standard_no_longer_blames_the_test_unconditionally():
    text = _read(TDD_STANDARD).lower()
    assert "your test is wrong" not in text, (
        "The unconditional 'your test is wrong' instruction is back. A first-run pass has two "
        "causes, and this wording admits only one of them."
    )


def test_the_tdd_standard_carries_the_already_shipped_branch():
    text = _read(TDD_STANDARD).lower()
    assert "already-shipped" in text or "already shipped" in text
    assert "already implemented" in text, (
        "The standard must name the possibility that the behavior already exists, not merely "
        "that the test might be faulty."
    )
    assert "stop" in text


def test_the_tdd_standard_still_fixes_a_genuinely_wrong_test():
    # The fix is a fork, not a deletion. A test that passes because it asserts nothing is still
    # a broken test and must still be repaired -- ruling out already-shipped comes first, it
    # does not replace the wrong-test branch.
    text = _read(TDD_STANDARD).lower()
    assert "fix the test" in text


def test_the_implement_prompt_stops_the_task_on_an_unexpected_green():
    text = _read(IMPLEMENT_PROMPT).lower()
    assert "already-shipped" in text, (
        "The phase-5 per-task loop must name the already-shipped stop condition; the standard "
        "alone is not loaded by every runner profile."
    )
    assert "stop" in text


def test_the_implement_prompt_declares_already_shipped_as_an_outcome():
    # A runner with no name for the result has nowhere to put it, and will report `partial` or
    # quietly proceed instead.
    text = _read(IMPLEMENT_PROMPT).lower()
    outcomes_line = [ln for ln in text.splitlines() if "one outcome for the batch" in ln]
    assert outcomes_line, "the prompt no longer declares batch outcomes"
    assert "already-shipped" in outcomes_line[0]
