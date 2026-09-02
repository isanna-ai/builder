"""_dispatch_runtime.paths must re-export the single-sourced runtime-dir helpers.

Regression guard for the duplicated RUNTIME_DIR_NAMES constant: paths.py used to
define its own copy of RUNTIME_DIR_NAMES/runtime_dir alongside _validators.runtime's
copy. Pin them to the same object so a future edit can't silently re-fork the two.
"""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from _dispatch_runtime import paths as dispatch_paths  # noqa: E402
from _validators import runtime as validators_runtime  # noqa: E402


def test_runtime_dir_names_is_single_sourced():
    assert dispatch_paths.RUNTIME_DIR_NAMES is validators_runtime.RUNTIME_DIR_NAMES


def test_runtime_dir_is_single_sourced():
    assert dispatch_paths.runtime_dir is validators_runtime.runtime_dir
