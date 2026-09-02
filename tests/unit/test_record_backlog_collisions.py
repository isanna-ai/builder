from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

record_spec = importlib.util.spec_from_file_location("record_backlog_collisions", SCRIPTS / "record.py")
record = importlib.util.module_from_spec(record_spec)
sys.modules["record_backlog_collisions"] = record
record_spec.loader.exec_module(record)


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _seed(root: Path) -> None:
    (root / ".builder").mkdir(parents=True)
    _write(root / ".builder" / "dispatch.yaml", {"queue_store": {"path": ".builder/dispatch-queue"}})
    _write(
        root / ".builder" / "product.yaml",
        {"product": "demo", "title": "Demo", "repos": [{"alias": root.name}]},
    )
    _write(
        root / ".builder" / "releases" / "now.yaml",
        {
            "release": "now",
            "product": "demo",
            "title": "Now",
            "status": "active",
            "intents": ["alpha", "beta"],
        },
    )
    for intent_id, change in (("alpha", "create"), ("beta", "rewire")):
        _write(
            root / ".builder" / "intents" / intent_id / "intent.yaml",
            {
                "artifact": "intent-object",
                "intent": intent_id,
                "title": intent_id.title(),
                "status": "accepted",
                "problem": "p",
                "why": "w",
                "success_criteria": [{"id": "sc-1", "statement": "s"}],
                "non_goals": ["n"],
                "ssot_delta": {
                    "capabilities": [{"target": "capability.search", "change": change}],
                    "behaviors": [],
                    "journeys": [],
                },
                "specs": [],
            },
        )


def _assert_claimed_collision_panel(page: str) -> None:
    start = page.index('<section class="panel backlog-capabilities">')
    panel = page[start:page.index("</section>", start)]
    assert "Active backlog · claimed register" in panel
    assert "capability.search" in panel
    assert "intent <strong>alpha</strong> · release now · lifecycle accepted · change create" in panel
    assert "intent <strong>beta</strong> · release now · lifecycle accepted · change rewire" in panel
    assert '<span class="chip claimed">claimed</span>' in panel
    assert '<span class="chip attention">collision</span>' in panel
    assert "host-seal" not in panel
    assert "badge--host" not in panel
    assert "stamp" not in panel


def test_record_renders_backlog_collisions_on_release_and_backlog_views_as_claimed_only(tmp_path):
    root = tmp_path / "demo-repo"
    _seed(root)
    registry = record.planning._registry(root, str(tmp_path))
    release = record.planning.find_release(root, "now")
    before = record.planning.completeness(release, registry)
    out = tmp_path / "out"

    assert record.main(["build", "--all", str(tmp_path), "--out", str(out)]) == 0

    roadmap = (out / root.name / "roadmap.html").read_text(encoding="utf-8")
    releases = (out / root.name / "releases.html").read_text(encoding="utf-8")
    project = (out / "projects" / "demo.html").read_text(encoding="utf-8")
    for page in (roadmap, releases, project):
        _assert_claimed_collision_panel(page)
    after = record.planning.completeness(release, registry)
    assert (after.verified, after.total, after.percent) == (before.verified, before.total, before.percent)
    assert "0%" in project
