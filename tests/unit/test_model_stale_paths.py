"""Tests for the stale absolute path annotation added to build_model (E4).

Scope: build_model must flag anchor `file` fields that are absolute and do
not resolve on disk with a `stale_absolute_path` collection_finding — WITHOUT
rewriting the spec artifact the anchor came from. It must NOT scan free-text
fields (e.g. task/AC check `command` strings) that merely mention a path,
since those legitimately contain historical path references.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "model.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("model_stale_paths", _SCRIPT_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


model_mod = _load_module()


def _write_yaml(path: Path, data) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return path


def _repo(tmp_path: Path) -> Path:
    root = Path(tmp_path)
    (root / ".builder" / "specs").mkdir(parents=True)
    return root


def _spec(root: Path, spec_id: str, *, status="verified") -> Path:
    spec = root / ".builder" / "specs" / spec_id
    _write_yaml(spec / "spec.yaml", {"name": spec_id, "status": status})
    return spec


def _tasks(spec: Path, tasks: list[dict]) -> None:
    _write_yaml(spec / "tasks.yaml", {"tasks": tasks})


def _trace_anchor(spec: Path, file_path: str) -> None:
    _write_yaml(
        spec / "traceability.yaml",
        {
            "task_links": [
                {
                    "task_id": "T1",
                    "files": [
                        {
                            "path": file_path,
                            "anchors": [{"id": "A1", "kind": "literal_string", "locator": "def demo"}],
                        }
                    ],
                }
            ]
        },
    )


def _stale_findings(model: dict, cap_key: str) -> list[dict]:
    cap = next(c for c in model["capabilities"] if c["key"] == cap_key)
    return [f for f in cap["collection_findings"] if f.get("kind") == "stale_absolute_path"]


def test_absolute_anchor_path_that_does_not_resolve_is_flagged(tmp_path):
    root = _repo(tmp_path)
    spec = _spec(root, "demo")
    stale_path = "/workspaces/example/scripts/record.py"
    _trace_anchor(spec, stale_path)
    _tasks(spec, [{"id": "T1", "verify": [{"command": "python3 -m pytest tests/test_demo.py -q"}]}])

    model = model_mod.build_model(root)

    findings = _stale_findings(model, "cap:demo")
    assert len(findings) == 1
    assert findings[0]["path"] == stale_path
    assert findings[0]["capability"] == "cap:demo"
    assert findings[0] in model["collection_findings"]


def test_resolvable_relative_anchor_path_is_not_flagged(tmp_path):
    root = _repo(tmp_path)
    spec = _spec(root, "demo")
    (root / "scripts").mkdir()
    (root / "scripts" / "record.py").write_text("def demo():\n    pass\n", encoding="utf-8")
    _trace_anchor(spec, "scripts/record.py")
    _tasks(spec, [{"id": "T1", "verify": [{"command": "python3 -m pytest tests/test_demo.py -q"}]}])

    model = model_mod.build_model(root)

    assert _stale_findings(model, "cap:demo") == []


def test_resolvable_absolute_anchor_path_is_not_flagged(tmp_path):
    root = _repo(tmp_path)
    spec = _spec(root, "demo")
    real_file = root / "real_file.py"
    real_file.write_text("def demo():\n    pass\n", encoding="utf-8")
    _trace_anchor(spec, str(real_file))
    _tasks(spec, [{"id": "T1", "verify": [{"command": "python3 -m pytest tests/test_demo.py -q"}]}])

    model = model_mod.build_model(root)

    assert _stale_findings(model, "cap:demo") == []


def test_free_text_command_mentioning_absolute_path_is_not_flagged(tmp_path):
    root = _repo(tmp_path)
    spec = _spec(root, "demo")
    # No traceability.yaml at all — the only place this absolute path
    # appears is inside a task verify `command` string, which is explicitly
    # out of scope (free text, not the anchor `file` field).
    _tasks(
        spec,
        [
            {
                "id": "T1",
                "verify": [
                    {"command": "python3 -m pytest /workspaces/example/tests/unit/test_record.py -q"}
                ],
            }
        ],
    )

    model = model_mod.build_model(root)

    assert _stale_findings(model, "cap:demo") == []
    assert model["capabilities"][0]["checks"][0]["command"] == (
        "python3 -m pytest /workspaces/example/tests/unit/test_record.py -q"
    )
