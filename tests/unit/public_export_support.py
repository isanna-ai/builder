"""Skip guards for tests whose SUBJECT does not exist in a public clone.

Two distinct populations in this suite assert against things that live only in the
maintainer's private tree, and both are dropped by the fresh-history public export
(`scripts/export-public.sh`, driven by `pre-publish-scan.py --list-publishable`):

  * **Live-portfolio invariants** read THIS repo's own `.builder/specs` + `.builder/intents`
    corpus -- real releases, real intent migrations, a real completeness tally. The export
    excludes `.builder/` (personal specs + live queue state), so in a public clone
    `planning.find_release(...)` returns None and the invariant has no population at all.
  * **Excluded-asset tests** load a file the export deliberately drops. No subject, nothing to
    assert.

Before this module those tests did not skip -- they FAILED, so `make gate` was red on a
fresh clone of the public repo while green here. CONTRIBUTING.md names `make gate` as the
merge criterion and this project's entire claim is that the host runs the tests, so a
public clone failing its own gate is the one regression it cannot afford.

Skipping is honest here and not green-by-omission for two reasons. The skip is CONDITIONAL
on the subject being absent, so in this repo -- where the corpus and the excluded files DO
exist -- every one of these tests still runs and still fails on real drift. And every
behavior these tests guard in `docs/system-behaviors.yaml` also lists sibling guards that
are fixture-based and run everywhere, so `check_guard_outcomes.py` still sees a live guard
for each. Verified 2026-09-01: none of the affected behaviors is left guardless.

Use `raise SkipTest(...)` at the top of the test, exactly like the shipped/unshipped skips
already in these files -- the pytest shim reports it and records it in PYTEST_SHIM_OUTCOMES.
"""

from __future__ import annotations

from pathlib import Path
from unittest import SkipTest

from _validators.runtime import runtime_dir

__all__ = ["require_live_spec_corpus", "require_repo_asset"]

_PUBLIC_CLONE_NOTE = (
    "the public export excludes it, so a public clone has no such subject to assert on"
)


def require_live_spec_corpus(root: Path, subject: str) -> None:
    """Skip unless `root` carries this repo's live `.builder/specs` corpus.

    `subject` names the invariant in the skip message, so a skipped run says WHICH
    live-portfolio fact went unchecked rather than just "skipped".
    """
    specs = runtime_dir(Path(root)) / "specs"
    if not specs.is_dir():
        raise SkipTest(
            f"{subject} is a live-portfolio invariant over this repo's own spec corpus; "
            f"{specs} does not exist -- {_PUBLIC_CLONE_NOTE}."
        )


def require_repo_asset(root: Path, relative_path: str, subject: str) -> Path:
    """Skip unless `relative_path` exists under `root`; return it when it does."""
    target = Path(root) / relative_path
    if not target.exists():
        raise SkipTest(
            f"{subject} asserts on {relative_path}, which is not present in this checkout -- "
            f"{_PUBLIC_CLONE_NOTE}."
        )
    return target
