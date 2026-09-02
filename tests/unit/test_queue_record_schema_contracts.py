from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
FIXTURES = ROOT / "tests" / "fixtures" / "builder_project_model" / "queue" / "v1"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from _builder_project_model.common import ValidationError
from _builder_project_model.queue_schema import (
    parse_attempt_record,
    parse_event_record,
    parse_lane_record,
    parse_work_item_record,
)
from _dispatch_runtime.queue_store import AttemptRecord, QueueStore, WorkItem


def _path(name: str) -> Path:
    return FIXTURES / name


def test_queue_record_schemas_accept_known_good_current_shapes():
    assert parse_work_item_record(_path("item-good.yaml")).kind == "item"
    assert parse_attempt_record(_path("attempt-good.yaml")).kind == "attempt"
    assert parse_lane_record(_path("lane-good.yaml")).kind == "lane"
    assert parse_event_record(_path("event-good.yaml")).kind == "event"


def test_queue_record_schemas_reject_bad_state_metadata_and_payload_shapes():
    try:
        parse_work_item_record(_path("item-bad-state.yaml"))
    except ValidationError as exc:
        assert any("unknown work-item state" in issue.render() for issue in exc.issues)
    else:
        raise AssertionError("expected bad-state rejection")

    try:
        parse_attempt_record(_path("attempt-bad-metadata.yaml"))
    except ValidationError as exc:
        assert any("metadata must be a mapping" in issue.render() for issue in exc.issues)
    else:
        raise AssertionError("expected metadata-shape rejection")

    try:
        parse_event_record(_path("event-bad-payload.yaml"))
    except ValidationError as exc:
        rendered = [issue.render() for issue in exc.issues]
        assert any("unknown event_type" in issue for issue in rendered)
        assert any("payload must be a mapping" in issue for issue in rendered)
    else:
        raise AssertionError("expected payload rejection")


def test_reconstruction_characterization_keeps_mis_serialized_lease_and_metadata_lenient(tmp_path):
    item = WorkItem.from_record({"id": "w1", "state": "queued", "lease": "{}"})
    attempt = AttemptRecord.from_record({"attempt_id": "a1", "work_id": "w1", "lane": "codex-cli", "metadata": "{}"})

    assert item.lease == {}
    assert attempt.metadata == {}

    store = QueueStore(tmp_path)
    (store.items_dir / "w1.yaml").write_text(_path("item-good.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    (store.attempts_dir / "a1.yaml").write_text(_path("attempt-good.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    (store.lanes_dir / "codex-cli.yaml").write_text(_path("lane-good.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    (store.events_dir / "event-1.yaml").write_text(_path("event-good.yaml").read_text(encoding="utf-8"), encoding="utf-8")

    snapshot = store.reconstruct()
    assert snapshot.items["w1"].state.value == "queued"
    assert snapshot.attempts["a1"].metadata["pid"] == 1234
    assert snapshot.lanes["codex-cli"].reason == "rate_limited"
    assert snapshot.events[0].event_type == "attempt_recorded"
