"""`_validate_intent_path` rejected the canonical path whenever the REPO ROOT reached it
through a symlink.

The comparison was asymmetric:

    root     = repo_root.resolve()      # resolved
    absolute = path.absolute()          # NOT resolved
    expected = root / ".builder" / "intents" / path.parent.name / "intent.yaml"
    if absolute != expected: raise

So any symlink component above the repo made the two sides disagree, and EVERY intent load
failed with `intent artifact must use .builder/intents/<intent-id>/intent.yaml` -- while the
caller was already using precisely that layout. The message sends you to check the one thing
that is not wrong.

This is not a macOS quirk. A project root reached through a symlink is ordinary -- a checkout
under a symlinked parent, or a home directory that is itself a link -- so
running builder through a symlinked parent directory broke the entire intent layer on any platform,
container included. On macOS it also broke 42 of the test suite's own cases, because
`tmp_path` lives under `/var/folders/...`, itself a symlink to `/private/var/folders/...`.

The security posture does NOT rest on the asymmetry, which is why symmetry is safe to
restore. Redirection is caught by defenses that run independently of this comparison:
explicit `is_symlink()` checks on `.builder`, `.builder/intents`, the intent directory and
the file, plus a `path.resolve().relative_to(root)` containment check. The tests below pin
both halves -- the false rejection is gone AND every symlink refusal still fires.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from _intent_model import _validate_intent_path  # noqa: E402


def _repo(tmp_path: Path, intent_id: str = "alpha") -> tuple[Path, Path]:
    root = tmp_path / "repo"
    d = root / ".builder" / "intents" / intent_id
    d.mkdir(parents=True)
    path = d / "intent.yaml"
    path.write_text("artifact: intent-object\n", encoding="utf-8")
    return root, path


def _raises(fn) -> str | None:
    """The shim has no pytest.raises; return the message or None."""
    try:
        fn()
    except ValueError as exc:
        return str(exc)
    return None


def test_the_canonical_path_is_accepted(tmp_path):
    root, path = _repo(tmp_path)
    assert _raises(lambda: _validate_intent_path(path, root)) is None


def test_a_repo_reached_through_a_symlinked_root_is_accepted(tmp_path):
    # THE REGRESSION. Reaching the repo through a symlinked parent; before the fix
    # this raised, and every intent in the repository became unreadable through that path.
    root, _ = _repo(tmp_path)
    link = tmp_path / "reps-builder"
    link.symlink_to(root, target_is_directory=True)
    linked_path = link / ".builder" / "intents" / "alpha" / "intent.yaml"
    assert _raises(lambda: _validate_intent_path(linked_path, link)) is None


def test_a_symlinked_intent_directory_is_still_refused(tmp_path):
    # The defense that actually matters: an <intent-id> directory symlink can redirect both
    # reads and CLI replacement writes out of the repository. Restoring symmetry must not
    # weaken this.
    root, _ = _repo(tmp_path)
    outside = tmp_path / "outside"
    (outside / "alpha").mkdir(parents=True)
    (outside / "alpha" / "intent.yaml").write_text("artifact: intent-object\n", encoding="utf-8")
    evil = root / ".builder" / "intents" / "evil"
    evil.symlink_to(outside / "alpha", target_is_directory=True)
    message = _raises(lambda: _validate_intent_path(evil / "intent.yaml", root))
    assert message is not None and "symlink" in message.lower()


def test_a_symlinked_intent_file_is_still_refused(tmp_path):
    root, _ = _repo(tmp_path)
    target = tmp_path / "elsewhere.yaml"
    target.write_text("artifact: intent-object\n", encoding="utf-8")
    d = root / ".builder" / "intents" / "beta"
    d.mkdir(parents=True)
    (d / "intent.yaml").symlink_to(target)
    message = _raises(lambda: _validate_intent_path(d / "intent.yaml", root))
    assert message is not None and "symlink" in message.lower()


def test_a_path_outside_the_intents_tree_is_still_refused(tmp_path):
    root, _ = _repo(tmp_path)
    stray = root / ".builder" / "intents" / "alpha" / "nested" / "intent.yaml"
    stray.parent.mkdir(parents=True)
    stray.write_text("artifact: intent-object\n", encoding="utf-8")
    assert _raises(lambda: _validate_intent_path(stray, root)) is not None


def test_a_spec_local_intent_is_still_out_of_scope(tmp_path):
    root, _ = _repo(tmp_path)
    p = root / ".builder" / "specs" / "demo" / "intent.yaml"
    p.parent.mkdir(parents=True)
    p.write_text("artifact: intent-object\n", encoding="utf-8")
    message = _raises(lambda: _validate_intent_path(p, root))
    assert message is not None and "out of scope" in message
