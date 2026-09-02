"""Discovery must survive a sibling directory it is not allowed to read.

`Registry._discover_legacy` scans every directory beside the repo for a `.builder/product.yaml`.
`Path.is_file()` propagates EACCES -- pathlib suppresses only ENOENT/ENOTDIR/EBADF/ELOOP -- so a
single unreadable sibling used to raise PermissionError straight out of discovery and take the
whole command down with it.

That is not exotic. It was found because a root-owned mode-700 temp directory appeared beside the
checkout and turned three unrelated tests red:

    PermissionError: [Errno 13] Permission denied: '/tmp/tmpk8ufvbbi/.builder/product.yaml'

Anyone whose projects root holds another user's directory would have hit the same thing, and the
failure names a path in someone else's directory rather than anything about their repo. Someone
else's permissions are not a fact about this project.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import planning  # noqa: E402


def _product(root: Path, name: str) -> None:
    (root / ".builder").mkdir(parents=True, exist_ok=True)
    (root / ".builder" / "product.yaml").write_text(
        f"product: {name}\nhome_repo: {root}\nrepo_aliases:\n  - {name}\n", encoding="utf-8"
    )


def test_an_unreadable_sibling_directory_does_not_break_discovery(tmp_path):
    repo = tmp_path / "repo"
    _product(repo, "visible")

    blocked = tmp_path / "unreadable"
    (blocked / ".builder").mkdir(parents=True)
    (blocked / ".builder" / "product.yaml").write_text("product: hidden\n", encoding="utf-8")
    os.chmod(blocked, 0o000)
    try:
        # Guard the guard: if this process can still read it (running as root), the test would
        # pass without exercising anything.
        try:
            (blocked / ".builder" / "product.yaml").is_file()
        except PermissionError:
            pass
        else:
            from unittest import SkipTest
            raise SkipTest("this process can read a mode-000 directory (running as root?)")

        registry = planning.Registry(tmp_path, repo)

        names = sorted(p.product for p in registry.products if p.product)
        assert "visible" in names, "the readable sibling was not discovered"
        assert "hidden" not in names, "an unreadable sibling must not be discovered"
    finally:
        os.chmod(blocked, 0o700)


def test_an_unreadable_projects_root_is_reported_not_raised(tmp_path):
    repo = tmp_path / "repo"
    _product(repo, "visible")
    blocked_root = tmp_path / "sealed"
    blocked_root.mkdir()
    os.chmod(blocked_root, 0o000)
    try:
        try:
            list(blocked_root.iterdir())
        except PermissionError:
            pass
        else:
            from unittest import SkipTest
            raise SkipTest("this process can read a mode-000 directory (running as root?)")

        registry = planning.Registry(blocked_root, repo)

        assert registry.products == []
        assert any("cannot enumerate projects root" in f for f in registry.findings), (
            "an unreadable projects root must be reported as a finding, not swallowed silently"
        )
    finally:
        os.chmod(blocked_root, 0o700)
