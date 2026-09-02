from __future__ import annotations

from pathlib import Path
from typing import Any

from .runtime import RUNTIME_DIR_NAMES

from .common import CheckResult, ValidationContext, parse_yaml_like_file


VALID_SCOPES = {"full", "security_only", "architecture_only", "none"}


def _root_from_spec(spec_dir: Path) -> Path:
    if len(spec_dir.parts) >= 3 and spec_dir.parts[-3] in RUNTIME_DIR_NAMES and spec_dir.parts[-2] == "specs":
        return spec_dir.parents[2]
    return spec_dir.parent


def _allow_list(root: Path) -> list[str]:
    data, _ = parse_yaml_like_file(root / "schemas" / "runner.schema.yaml")
    examples = data.get("properties", {}).get("shell_allow_list", {}).get("examples", [])
    return [str(item) for item in examples] if isinstance(examples, list) else []


def run(context: ValidationContext) -> CheckResult:
    spec, spec_errors = parse_yaml_like_file(context.spec_dir / "spec.yaml")
    if spec_errors:
        return CheckResult("runner_ready", spec_errors)
    if not str(spec.get("target_model_profile", "")).strip():
        return CheckResult("runner_ready", [], summary="runner_ready: legacy mode")

    errors: list[str] = []
    tasks, task_errors = parse_yaml_like_file(context.spec_dir / "tasks.yaml")
    errors.extend(task_errors)
    for task in tasks.get("tasks", []) if isinstance(tasks.get("tasks"), list) else []:
        if isinstance(task, dict) and "human_gate" in task:
            errors.append(f"Task {task.get('id')} has HUMAN GATE but target_model_profile is set - remove human_gate or unset profile")

    decisions_path = context.spec_dir / "decisions.yaml"
    if decisions_path.exists():
        decisions, decision_errors = parse_yaml_like_file(decisions_path)
        errors.extend(decision_errors)
        entries = decisions.get("decisions", decisions.get("entries", []))
        for entry in entries if isinstance(entries, list) else []:
            if isinstance(entry, dict) and str(entry.get("status", "")).strip().lower() == "unresolved":
                phase = str(entry.get("phase", "")).strip()
                where = f"phase {phase}" if phase else "its owning phase"
                errors.append(f"Decision {entry.get('id')} is unresolved - resolve it in {where} (set status: resolved with chosen + rationale) before runner-ready approval")

    root = _root_from_spec(context.spec_dir)
    allowed = _allow_list(root)
    for item in spec.get("environment_readiness", []) if isinstance(spec.get("environment_readiness"), list) else []:
        if not isinstance(item, dict):
            continue
        verify = str(item.get("verify", "")).strip()
        if verify and not any(verify.startswith(prefix) for prefix in allowed):
            errors.append(f"environment_readiness {item.get('id')}: verify command not allow-listed: {verify}")

    post = spec.get("post_runner_review")
    if isinstance(post, dict):
        scope = str(post.get("scope", "")).strip()
        if scope and scope not in VALID_SCOPES:
            errors.append(f"post_runner_review.scope invalid: {scope}")

    return CheckResult("runner_ready", errors, summary=None if errors else "runner_ready: valid")
