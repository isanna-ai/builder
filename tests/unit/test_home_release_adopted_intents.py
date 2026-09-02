"""The home-release manifest parser must accept the optional `adopted_intents` key.

Owner-adoption (planning) writes `adopted_intents` into the release manifest. The strict home-release
parser (reject_unknown_keys) must ALLOW that key, or load_valid_snapshot degrades the whole Builder
Home to standalone (breaking the central daemon's governance) — the bug this guards against.
parse_release_manifest RAISES ValidationError on any issue, and requires name == filename stem.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from _builder_project_model.common import ValidationError
from _builder_project_model.parsers import parse_release_manifest


def _write(tmp_path, name: str, extra: str) -> Path:
    p = tmp_path / f"{name}.yaml"
    p.write_text(
        "schema_version: 1\n"
        f"name: {name}\n"
        'description: "x"\n'
        "status: active\n"
        "intents:\n"
        "  - intent-a\n" + extra,
        encoding="utf-8",
    )
    return p


def test_adopted_intents_key_is_accepted(tmp_path):
    p = _write(
        tmp_path,
        "rel-x",
        "adopted_intents:\n"
        "  - intent: intent-a\n"
        '    authorization: "owner note"\n'
        "    basis: merged-and-host-verified\n",
    )
    manifest = parse_release_manifest(p)  # must NOT raise
    assert manifest is not None
    assert manifest.name == "rel-x"


def test_unknown_key_still_rejected(tmp_path):
    p = _write(tmp_path, "rel-y", "totally_bogus_key: 1\n")
    try:
        parse_release_manifest(p)
    except ValidationError as exc:
        assert "unknown key" in str(exc)
        return
    raise AssertionError("expected ValidationError for an unknown key")
