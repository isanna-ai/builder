from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .common import (
    CheckResult,
    VALID_ARTIFACT_MODES,
    VALID_TDD_EXEMPT_REASONS,
    ValidationContext,
    _parse_entry,
    mapping_list,
    parse_yaml_like_file,
    string_list,
)
from .evidence import load_task_evidence, normalize_task_id

REQUIRED_TASK_FIELDS = [
    "Repo",
    "Files",
    "TDD",
    "Steps",
    "Verify",
    "Done when",
    "Depends on",
    "Parallel with",
]

VALID_STATUS_VALUES: set[str] = {
    "specifying",
    "specified",
    "spec-reviewed",
    "designed",
    "reviewed",
    "planned",
    "implementing",
    "implemented",
    "adversarially-reviewed",
    "verifying",
    "verified",
    "verified_with_tasks",
    "syncing",
    "synced",
    "archived",
}

SPEC_YAML_REQUIRED_FIELDS = ["name", "created", "status", "current_phase", "next_action"]
DECISIONS_REQUIRED_FIELDS = ["id", "phase", "timestamp", "question"]
# chosen/rationale are required only for a resolved decision; an unresolved
# decision records an open question with no answer yet (contract: status
# resolved|unresolved). Absent status == resolved for back-compat.
DECISIONS_RESOLVED_REQUIRED_FIELDS = ["chosen", "rationale"]

SYSTEM_MODEL_REQUIRED_TOP_LEVEL = [
    "version",
    "what",
    "who",
    "when",
    "where",
    "why",
    "how",
    "upstream",
    "downstream",
]

SYSTEM_MODEL_REQUIRED_NESTED = {
    "what": ["entities", "capabilities"],
    "who": ["actors"],
    "when": ["events"],
    "where": ["boundaries"],
    "why": ["rules"],
    "how": ["behaviors"],
    "upstream": ["sources"],
    "downstream": ["sinks"],
}

SYSTEM_MODEL_ENTRY_FIELDS = {
    "entities": ["id", "name"],
    "capabilities": ["id", "name"],
    "actors": ["id", "name", "capabilities"],
    "events": ["id", "name", "trigger"],
    "boundaries": ["id", "name", "purpose"],
    "rules": ["id", "statement", "applies_to"],
    "behaviors": ["capability", "success", "failures"],
    "sources": ["id", "name", "contract"],
    "sinks": ["id", "name", "contract"],
}

TASK_HEADER_RE = re.compile(r"^- \[[ x]\] (\d+)\. (.+)$")
FIELD_RE = re.compile(r"^\s*-\s*\*\*(?P<name>[A-Za-z ]+):\*\*\s*(?P<value>.*)$")
STEP_ITEM_RE = re.compile(r"^\s*\d+\.\s+(.+)$")


@dataclass
class Task:
    number: int
    title: str
    line: int
    fields: dict[str, str] = field(default_factory=dict)
    steps: list[str] = field(default_factory=list)
    verify_lines: list[str] = field(default_factory=list)
    files_block: list[str] = field(default_factory=list)


def extract_contract_block(name: str, contract_path: Path) -> list[str]:
    try:
        text = contract_path.read_text(encoding="utf-8")
    except OSError:
        return []

    appendix_match = re.search(r"^## Machine-readable Appendix$", text, re.MULTILINE)
    if not appendix_match:
        return []
    appendix = text[appendix_match.start():]
    block_match = re.search(rf"^```yaml {re.escape(name)}\n(.*?)^```", appendix, re.MULTILINE | re.DOTALL)
    if not block_match:
        return []
    values: list[str] = []
    for line in block_match.group(1).splitlines():
        stripped = line.strip()
        if stripped.startswith("- "):
            values.append(stripped[2:].strip().strip("\"'"))
    return values


def run_spec_yaml(context: ValidationContext) -> CheckResult:
    path = context.spec_dir / "spec.yaml"
    if not path.exists():
        return CheckResult("spec.yaml", [], skipped=True, skip_message=f"spec.yaml not found at {path}")
    errors = validate_spec_yaml(path, context.contract_path, context.strict)
    return CheckResult("spec.yaml", errors, summary=None if errors else "spec.yaml: valid")


