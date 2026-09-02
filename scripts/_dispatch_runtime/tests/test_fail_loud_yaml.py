"""R12: a corrupt control-plane YAML must fail LOUD (block for human repair), not
silently degrade to {} and re-run the spec phase over approved artifacts.

Written to pass under BOTH the repo's permissive yaml shim and real PyYAML (and the
repo's vendored pytest, so no pytest.raises/monkeypatch):
- a non-mapping (YAML list) raises MalformedControlFile under either backend;
- a garbage-phase spec.yaml HUMAN_BLOCKs the lane under either backend (real yaml ->
  MalformedControlFile, shim -> the ValueError it degrades to — both caught).
"""

from __future__ import annotations

from _dispatch_runtime.lane_executor import DispatchResultType
from _dispatch_runtime.phase_runtime import MalformedControlFile, detect_phase, load_control_yaml

_LIST_YAML = "- a\n- b\n"                     # non-mapping: raises under BOTH backends
_UNRESOLVABLE = "current_phase: [1, 2, 3\n"   # no valid phase (raise OR garbage) -> HUMAN_BLOCK either way


def _raises(fn, exc) -> bool:
    try:
        fn()
        return False
    except exc:
        return True


def _spec_dir(tmp_path, contents=None):
    d = tmp_path / ".builder" / "specs" / "demo"
    d.mkdir(parents=True, exist_ok=True)
    if contents is not None:
        (d / "spec.yaml").write_text(contents, encoding="utf-8")
    return d


def test_load_control_yaml_missing_returns_none(tmp_path):
    assert load_control_yaml(tmp_path / "nope.yaml") is None


def test_load_control_yaml_rejects_non_mapping(tmp_path):
    p = tmp_path / "spec.yaml"
    p.write_text(_LIST_YAML, encoding="utf-8")
    assert _raises(lambda: load_control_yaml(p), MalformedControlFile)


def test_load_control_yaml_reads_valid_mapping(tmp_path):
    p = tmp_path / "spec.yaml"
    p.write_text("current_phase: implement\n", encoding="utf-8")
    assert load_control_yaml(p) == {"current_phase": "implement"}


def test_detect_phase_raises_on_non_mapping_spec(tmp_path):
    spec_dir = _spec_dir(tmp_path, _LIST_YAML)
    assert _raises(lambda: detect_phase(spec_dir, tmp_path, None), MalformedControlFile)


def test_detect_phase_tolerates_missing_spec(tmp_path):
    # Missing spec.yaml is NOT malformed — no raise (returns None for no phase).
    spec_dir = _spec_dir(tmp_path, None)
    assert detect_phase(spec_dir, tmp_path, None) is None


def test_detect_phase_reads_valid_spec(tmp_path):
    spec_dir = _spec_dir(tmp_path, "name: demo\ncurrent_phase: implement\n")
    assert detect_phase(spec_dir, tmp_path, None) == "implement"


def _lane_result(lane_cls, tmp_path, content):
    _spec_dir(tmp_path, content)
    task_ref = {"kind": "builder-phase-batch", "spec_id": "demo"}
    ctx = {"work_id": "w1", "attempt_id": "a1", "workspace_root": str(tmp_path)}
    return lane_cls().execute(task_ref, "lane", ctx)


def test_claude_lane_human_blocks_on_unresolvable_spec(tmp_path):
    from _dispatch_runtime.lane_claude_code_cli import ClaudeCodeCliLane
    r = _lane_result(ClaudeCodeCliLane, tmp_path, _UNRESOLVABLE)
    assert r.result_type == DispatchResultType.HUMAN_BLOCK


def test_codex_lane_human_blocks_on_unresolvable_spec(tmp_path):
    from _dispatch_runtime.lane_codex_cli import CodexCliLane
    r = _lane_result(CodexCliLane, tmp_path, _UNRESOLVABLE)
    assert r.result_type == DispatchResultType.HUMAN_BLOCK


def test_claude_lane_human_blocks_on_non_mapping_spec(tmp_path):
    from _dispatch_runtime.lane_claude_code_cli import ClaudeCodeCliLane
    r = _lane_result(ClaudeCodeCliLane, tmp_path, _LIST_YAML)
    assert r.result_type == DispatchResultType.HUMAN_BLOCK
