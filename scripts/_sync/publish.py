from __future__ import annotations

import os
import tempfile
from pathlib import Path


def atomic_publish(files: dict[Path, bytes]) -> None:
    staged: list[tuple[Path, Path]] = []
    preimages: dict[Path, bytes | None] = {}
    published: list[Path] = []
    try:
        for dest, payload in files.items():
            dest.parent.mkdir(parents=True, exist_ok=True)
            preimages[dest] = dest.read_bytes() if dest.exists() else None
            fd, tmp = tempfile.mkstemp(prefix=f".{dest.name}.", dir=str(dest.parent))
            tmp_path = Path(tmp)
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            staged.append((dest, tmp_path))
        for dest, tmp_path in staged:
            os.replace(tmp_path, dest)
            published.append(dest)
    except Exception:
        for dest in reversed(published):
            previous = preimages[dest]
            if previous is None:
                dest.unlink(missing_ok=True)
                continue
            fd, rollback = tempfile.mkstemp(prefix=f".{dest.name}.", dir=str(dest.parent))
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(previous)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(rollback, dest)
            finally:
                try:
                    Path(rollback).unlink(missing_ok=True)
                except OSError:
                    pass
        raise
    finally:
        for _, tmp_path in staged:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