# Bookkeeping counts that NOTHING maintains. No template writes them, no prompt emits them,
# no code derives or reads them -- before this deprecation the strict-mode allowlist below was
# their only mention anywhere in the repository. They are typed by hand once and never
# reconciled against anything again.
#
# That is not a tidiness problem. `beta-approve-funnel` sat at `status: planned`,
# `tasks_done: 0 / 11` while every one of its headline deliverables was live in production:
# the declared number said "nothing built", and a reader who trusted it would have rebuilt
# shipped functionality. An unmaintained number is not neutral -- it is read as fact.
#
# Deprecated rather than hard-removed because 33 spec.yaml files across five repositories
# still carry these fields, 30 of them in repositories this change does not own.
DEPRECATED_BOOKKEEPING_FIELDS = ("task_count", "tasks_done", "tasks_total", "tasks_parallelizable")


def _spec_bookkeeping_enforced() -> bool:
    """Staged exactly like BUILDER_TRACE_COVERAGE and BUILDER_VERIFY_LINT: `warn` (default)
    records an advisory on stderr and never blocks; `enforce` promotes it to a hard error.
    Anything else -- including a typo -- stays at warn rather than silently enforcing."""
    return (os.environ.get("BUILDER_SPEC_BOOKKEEPING", "warn") or "warn").strip().lower() == "enforce"


def validate_spec_yaml(path: Path, contract_path: Optional[Path], strict: bool) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return [f"{path.name}: cannot read ({exc})"]

    data: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line or line.startswith("#") or line.startswith(" "):
            continue
        if ":" in line:
            key, _, value = line.partition(":")
            data[key.strip()] = value.strip().strip("\"'")

    return validate_spec_yaml_data(data, path.name, contract_path, strict=strict)


def validate_spec_yaml_data(
    data: dict, name: str, contract_path: Optional[Path] = None, *, strict: bool = False,
) -> list[str]:
    """Validate an already-parsed spec.yaml mapping. Split out from `validate_spec_yaml` so the
    rules can be exercised directly on a mapping instead of round-tripping through a file."""
    errors: list[str] = []

    for field_name in SPEC_YAML_REQUIRED_FIELDS:
        if field_name not in data:
            errors.append(f"{name}: missing required field '{field_name}'")

    valid_statuses: set[str] = set(VALID_STATUS_VALUES)
    if contract_path and contract_path.is_file():
        contract_statuses = extract_contract_block("status-enum", contract_path)
        if contract_statuses:
            valid_statuses = set(contract_statuses)
    status = data.get("status", "")
    if status and status not in valid_statuses:
        errors.append(f"{name}: invalid status '{status}' (not in contract status-enum)")

    artifact_mode = data.get("artifact_mode", "")
    if artifact_mode and artifact_mode not in VALID_ARTIFACT_MODES:
        errors.append(f"{name}: invalid artifact_mode '{artifact_mode}' (expected one of {sorted(VALID_ARTIFACT_MODES)})")

    reviews = data.get("reviews", "")
    if reviews and reviews not in {"0", "1", "2"}:
        if re.fullmatch(r"\d+", reviews) and int(reviews) > 2:
            errors.append(f"reviews: {reviews} is not supported; use 0, 1, or 2")
        else:
            errors.append(f"{name}: invalid reviews '{reviews}' (expected 0, 1, or 2)")

    # Presence, not truthiness: `tasks_done: 0` is the exact shape that misled a reader on
    # beta-approve-funnel, and a truthiness test would skip precisely that case.
    deprecated_present = [f for f in DEPRECATED_BOOKKEEPING_FIELDS if f in data]
    if deprecated_present:
        message = (
            f"{name}: deprecated bookkeeping field(s) {', '.join(deprecated_present)} -- nothing "
            f"writes, derives, or reads these, so the number is unmaintained and must not be read "
            f"as progress; delete them and measure with `isanna verify --spec <name>` instead"
        )
        if _spec_bookkeeping_enforced():
            errors.append(message)
        else:
            print(f"WARN  {message} (BUILDER_SPEC_BOOKKEEPING=warn)", file=sys.stderr)

    if strict:
        # The deprecated fields stay in this allowlist deliberately: the deprecation check
        # above owns their message, and reporting the same field twice through two different
        # channels trains readers to skim both.
        known_fields = set(SPEC_YAML_REQUIRED_FIELDS) | set(DEPRECATED_BOOKKEEPING_FIELDS) | {"next_model_class", "used_model_class", "artifact_mode", "target_model_profile", "reviews", "summary"}
        for key in data:
            if key not in known_fields:
                errors.append(f"{name}: unknown field '{key}' (strict mode)")

    return errors


