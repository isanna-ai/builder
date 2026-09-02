from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"


def _load_record():
    spec = importlib.util.spec_from_file_location("record_intent_hierarchy_under_test", SCRIPTS / "record.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_record_renders_claimed_intent_over_separate_spec_status_and_host_verdict(tmp_path):
    record = _load_record()
    root = tmp_path / "demo"
    spec_dir = root / ".builder" / "specs" / "member"
    spec_dir.mkdir(parents=True)
    (spec_dir / "spec.yaml").write_text("name: Member\nstatus: synced\n", encoding="utf-8")
    release = root / ".builder" / "releases" / "demo.yaml"
    release.parent.mkdir(parents=True)
    release.write_text(
        "release: demo\nproduct: demo\ntitle: Demo release\nstatus: active\nintents:\n  - demo-intent\n",
        encoding="utf-8",
    )
    product = root / ".builder" / "product.yaml"
    product.write_text("product: demo\ntitle: Demo\nrepos:\n  - alias: demo\n", encoding="utf-8")
    intent = root / ".builder" / "intents" / "demo-intent" / "intent.yaml"
    intent.parent.mkdir(parents=True)
    intent.write_text(
        "artifact: intent-object\nintent: demo-intent\ntitle: Demo intent\nstatus: accepted\n"
        "problem: p\nwhy: w\nsuccess_criteria:\n  - id: sc-1\n    statement: s\n"
        "non_goals:\n  - n\nssot_delta:\n  capabilities: []\n  behaviors: []\n  journeys: []\n"
        "specs:\n  - member\n",
        encoding="utf-8",
    )
    record.planning._scan_cache[str(root.resolve())] = {
        "specs": [{"spec": "member", "verification": "host-verified"}]
    }

    html = record._releases("demo", root, {"member": spec_dir})

    assert "claimed intent" in html and "Demo intent" in html
    assert "canonical status: synced" in html
    assert "host-verified" in html
    assert "1/1 · 100% done" in html
    assert "claimed intent</span>" in html
