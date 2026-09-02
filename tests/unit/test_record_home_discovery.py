from __future__ import annotations

import importlib.util
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
FIXTURE = ROOT / "tests" / "fixtures" / "builder_project_model" / "home" / "portfolio"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


def _load_record():
    spec = importlib.util.spec_from_file_location("record_home_discovery", SCRIPTS / "record.py")
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _copy_fixture(tmp_path: Path) -> Path:
    target = tmp_path / "portfolio"
    shutil.copytree(FIXTURE, target)
    for repo in ("alpha-repo", "beta-repo", "shared-repo"):
        (target / repo / ".git").mkdir(parents=True)
        dispatch = target / repo / ".builder" / "dispatch.yaml"
        dispatch.parent.mkdir(parents=True, exist_ok=True)
        dispatch.write_text('{"queue_store":{"path":".builder/dispatch-queue"}}\n', encoding="utf-8")
    return target


def _stub_scan(planning_mod, repo: Path, rows: dict[str, str]) -> None:
    planning_mod._scan_cache[str(repo.resolve())] = {
        "specs": [{"spec": spec_id, "verification": verdict} for spec_id, verdict in rows.items()]
    }


def test_record_build_uses_builder_home_intent_completeness(tmp_path):
    portfolio = _copy_fixture(tmp_path)
    record_mod = _load_record()
    planning_mod = sys.modules["planning"]
    planning_mod._scan_cache.clear()
    _stub_scan(planning_mod, portfolio / "alpha-repo", {"alpha-core": planning_mod.HOST_VERIFIED, "alpha-backlog": planning_mod.UNKNOWN})
    _stub_scan(planning_mod, portfolio / "shared-repo", {"shared-spec": planning_mod.SELF_REPORTED})
    _stub_scan(planning_mod, portfolio / "beta-repo", {"beta-fix": planning_mod.UNKNOWN, "beta-backlog": planning_mod.UNKNOWN})

    out = tmp_path / "out"
    code = record_mod.main(["build", "--all", str(portfolio), "--out", str(out)])
    assert code == 0

    index = (out / "index.html").read_text(encoding="utf-8")
    alpha = (out / "projects" / "alpha.html").read_text(encoding="utf-8")
    beta = (out / "projects" / "beta.html").read_text(encoding="utf-8")

    assert "Declared projects" in index
    assert "Alpha" in index and "Beta" in index
    assert "0/1 host done" in alpha
    assert "shared/shared-spec" in alpha
    assert "self-reported" in alpha
    assert "0/1 host done" in beta