def run_system_model(context: ValidationContext) -> CheckResult:
    path = context.spec_dir / "system-model.yaml"
    if not path.exists():
        return CheckResult("system-model.yaml", [f"system-model.yaml: required file not found at {path}"], summary=None)
    errors = validate_system_model(path)
    return CheckResult("system-model.yaml", errors, summary=None if errors else "system-model.yaml: valid")


def validate_system_model(path: Path) -> list[str]:
    data, load_errors = parse_yaml_like_file(path)
    if load_errors:
        return load_errors

    errors: list[str] = []
    path_name = path.name
    for field_name in SYSTEM_MODEL_REQUIRED_TOP_LEVEL:
        if field_name not in data:
            errors.append(f"{path_name}: missing required top-level field '{field_name}'")

    defined_ids: dict[str, set[str]] = {
        "entities": set(),
        "capabilities": set(),
        "actors": set(),
        "events": set(),
        "boundaries": set(),
        "rules": set(),
        "sources": set(),
        "sinks": set(),
    }
    section_entries: dict[str, list[dict[str, Any]]] = {}

    for section_name, nested_fields in SYSTEM_MODEL_REQUIRED_NESTED.items():
        section_value = data.get(section_name)
        if not isinstance(section_value, dict):
            errors.append(f"{path_name}: `{section_name}` must be a mapping")
            continue
        for nested_name in nested_fields:
            if nested_name not in section_value:
                errors.append(f"{path_name}: missing required field `{section_name}.{nested_name}`")
                continue
            entries = mapping_list(path_name, f"{section_name}.{nested_name}", section_value.get(nested_name), errors)
            section_entries[nested_name] = entries
            for index, entry in enumerate(entries, start=1):
                for required in SYSTEM_MODEL_ENTRY_FIELDS[nested_name]:
                    if required not in entry:
                        errors.append(f"{path_name}: `{section_name}.{nested_name}[{index}]` missing field `{required}`")
                entry_id = str(entry.get("id", "")).strip()
                if nested_name in defined_ids and entry_id:
                    defined_ids[nested_name].add(entry_id)

    defined_refs = set().union(*defined_ids.values())
    defined_capabilities = defined_ids["capabilities"]

    for index, actor in enumerate(section_entries.get("actors", []), start=1):
        for capability in string_list(actor.get("capabilities")):
            if capability not in defined_capabilities:
                errors.append(f"{path_name}: `who.actors[{index}].capabilities` references unknown capability `{capability}`")

    for index, behavior in enumerate(section_entries.get("behaviors", []), start=1):
        capability = str(behavior.get("capability", "")).strip()
        if capability and capability not in defined_capabilities:
            errors.append(f"{path_name}: `how.behaviors[{index}].capability` references unknown capability `{capability}`")

    for index, rule in enumerate(section_entries.get("rules", []), start=1):
        for ref in string_list(rule.get("applies_to")):
            if ref not in defined_refs:
                errors.append(f"{path_name}: `why.rules[{index}].applies_to` references unknown id `{ref}`")

    return errors


def run_tasks_md(context: ValidationContext) -> CheckResult:
    path = context.spec_dir / "tasks.md"
    if not path.exists():
        return CheckResult("tasks.md", [], skipped=True, skip_message=f"tasks.md not found at {path}", total_checks=0)
    tasks, parse_errors = parse_tasks(path)
    errors = list(parse_errors)
    for task in tasks:
        errors.extend(validate_task(task))
    return CheckResult("tasks.md", errors, total_checks=len(tasks), summary=f"tasks.md: {len(tasks)} tasks parsed")


