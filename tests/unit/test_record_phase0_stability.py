from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
FIXTURE = ROOT / "tests" / "fixtures" / "builder_project_model" / "record_baselines" / "minimal-build-sha256.json"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


def _load_record():
    spec = importlib.util.spec_from_file_location("record_phase0_stability", SCRIPTS / "record.py")
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


record_mod = _load_record()


def _write_yaml(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _seed_repo(root: Path) -> None:
    (root / ".builder").mkdir(parents=True)
    _write_yaml(root / ".builder" / "dispatch.yaml", {"queue_store": {"path": ".builder/dispatch-queue"}})
    _write_yaml(
        root / ".builder" / "specs" / "demo" / "spec.yaml",
        {
            "name": "demo",
            "status": "verified",
            "current_phase": "6-verify",
            "next_action": "Run the next Builder phase.",
            "lane": "codex",
        },
    )
    _write_yaml(
        root / ".builder" / "dispatch-queue" / "queue" / "attempts" / "attempt-1.yaml",
        {
            "attempt_id": "attempt-1",
            "metadata": {
                "spec_id": "demo",
                "phase": "verify",
                "decision": "phase-complete",
                "reason": "outcome: SUCCEEDED",
                "started_at": "2026-07-13T00:00:00Z",
                "lane": "codex",
                "gates": {"host_verify": "pass", "source_diff": "abstain:non_gated_phase"},
            },
            "created_at": "2026-07-13T00:00:00Z",
        },
    )


def test_record_output_is_byte_stable_with_phase0_parser_module_present(tmp_path):
    from _builder_project_model import lint_home  # import proves the new module is loaded but unused by record

    root = tmp_path / "repo"
    out = tmp_path / "out"
    _seed_repo(root)
    assert callable(lint_home)
    assert record_mod.main([
        "build", "--root", str(root), "--all", str(root.parent), "--out", str(out),
    ]) == 0

    built = out / root.name
    expected = json.loads(FIXTURE.read_text(encoding="utf-8"))
    actual = {
        rel: hashlib.sha256((built / rel).read_bytes()).hexdigest()
        for rel in expected
    }

    assert actual == expected
