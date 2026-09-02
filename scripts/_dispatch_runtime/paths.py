"""Canonical runtime-dir helpers -- single-sourced from the standalone validator package.

_validators must remain import-free (ships standalone); _dispatch_runtime always ships alongside it (both script-class
manifest assets staged under one root -- guarded by tests/unit/test_staged_import_closure.py)."""

from __future__ import annotations

from _validators.runtime import ARCHIVE_DIR_NAME, RUNTIME_DIR_NAMES, resolve_spec_dir, runtime_dir

__all__ = ["ARCHIVE_DIR_NAME", "RUNTIME_DIR_NAMES", "resolve_spec_dir", "runtime_dir"]