def parse_tasks(path: Path) -> tuple[list[Task], list[str]]:
    errors: list[str] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return [], [f"{path}: cannot read ({exc})"]

    tasks: list[Task] = []
    current: Optional[Task] = None
    current_field: Optional[str] = None
    in_code_fence = False

    for index, raw in enumerate(text.splitlines(), start=1):
        line = raw.rstrip("\n")
        stripped = line.strip()

        header = TASK_HEADER_RE.match(line)
        if header and not in_code_fence:
            current = Task(number=int(header.group(1)), title=header.group(2).strip(), line=index)
            tasks.append(current)
            current_field = None
            continue
        if current is None:
            continue
        if stripped.startswith("```"):
            in_code_fence = not in_code_fence
            if current_field == "Verify":
                current.verify_lines.append(line)
            continue

        match = FIELD_RE.match(line)
        if match and not in_code_fence:
            name = match.group("name").strip()
            value = match.group("value").strip()
            current.fields[name] = value
            current_field = name
            continue

        if current_field == "Steps":
            step = STEP_ITEM_RE.match(line)
            if step:
                current.steps.append(step.group(1).strip())
        elif current_field == "Files":
            if stripped.startswith("-"):
                current.files_block.append(stripped.lstrip("- ").strip())
        elif current_field == "Verify":
            current.verify_lines.append(line)

    return tasks, errors


def validate_task(task: Task) -> list[str]:
    errors: list[str] = []
    prefix = f"tasks.md:{task.line} task {task.number}"

    for required in REQUIRED_TASK_FIELDS:
        if required not in task.fields:
            errors.append(f"{prefix}: missing required field `{required}`")

    tdd_raw = task.fields.get("TDD", "").strip().strip("`")
    if not tdd_raw:
        return errors

    mode = "required"
    if tdd_raw == "required":
        mode = "required"
    else:
        match = re.match(r"^exempt\s*\(([^)]+)\)$", tdd_raw)
        if not match:
            errors.append(f"{prefix}: TDD must be `required` or `exempt (<reason>)`, got `{tdd_raw}`")
            return errors
        mode = "exempt"
        reason = match.group(1).strip()
        if reason not in VALID_TDD_EXEMPT_REASONS:
            errors.append(f"{prefix}: TDD exemption reason `{reason}` not in {sorted(VALID_TDD_EXEMPT_REASONS)}")

    files_field = (task.fields.get("Files", "") + " " + " ".join(task.files_block)).lower()
    has_test_file = "test" in files_field or "_test" in files_field or "/tests/" in files_field
    if mode == "required":
        if not has_test_file:
            errors.append(f"{prefix}: TDD: required but no test file found in Files")
        if not task.steps:
            errors.append(f"{prefix}: TDD: required but Steps are empty")
        else:
            first_step = task.steps[0].lower()
            if not ("fail" in first_step or "red" in first_step or ("write" in first_step and "test" in first_step)):
                errors.append(f"{prefix}: TDD: required but Step 1 does not look like a RED step (got: {task.steps[0][:80]!r})")

    verify_text = "\n".join(task.verify_lines).strip()
    if not verify_text:
        errors.append(f"{prefix}: Verify block is empty")
    elif "```" not in "\n".join(task.verify_lines):
        if not re.search(r"[a-zA-Z_][\w\-]*\s|\$|\|", verify_text):
            errors.append(f"{prefix}: Verify block has no recognizable commands")

    command_lines = [line for line in task.verify_lines if line.strip() and not line.strip().startswith(("```", "#"))]
    if mode == "required" and len(command_lines) < 2:
        errors.append(f"{prefix}: TDD: required Verify must include a focused test command and a project verification command")

    if not task.fields.get("Depends on", "").strip().strip("`"):
        errors.append(f"{prefix}: Depends on must be `none` or a list of task numbers")
    if not task.fields.get("Parallel with", "").strip().strip("`"):
        errors.append(f"{prefix}: Parallel with must be `none` or a list of task numbers")
    return errors


def run_phase_log(context: ValidationContext) -> CheckResult:
    path = context.spec_dir / "phase-log.yaml"
    if not path.exists():
        return CheckResult("phase-log.yaml", [], skipped=True, skip_message=f"phase-log.yaml not found at {path}", total_checks=0)
    phases, parse_errors = parse_phase_log(path)
    errors = list(parse_errors)
    errors.extend(validate_phase_log(phases, path))
    return CheckResult("phase-log.yaml", errors, total_checks=len(phases), summary=f"phase-log.yaml: {len(phases)} phase entries parsed")


