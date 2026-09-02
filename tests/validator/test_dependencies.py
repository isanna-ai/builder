"""R4: same-repo dependency-cycle detection in `validate_dependencies`.

Exercises `validate_dependencies` directly (the same function `dependencies.py::run`
delegates to via `validate_canonical_artifact`'s `extra_validation` hook) — no need
for the full canonical-artifact/schema machinery to test the graph walk in
isolation. Shim-safe: no pytest.raises/monkeypatch.
"""

from __future__ import annotations

from pathlib import Path

from scripts._validators.dependencies import validate_dependencies


def _dep(spec: str, kind: str = "required", reason: str = "because") -> dict:
    return {"spec": spec, "kind": kind, "reason": reason}


def _write_sibling(specs_root: Path, name: str, deps: list[dict]) -> None:
    """Write a SIBLING spec's own dependencies.yaml on disk (as the graph walk
    reads it directly, bypassing the in-memory `data` the function under test
    receives for the CURRENT spec)."""
    sibling_dir = specs_root / name
    sibling_dir.mkdir(parents=True, exist_ok=True)
    lines = ["artifact: dependencies", f"spec: {name}", "dependencies:"]
    for dep in deps:
        lines.append(f"  - spec: {dep['spec']}")
        lines.append(f"    kind: {dep['kind']}")
        lines.append(f"    reason: {dep['reason']}")
    (sibling_dir / "dependencies.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _spec_dir(specs_root: Path, name: str) -> Path:
    d = specs_root / name
    d.mkdir(parents=True, exist_ok=True)
    return d


def test_no_dependencies_yields_no_cycle_error(tmp_path):
    specs_root = tmp_path / ".builder" / "specs"
    spec_dir = _spec_dir(specs_root, "solo-spec")
    data = {"spec": "solo-spec", "dependencies": []}

    errors = validate_dependencies(data, "dependencies.yaml", spec_dir)

    assert not any("cycle" in e.lower() for e in errors)


def test_direct_two_node_cycle_is_detected(tmp_path):
    """A -> B (declared in A's own data) and B -> A (on disk) is a cycle."""
    specs_root = tmp_path / ".builder" / "specs"
    spec_dir = _spec_dir(specs_root, "spec-a")
    _write_sibling(specs_root, "spec-b", [_dep("spec-a")])
    data = {"spec": "spec-a", "dependencies": [_dep("spec-b")]}

    errors = validate_dependencies(data, "dependencies.yaml", spec_dir)

    cycle_errors = [e for e in errors if "cycle" in e.lower()]
    assert cycle_errors, f"expected a cycle error, got {errors}"
    assert "spec-a" in cycle_errors[0] and "spec-b" in cycle_errors[0]


def test_three_node_cycle_is_detected(tmp_path):
    """A -> B -> C -> A: validating A must catch the multi-hop cycle."""
    specs_root = tmp_path / ".builder" / "specs"
    spec_dir = _spec_dir(specs_root, "spec-a")
    _write_sibling(specs_root, "spec-b", [_dep("spec-c")])
    _write_sibling(specs_root, "spec-c", [_dep("spec-a")])
    data = {"spec": "spec-a", "dependencies": [_dep("spec-b")]}

    errors = validate_dependencies(data, "dependencies.yaml", spec_dir)

    cycle_errors = [e for e in errors if "cycle" in e.lower()]
    assert cycle_errors, f"expected a cycle error, got {errors}"
    for name in ("spec-a", "spec-b", "spec-c"):
        assert name in cycle_errors[0]


def test_diamond_shape_is_not_a_false_positive_cycle(tmp_path):
    """A -> B, A -> C, B -> D, C -> D (D is a shared dead-end leaf): not a cycle."""
    specs_root = tmp_path / ".builder" / "specs"
    spec_dir = _spec_dir(specs_root, "spec-a")
    _write_sibling(specs_root, "spec-b", [_dep("spec-d")])
    _write_sibling(specs_root, "spec-c", [_dep("spec-d")])
    _write_sibling(specs_root, "spec-d", [])
    data = {"spec": "spec-a", "dependencies": [_dep("spec-b"), _dep("spec-c")]}

    errors = validate_dependencies(data, "dependencies.yaml", spec_dir)

    assert not any("cycle" in e.lower() for e in errors)


def test_missing_sibling_dependencies_file_is_a_dead_end_not_a_crash(tmp_path):
    """A dependency target with no dependencies.yaml of its own (or no dir at all)
    is treated as a dead end — never raises, never falsely reports a cycle."""
    specs_root = tmp_path / ".builder" / "specs"
    spec_dir = _spec_dir(specs_root, "spec-a")
    _spec_dir(specs_root, "spec-b")  # exists, but has no dependencies.yaml
    data = {"spec": "spec-a", "dependencies": [_dep("spec-b")]}

    errors = validate_dependencies(data, "dependencies.yaml", spec_dir)

    assert not any("cycle" in e.lower() for e in errors)


def test_malformed_sibling_dependencies_file_is_skipped_not_a_crash(tmp_path):
    """A sibling's dependencies.yaml that parses to a top-level LIST (not a
    mapping) is malformed for this artifact — raises via `.get()` under both real
    PyYAML and the shim if unguarded. Must be skipped as a dead end, never crash
    the graph walk."""
    specs_root = tmp_path / ".builder" / "specs"
    spec_dir = _spec_dir(specs_root, "spec-a")
    sibling_dir = specs_root / "spec-b"
    sibling_dir.mkdir(parents=True, exist_ok=True)
    (sibling_dir / "dependencies.yaml").write_text("- a\n- b\n", encoding="utf-8")
    data = {"spec": "spec-a", "dependencies": [_dep("spec-b")]}

    errors = validate_dependencies(data, "dependencies.yaml", spec_dir)

    assert not any("cycle" in e.lower() for e in errors)


def test_self_dependency_is_not_double_reported_as_a_cycle(tmp_path):
    """A self-dependency is already flagged distinctly ('self dependency is not
    allowed'); it must not ALSO produce a redundant 'cycle detected' message."""
    specs_root = tmp_path / ".builder" / "specs"
    spec_dir = _spec_dir(specs_root, "spec-a")
    data = {"spec": "spec-a", "dependencies": [_dep("spec-a")]}

    errors = validate_dependencies(data, "dependencies.yaml", spec_dir)

    assert any("self dependency" in e.lower() for e in errors)
    assert not any("cycle" in e.lower() for e in errors)


def test_contextual_backedge_is_not_a_gating_cycle(tmp_path):
    """Cycle detection tracks ONLY `required` (dispatch-gating) edges: A -> B
    (required) with B -> A (contextual) is NOT a deadlock — B dispatches without
    waiting on A — so it must not be reported. (An all-`required` cycle is still
    caught: see test_direct_two_node_cycle_is_detected.)"""
    specs_root = tmp_path / ".builder" / "specs"
    spec_dir = _spec_dir(specs_root, "spec-a")
    _write_sibling(specs_root, "spec-b", [_dep("spec-a", kind="contextual")])
    data = {"spec": "spec-a", "dependencies": [_dep("spec-b", kind="required")]}

    errors = validate_dependencies(data, "dependencies.yaml", spec_dir)

    assert not any("cycle" in e.lower() for e in errors)
