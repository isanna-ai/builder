from __future__ import annotations

from pathlib import Path
from typing import Any

from .runtime import RUNTIME_DIR_NAMES

from .common import CheckResult, ValidationContext, load_schema, parse_yaml_like_file, resolve_artifact_mode


DERIVED_SPEC_MARKDOWN = {
    "constitution-review.md",
    "design.md",
    "handoff.md",
    "requirements.md",
    "review-log.md",
    "tasks.md",
}


def _root_from_spec(spec_dir: Path) -> Path:
    if len(spec_dir.parts) >= 3 and spec_dir.parts[-3] in RUNTIME_DIR_NAMES and spec_dir.parts[-2] == "specs":
        return spec_dir.parents[2]
    return spec_dir.parent


def _profile(root: Path, name: str) -> dict[str, Any]:
    data, _ = load_schema("runner.schema.yaml")
    profiles = data.get("properties", {}).get("model_profiles", {}).get("properties", {})
    profile = profiles.get(name)
    return profile if isinstance(profile, dict) else {}


def _tasks_by_id(spec_dir: Path) -> dict[str, dict[str, Any]]:
    data, _ = parse_yaml_like_file(spec_dir / "tasks.yaml")
    return {str(t.get("id")): t for t in data.get("tasks", []) if isinstance(t, dict)}


def _is_derived_spec_markdown(path_value: Any) -> bool:
    path = Path(str(path_value))
    return path.suffix == ".md" and path.name in DERIVED_SPEC_MARKDOWN


def _requires_rendered_review(task: dict[str, Any]) -> bool:
    human_gate = str(task.get("human_gate", "")).lower()
    return "render" in human_gate and ("review" in human_gate or "markdown" in human_gate)


def run(context: ValidationContext) -> CheckResult:
    spec, spec_errors = parse_yaml_like_file(context.spec_dir / "spec.yaml")
    if spec_errors:
        return CheckResult("packet_fit", spec_errors)
    profile_name = str(spec.get("target_model_profile", "")).strip()
    if not profile_name:
        return CheckResult("packet_fit", [], skipped=True, skip_message="packet_fit skipped: no target_model_profile")

    root = _root_from_spec(context.spec_dir)
    profile = _profile(root, profile_name)
    artifact_mode = resolve_artifact_mode(context)
    errors: list[str] = []
    if not profile:
        return CheckResult("packet_fit", [f"unknown target_model_profile `{profile_name}`"])
    allow_rendered_markdown = profile.get("allow_rendered_markdown") is not False

    trace, trace_errors = parse_yaml_like_file(context.spec_dir / "traceability.yaml")
    errors.extend(trace_errors)
    tasks = _tasks_by_id(context.spec_dir)
    task_links = trace.get("task_links") if isinstance(trace.get("task_links"), list) else []
    for link in task_links:
        if not isinstance(link, dict):
            continue
        task_id = str(link.get("task_id", "")).strip()
        files = link.get("files") if isinstance(link.get("files"), list) else []
        must_files = [f for f in files if isinstance(f, dict) and str(f.get("load_priority", "must")) == "must"]
        total = sum(int(f.get("estimated_tokens") or 0) for f in must_files)
        full_count = sum(1 for f in must_files if f.get("full_read_eligible", True) is True)
        slice_count = sum(1 for f in must_files if f.get("full_read_eligible", True) is False or f.get("mode") == "anchored")
        task = tasks.get(task_id, {})
        packet_fit = task.get("packet_fit") if isinstance(task.get("packet_fit"), dict) else {}
        declared = packet_fit.get("initial_packet_tokens")
        for file_entry in must_files:
            path = str(file_entry.get("path", "")).strip()
            if not _is_derived_spec_markdown(path):
                continue
            if _requires_rendered_review(task):
                continue
            if artifact_mode == "ai_native" or not allow_rendered_markdown:
                errors.append(f"{task_id}: rendered Markdown `{path}` cannot be in required packet load set")
        if declared is not None and int(declared) != total:
            errors.append(f"{task_id}: packet token sum mismatch: declared {declared}, computed {total}")
        if total > int(profile.get("effective_context_tokens", 0)):
            errors.append(f"{task_id}: packet exceeds context budget ({total} > {profile.get('effective_context_tokens')})")
        if total > int(profile.get("initial_packet_cap_tokens", 0)):
            errors.append(f"{task_id}: packet exceeds initial cap ({total} > {profile.get('initial_packet_cap_tokens')})")
        if full_count > int(profile.get("max_full_read_files", 0)):
            errors.append(f"{task_id}: full_read_files count {full_count} exceeds limit {profile.get('max_full_read_files')}")
        if slice_count > int(profile.get("max_slice_files", 0)):
            errors.append(f"{task_id}: slice_read_files count {slice_count} exceeds limit {profile.get('max_slice_files')}")
        if packet_fit.get("status") == "not_fit":
            errors.append(f"{task_id}: packet_fit status not_fit blocks runner-ready approval")

    return CheckResult("packet_fit", errors, total_checks=max(1, len(task_links)), summary=None if errors else "packet_fit: valid")