def parse_phase_log(path: Path) -> tuple[list[dict[str, Any]], list[str]]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return [], [f"{path}: cannot read ({exc})"]

    try:
        from _yaml import yaml  # type: ignore

        data = yaml.safe_load(text) or {}
        phases = data.get("phases", []) if isinstance(data, dict) else []
        return list(phases), []
    except ImportError:
        pass

    lines = text.splitlines()
    phases: list[dict[str, Any]] = []
    index = 0
    in_phases = False
    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        if not in_phases:
            if stripped == "phases:" or stripped.startswith("phases:"):
                in_phases = True
            index += 1
            continue
        if stripped.startswith("- "):
            base_indent = len(line) - len(line.lstrip(" "))
            entry_lines = [line[base_indent + 2 :]]
            index += 1
            while index < len(lines):
                next_line = lines[index]
                if not next_line.strip():
                    index += 1
                    continue
                if next_line.startswith(" " * (base_indent + 2)):
                    entry_lines.append(next_line[base_indent + 2 :])
                    index += 1
                    continue
                break
            parsed = _parse_entry(entry_lines)
            if isinstance(parsed, dict):
                phases.append(parsed)
        else:
            index += 1
    return phases, []


def validate_phase_log(phases: list[dict[str, Any]], path: Path) -> list[str]:
    errors: list[str] = []
    for phase_index, phase in enumerate(phases, start=1):
        location = f"{path.name}#phase[{phase_index}]"
        if not isinstance(phase, dict):
            errors.append(f"{location}: phase entry is not a mapping")
            continue
        phase_id = phase.get("phase", "<unknown>")
        if "used_model" not in phase or not str(phase.get("used_model", "")).strip():
            errors.append(f"{location} ({phase_id}): missing `used_model`")

        if phase_id == "5-implement":
            tasks = phase.get("tasks") if isinstance(phase.get("tasks"), list) else []
            for task_index, task in enumerate(tasks, start=1):
                task_location = f"{location}/tasks[{task_index}]"
                if not isinstance(task, dict):
                    errors.append(f"{task_location}: task entry is not a mapping")
                    continue
                if task.get("status") != "done":
                    continue
                task_id = normalize_task_id(task.get("task_id") or task.get("task"))
                if not task_id:
                    errors.append(f"{task_location}: missing `task_id`")
                    continue
                tdd = task.get("tdd") if isinstance(task.get("tdd"), dict) else {}
                mode = tdd.get("mode")
                evidence_file = str(task.get("evidence_file", "")).strip()
                if not evidence_file:
                    errors.append(f"{task_location}: missing `evidence_file`")
                    continue
                evidence_data, evidence_errors = load_task_evidence(path.parent, task_id, evidence_file)
                errors.extend(evidence_errors)
                evidence_list = evidence_data.get("entries") if isinstance(evidence_data.get("entries"), list) else []
                evidence_by_step: dict[str, dict[str, Any]] = {}
                for entry in evidence_list:
                    if isinstance(entry, dict) and "step" in entry:
                        evidence_by_step[str(entry["step"])] = entry
                if mode == "required":
                    for step_name in ("red", "green", "verify"):
                        block = evidence_by_step.get(step_name)
                        if block is None:
                            errors.append(f"{task_location}: TDD required but missing '{step_name}' evidence step")
                            continue
                        exit_code = str(block.get("exit_code", ""))
                        if step_name == "red" and exit_code == "0":
                            errors.append(f"{task_location}: RED exit_code must be non-zero")
                        if step_name in {"green", "verify"} and exit_code not in {"0", ""}:
                            errors.append(f"{task_location}: {step_name.upper()} exit_code must be 0, got '{exit_code}'")
                elif mode == "exempt" and "verify" not in evidence_by_step:
                    errors.append(f"{task_location}: TDD exempt task must have a 'verify' evidence step")

        if phase_id == "6-verify":
            verification = phase.get("verification") if isinstance(phase.get("verification"), list) else []
            if not verification:
                errors.append(f"{location} (6-verify): missing `verification` block")
                continue
            for verify_index, group in enumerate(verification, start=1):
                verify_location = f"{location}/verification[{verify_index}]"
                if not isinstance(group, dict):
                    errors.append(f"{verify_location}: verification entry is not a mapping")
                    continue
                if "task_id" not in group or str(group.get("task_id", "")).strip() == "":
                    errors.append(f"{verify_location}: missing `task_id`")
                task_id = normalize_task_id(group.get("task_id"))
                evidence_file = str(group.get("evidence_file", "")).strip()
                if not evidence_file:
                    errors.append(f"{verify_location}: missing `evidence_file`")
                    continue
                evidence_data, evidence_errors = load_task_evidence(path.parent, task_id, evidence_file)
                errors.extend(evidence_errors)
                evidence_list = evidence_data.get("entries") if isinstance(evidence_data.get("entries"), list) else []
                evidence_by_step: dict[str, dict[str, Any]] = {}
                for entry in evidence_list:
                    if isinstance(entry, dict) and "step" in entry:
                        evidence_by_step[str(entry["step"])] = entry
                verify_block = evidence_by_step.get("verify")
                if verify_block is None:
                    errors.append(f"{verify_location}: missing 'verify' evidence step")
                    continue
                exit_code = str(verify_block.get("exit_code", ""))
                if exit_code not in {"0", ""}:
                    errors.append(f"{verify_location}: VERIFY exit_code must be 0, got '{exit_code}'")
                for field_name in ("command", "output_summary"):
                    if field_name not in verify_block or str(verify_block.get(field_name, "")).strip() == "":
                        errors.append(f"{verify_location}: verify evidence missing '{field_name}'")

    return errors


