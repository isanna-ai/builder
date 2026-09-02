"""One resolver for every staged gate: env, then the repo's own dispatch.yaml, then warn.

Builder stages its gates rather than switching them on hard (BUILDER_TRACE_COVERAGE,
BUILDER_VERIFY_LINT, BUILDER_SPEC_BOOKKEEPING, BUILDER_ARCHIVE_REQUIRE_SYNC,
BUILDER_REQUIRE_SSOT_DELTA). Two of those also need PER-REPO settings, because enforcement has
to travel with the repo: the 22 wired repos sit at different stages of SSOT backfill, so
"enforce once this one is curated" is only expressible per repo, not per shell.

This lives in one place on purpose. Two copies of "env, then repo key, then default" would
drift, and the drift would be invisible until one gate behaved differently from the other on
the same repo.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from _dispatch_runtime.paths import runtime_dir


def repo_pipeline_setting(repo_root: Path, key: str) -> Optional[str]:
    """`pipeline.<key>` from the repo's own `.builder/dispatch.yaml`, or None.

    Shape-safe by design: a missing, unreadable or malformed config returns None so the caller
    falls back to its default. A broken config must never produce enforcement it was not asked
    for, and must never raise in the middle of a gate."""
    path = runtime_dir(repo_root) / "dispatch.yaml"
    try:
        if not path.is_file():
            return None
        from _validators.common import parse_yaml_like_file

        data, errors = parse_yaml_like_file(path)
        if errors or not isinstance(data, dict):
            return None
        pipeline = data.get("pipeline")
        if not isinstance(pipeline, dict):
            return None
        value = pipeline.get(key)
        text = str(value).strip() if value is not None else ""
        return text or None
    except Exception:  # noqa: BLE001 - a broken config is an absent one, never a crash
        return None


def staged_gate_enforced(env_var: str, repo_root: Path | None = None,
                         pipeline_key: str | None = None) -> bool:
    """True only when this gate is explicitly set to `enforce`.

    Resolution order: `env_var`, then the repo's `pipeline.<pipeline_key>`, then False (warn).

    The env var wins because it is the narrower, more deliberate scope -- an operator can
    override for a single invocation without editing a committed file. An EMPTY env value counts
    as UNSET rather than as `warn`, so a stray `export <VAR>=` in a shell profile cannot
    silently disable every repo's committed setting.

    Anything that is not exactly `enforce` (case-insensitively, trimmed) leaves the gate at
    warn -- including a typo. A misspelling must never silently enable enforcement, and it must
    never silently disable it either: warn is the safe direction in both cases."""
    raw = os.environ.get(env_var)
    if raw is not None and raw.strip():
        return raw.strip().lower() == "enforce"
    if repo_root is not None and pipeline_key:
        setting = repo_pipeline_setting(Path(repo_root), pipeline_key)
        if setting is not None:
            return setting.lower() == "enforce"
    return False
