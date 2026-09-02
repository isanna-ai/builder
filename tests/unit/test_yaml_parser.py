"""Tests for the local YAML compatibility shim."""

from enum import Enum, IntEnum, StrEnum
import os
import subprocess
import sys
from pathlib import Path

from _yaml import yaml
from _yaml_compat import safe_load


def test_safe_load_parses_nested_mapping_inside_sequence_item() -> None:
    text = (
        "builder_root: /path/to/project\n"
        "backends:\n"
        "  - adapter:\n"
        "      module_path: _runner_protocol.fakes\n"
        "      class_name: FakeBackend\n"
    )

    data = safe_load(text)

    assert data == {
        "builder_root": "/path/to/project",
        "backends": [
            {
                "adapter": {
                    "module_path": "_runner_protocol.fakes",
                    "class_name": "FakeBackend",
                }
            }
        ],
    }


def test_safe_load_parses_flow_mapping_gate_evidence_reference() -> None:
    data = safe_load(
        "gate_evidence:\n"
        "  - {path: gate-evidence/0001-host_verify-verify.yaml, sha256: deadbeef}\n"
    )

    assert data == {
        "gate_evidence": [{
            "path": "gate-evidence/0001-host_verify-verify.yaml",
            "sha256": "deadbeef",
        }],
    }


def test_str_enum_round_trips_but_non_string_enums_are_not_coerced() -> None:
    class DispatchState(StrEnum):
        READY = "ready"

    class Plain(Enum):
        READY = "ready"

    class Number(IntEnum):
        ONE = 1

    dumped = yaml.safe_dump({"state": DispatchState.READY}, sort_keys=False)
    assert yaml.safe_load(dumped) == {"state": "ready"}
    try:
        yaml.safe_dump(Plain.READY)
    except Exception:
        pass
    else:
        raise AssertionError("plain Enum was silently coerced")
    try:
        yaml.safe_dump(Number.ONE)
    except Exception:
        pass
    else:
        raise AssertionError("IntEnum was silently coerced")


def test_forced_compat_has_the_same_str_enum_contract() -> None:
    """Run without site packages so _yaml must select the bundled formatter."""
    scripts = Path(__file__).resolve().parents[2] / "scripts"
    program = """
from enum import Enum, IntEnum, StrEnum
from _yaml import yaml
class String(StrEnum): READY = 'ready'
class Plain(Enum): READY = 'ready'
class Number(IntEnum): ONE = 1
assert yaml.safe_load(yaml.safe_dump({'state': String.READY})) == {'state': 'ready'}
for value in (Plain.READY, Number.ONE):
    try:
        yaml.safe_dump(value)
    except Exception:
        continue
    raise AssertionError(f'{value!r} was silently coerced')
"""
    result = subprocess.run(
        [sys.executable, "-S", "-c", program],
        env={**os.environ, "PYTHONPATH": str(scripts)},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