def run_decisions(context: ValidationContext) -> CheckResult:
    path = context.spec_dir / "decisions.yaml"
    if not path.exists():
        return CheckResult("decisions.yaml", [], skipped=True, skip_message=f"decisions.yaml not found at {path}")
    errors = validate_decisions(path)
    return CheckResult("decisions.yaml", errors, summary=None if errors else "decisions.yaml: valid")


def validate_decisions(path: Path) -> list[str]:
    data, load_errors = parse_yaml_like_file(path)
    if load_errors:
        return load_errors
    if "decisions" not in data or not isinstance(data.get("decisions"), list):
        return [f"{path.name}: missing top-level 'decisions:' key"]
    errors: list[str] = []
    for index, entry in enumerate(data.get("decisions") or [], start=1):
        if not isinstance(entry, dict):
            errors.append(f"{path.name}#decisions[{index}]: entry is not a mapping")
            continue
        for required in DECISIONS_REQUIRED_FIELDS:
            if required not in entry:
                errors.append(f"{path.name}#decisions[{index}]: missing field '{required}'")
        # Absent/null status == resolved (back-compat). `accepted`/`superseded` are
        # legacy synonyms for resolved. Any other value is warn-staged (treated as
        # resolved, advisory to stderr) so the varied existing corpus vocabulary
        # (accepted, ...) does not hard-break on revalidation.
        raw_status = entry.get("status")
        status = "resolved" if raw_status is None else str(raw_status).strip().lower()
        if status == "unresolved":
            resolved_like = False
        elif status in {"resolved", "accepted", "superseded"}:
            resolved_like = True
        else:
            print(
                f"WARN  {path.name}#decisions[{index}]: unrecognized status '{status}' (resolved|unresolved) -> treated as resolved",
                file=sys.stderr,
            )
            resolved_like = True
        if resolved_like:
            for required in DECISIONS_RESOLVED_REQUIRED_FIELDS:
                if required not in entry:
                    errors.append(f"{path.name}#decisions[{index}]: missing field '{required}' (required when status: resolved)")
    return errors


def run_traceability(context: ValidationContext) -> CheckResult:
    path = context.spec_dir / "traceability.yaml"
    if not path.exists():
        return CheckResult("traceability.yaml", [], skipped=True, skip_message=f"traceability.yaml not found at {path}")
    errors = validate_traceability(path)
    return CheckResult("traceability.yaml", errors, summary=None if errors else "traceability.yaml: valid")


def validate_traceability(path: Path) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return [f"{path.name}: cannot read ({exc})"]
    if "tasks:" not in text and "requirements:" not in text:
        return [f"{path.name}: must have at least one of 'tasks:' or 'requirements:'"]
    return []
