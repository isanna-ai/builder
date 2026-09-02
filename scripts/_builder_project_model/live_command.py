"""Command mapping from a federated candidate to the lease-scoped runner."""

from __future__ import annotations

import sys
from pathlib import Path

from _dispatch_runtime.paths import runtime_dir

from .repo_controller import Candidate


def live_command_builder(candidate: Candidate, *, isanna_script: Path | None = None) -> list[str]:
    script = Path(isanna_script or (Path(__file__).resolve().parents[1] / "isanna.py")).resolve()
    config = (runtime_dir(candidate.repo_root) / "dispatch.yaml").resolve()
    return [
        sys.executable,
        str(script),
        "dispatch",
        "--attempt",
        candidate.work_id,
        "--config",
        str(config),
    ]
