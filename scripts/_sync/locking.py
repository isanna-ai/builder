from __future__ import annotations

import fcntl
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, TextIO


class SpecMutationBusy(RuntimeError):
    pass


@contextmanager
def spec_mutation_lock(root: Path, spec_id: str, *, blocking: bool, owner: str) -> Iterator[TextIO]:
    """One host lock shared by dispatch, sync, and readmission mutations for a spec."""
    lock_dir = root.resolve() / ".builder" / "locks" / "spec-mutation"
    lock_dir.mkdir(parents=True, exist_ok=True)
    path = lock_dir / f"{spec_id}.lock"
    stream = path.open("a+", encoding="utf-8")
    flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
    try:
        try:
            fcntl.flock(stream.fileno(), flags)
        except BlockingIOError as exc:
            raise SpecMutationBusy(f"spec mutation lock is busy: {path}") from exc
        stream.seek(0)
        stream.truncate()
        stream.write(f"{owner}-{os.getpid()}\n")
        stream.flush()
        yield stream
    finally:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        finally:
            stream.close()
