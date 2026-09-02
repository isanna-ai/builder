from __future__ import annotations

from pathlib import Path

from _sync.publish import atomic_publish
import _sync.publish as publish


def test_atomic_publish_replaces_all_targets(tmp_path: Path):
    target = tmp_path / "docs" / "system-behaviors.yaml"
    target.parent.mkdir(parents=True)
    target.write_text("before\n", encoding="utf-8")
    atomic_publish({target: b"after\n"})
    assert target.read_text(encoding="utf-8") == "after\n"


def test_atomic_publish_rolls_back_prior_replacements(tmp_path: Path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.write_bytes(b"before-1")
    second.write_bytes(b"before-2")
    real_replace = publish.os.replace
    calls = 0

    def fail_second(source, destination):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected publish failure")
        return real_replace(source, destination)

    publish.os.replace = fail_second
    try:
        try:
            atomic_publish({first: b"after-1", second: b"after-2"})
        except OSError:
            pass
        else:
            raise AssertionError("publish failure was not propagated")
    finally:
        publish.os.replace = real_replace
    assert first.read_bytes() == b"before-1"
    assert second.read_bytes() == b"before-2"
