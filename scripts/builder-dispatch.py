#!/usr/bin/env python3
"""Builder dispatch control-plane operator entrypoint."""

from __future__ import annotations

from _dispatch_runtime.cli import run


if __name__ == "__main__":
    raise SystemExit(run())
