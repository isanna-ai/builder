from __future__ import annotations

from pathlib import Path
from typing import Any

from .canonical import validate_canonical_artifact
from .common import ValidationContext


def run(context: ValidationContext):
    return validate_canonical_artifact(
        context,
        artifact_name="dependencies",
        source_file="dependencies.yaml",
        schema_file="dependencies.schema.yaml",
        render=None,
        rendered_file=None,
        extra_validation=lambda data, source_name: validate_dependencies(data, source_name, context.spec_dir),
    )


def validate_dependencies(data: dict[str, Any], source_name: str, spec_dir: Path) -> list[str]:
    errors: list[str] = []
    current_spec_name = str(data.get("spec", "")).strip() or spec_dir.name
    if current_spec_name != spec_dir.name:
        errors.append(
            f"{source_name}: spec field `{current_spec_name}` does not match containing spec directory `{spec_dir.name}`"
        )

    dependencies = data.get("dependencies") if isinstance(data.get("dependencies"), list) else []
    sibling_root = spec_dir.parent
    seen_targets: set[str] = set()

    for index, dependency in enumerate(dependencies, start=1):
        if not isinstance(dependency, dict):
            continue
        target_spec = str(dependency.get("spec", "")).strip()
        location = f"{source_name}.dependencies[{index}]"
        if not target_spec:
            continue
        if target_spec == current_spec_name:
            errors.append(f"{location}.spec: self dependency is not allowed")
        if target_spec in seen_targets:
            errors.append(f"{location}.spec: duplicate dependency target `{target_spec}`")
        else:
            seen_targets.add(target_spec)

        if not (sibling_root / target_spec).is_dir():
            errors.append(f"{location}.spec: unknown sibling spec `{target_spec}`")

    # R4: same-repo cycle detection over `required` (dispatch-gating) edges ONLY.
    # `kind: contextual`/`optional` deps are informational and never gate dispatch
    # (see scheduler._unmet_dependencies), so a contextual "see also" cycle cannot
    # deadlock anything and must NOT be reported — flagging it would both false-alarm
    # and diverge from R4's own gating rule. Walk the sibling graph from this spec's
    # OWN required, self-excluded edges; every other node is resolved by reading ITS
    # OWN dependencies.yaml off disk. A missing/malformed sibling file is a dead end
    # (never crashes the walk) — same posture as the hygiene checks above.
    direct_targets = sorted(
        {target for target in _required_targets(dependencies) if target != current_spec_name}
    )
    cycle = _find_dependency_cycle(sibling_root, current_spec_name, direct_targets)
    if cycle is not None:
        errors.append(f"{source_name}: dependency cycle detected: {' -> '.join(cycle)}")

    return errors


_NON_GATING_KINDS = ("contextual", "optional")


def _required_targets(deps: Any) -> list[str]:
    """Target spec names of `required` (dispatch-gating) dependencies only, in
    declared order. A missing/blank `kind` defaults to `required` (fail-closed,
    matching scheduler._unmet_dependencies); `contextual`/`optional` are dropped."""
    if not isinstance(deps, list):
        return []
    targets: list[str] = []
    for dependency in deps:
        if not isinstance(dependency, dict):
            continue
        kind = str(dependency.get("kind", "required")).strip().lower()
        if kind in _NON_GATING_KINDS:
            continue
        target = str(dependency.get("spec", "")).strip()
        if target:
            targets.append(target)
    return targets


def _sibling_dependency_targets(sibling_root: Path, spec_name: str) -> list[str]:
    """Best-effort read of a sibling spec's OWN `required` dependency target names
    (contextual/optional edges excluded, same as the start node). Missing/malformed
    files are skipped (dead end, never crashes the graph walk) — same-repo only."""
    path = sibling_root / spec_name / "dependencies.yaml"
    if not path.is_file():
        return []
    try:
        from _yaml import yaml  # type: ignore

        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001 - a corrupt sibling file must never crash validation
        return []
    if not isinstance(data, dict):
        return []
    return _required_targets(data.get("dependencies"))


def _find_dependency_cycle(sibling_root: Path, start: str, start_targets: list[str]) -> list[str] | None:
    """DFS the same-repo sibling dependency graph from `start`, using the
    already-parsed `start_targets` as start's own edges and reading each other
    node's dependencies.yaml for its edges. Returns the cycle path
    (`[start, ..., start]`) if the walk returns to `start`, else None. A global
    visited set bounds the walk (avoids re-exploring shared subgraphs / looping on
    a cycle that does not involve `start`)."""
    visited: set[str] = set()

    def edges_of(node: str) -> list[str]:
        return start_targets if node == start else _sibling_dependency_targets(sibling_root, node)

    def dfs(node: str, path: list[str]) -> list[str] | None:
        for target in edges_of(node):
            if target == start:
                return path + [target]
            if target in visited:
                continue
            visited.add(target)
            found = dfs(target, path + [target])
            if found is not None:
                return found
        return None

    return dfs(start, [start])
