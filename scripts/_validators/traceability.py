from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Any

from .canonical import validate_canonical_artifact
from .common import ValidationContext, parse_yaml_like_file, string_list
from .intent import collect_intent_ids


def _coverage_enforced() -> bool:
    """Coverage checks (every requirement->design->task reached; no empty link
    arrays; readable sources) are staged: `warn` (default) records to stderr and
    never blocks; `enforce` promotes them to hard errors. Flip to `enforce` only
    after existing specs are backfilled with the new design ids + full trace
    links, mirroring the BUILDER_HOST_VERIFY warn->enforce rollout."""
    return (os.environ.get("BUILDER_TRACE_COVERAGE", "warn") or "warn").strip().lower() == "enforce"


def run(context: ValidationContext):
    return validate_canonical_artifact(
        context,
        artifact_name="traceability",
        source_file="traceability.yaml",
        schema_file="traceability.schema.yaml",
        render=None,
        rendered_file=None,
        extra_validation=lambda data, source_name: validate_traceability(data, source_name, context.spec_dir),
    )


def _collect_requirement_ids(spec_dir: Path) -> tuple[set[str], list[str]]:
    data, errors = parse_yaml_like_file(spec_dir / "requirements.yaml")
    if errors:
        return set(), errors
    requirements = data.get("requirements") if isinstance(data.get("requirements"), list) else []
    ids = {str(item.get("id", "")).strip() for item in requirements if isinstance(item, dict) and str(item.get("id", "")).strip()}
    return ids, []


def _collect_must_acceptance_ids(spec_dir: Path) -> tuple[set[str], list[str]]:
    # The ids of STRUCTURED (object-form) acceptance criteria marked `priority: must`.
    # Returns (ids, errors) mirroring the other `_collect_*` helpers. An EMPTY id-set on
    # a readable requirements.yaml means the spec uses only bare-string acceptance (or no
    # must-priority structured criteria) — the acceptance-coverage check is then skipped
    # entirely, so legacy string-only specs are unaffected.
    data, errors = parse_yaml_like_file(spec_dir / "requirements.yaml")
    if errors:
        return set(), errors
    requirements = data.get("requirements") if isinstance(data.get("requirements"), list) else []
    collected: set[str] = set()
    for requirement in requirements:
        if not isinstance(requirement, dict):
            continue
        acceptance = requirement.get("acceptance") if isinstance(requirement.get("acceptance"), list) else []
        for item in acceptance:
            if not isinstance(item, dict):
                continue  # bare-string acceptance exposes no coverable id
            if str(item.get("priority", "")).strip() != "must":
                continue
            ac_id = str(item.get("id", "")).strip()
            if ac_id:
                collected.add(ac_id)
    return collected, []


def _collect_proves_refs(spec_dir: Path) -> tuple[set[str], list[str]]:
    # The acceptance-criterion ids referenced by tasks' verify[].proves (and any
    # task-level `proves`). Returns (ids, errors) so an UNREADABLE tasks.yaml is
    # distinguished from a readable one that simply carries no proves references.
    data, errors = parse_yaml_like_file(spec_dir / "tasks.yaml")
    if errors:
        return set(), errors
    tasks = data.get("tasks") if isinstance(data.get("tasks"), list) else []
    collected: set[str] = set()
    for task in tasks:
        if not isinstance(task, dict):
            continue
        collected.update(string_list(task.get("proves")))
        verify_items = task.get("verify") if isinstance(task.get("verify"), list) else []
        for verify_item in verify_items:
            if isinstance(verify_item, dict):
                collected.update(string_list(verify_item.get("proves")))
    return collected, []


def _collect_core_changes(spec_dir: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    data, errors = parse_yaml_like_file(spec_dir / "design.yaml")
    if errors:
        return [], [], errors
    core_changes = data.get("core_changes") if isinstance(data.get("core_changes"), list) else []
    resp_alloc = data.get("responsibility_allocation") if isinstance(data.get("responsibility_allocation"), list) else []
    return (
        [item for item in core_changes if isinstance(item, dict)],
        [item for item in resp_alloc if isinstance(item, dict)],
        [],
    )


def _collect_design_ids(items: list[dict[str, Any]]) -> set[str]:
    # Design ids are the `id` field on core_changes AND responsibility_allocation
    # (both carry ids per the design template/prompt). Titles are no longer a
    # fallback: an unset id must surface as a coverage gap, not silently become a
    # phantom design id derived from prose.
    collected: set[str] = set()
    for item in items:
        design_id = str(item.get("id") or "").strip()
        if design_id:
            collected.add(design_id)
    return collected


def _collect_task_ids(spec_dir: Path) -> tuple[set[str], list[str]]:
    # Returns (ids, errors) so callers can distinguish a READABLE-but-empty source
    # (errors == []) from an UNREADABLE one (parse errors) — the empty-set case must
    # still flag unknown references under enforce.
    data, errors = parse_yaml_like_file(spec_dir / "tasks.yaml")
    if errors:
        return set(), errors
    tasks = data.get("tasks") if isinstance(data.get("tasks"), list) else []
    return (
        {str(item.get("id", "")).strip() for item in tasks if isinstance(item, dict) and str(item.get("id", "")).strip()},
        [],
    )


def _collect_evidence_ids(spec_dir: Path) -> tuple[set[str], list[str]]:
    # Returns (ids, errors). A missing evidence dir is a readable-but-empty source
    # (evidence is produced later in the lifecycle), NOT a parse failure; a file that
    # fails to parse is surfaced so the source counts as unreadable.
    evidence_dir = spec_dir / "evidence"
    if not evidence_dir.is_dir():
        return set(), []
    collected: set[str] = set()
    parse_errors: list[str] = []
    for evidence_path in sorted(evidence_dir.glob("task-*.yaml")):
        data, errors = parse_yaml_like_file(evidence_path)
        if errors:
            parse_errors.extend(errors)
            continue
        entries = data.get("entries") if isinstance(data.get("entries"), list) else []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            evidence_id = str(entry.get("id", "")).strip()
            if evidence_id:
                collected.add(evidence_id)
    return collected, parse_errors


def validate_traceability(data: dict[str, Any], source_name: str, spec_dir: Path) -> list[str]:
    errors: list[str] = []
    coverage_warnings: list[str] = []
    enforced = _coverage_enforced()

    # Collect the design ids FIRST, before any coverage_gap() call, so the
    # new-methodology grandfathering marker is known throughout. Design ids span BOTH
    # core_changes and responsibility_allocation (the template assigns D-ids to both and
    # links reference either), so a link to a valid responsibility_allocation id must not
    # hard-fail as "unknown design".
    core_changes, resp_alloc, design_parse_errors = _collect_core_changes(spec_dir)
    design_ids = _collect_design_ids(core_changes) | _collect_design_ids(resp_alloc)
    # New-methodology marker (grandfathering): a spec has adopted the canonical D-id
    # convention when at least one collected design id matches ^D[0-9]+$. Legacy specs
    # use free-form design ids (e.g. `billing-role-migration`) or none — they are NOT
    # new-methodology and their coverage gaps stay advisory (stderr) even under enforce.
    has_canonical_design = any(re.match(r"^D[0-9]+$", did) for did in design_ids)

    def coverage_gap(message: str, *, grandfather: bool = True) -> None:
        # Staged AND grandfathered: a trace-coverage gap is a HARD error only under enforce
        # AND when the spec adopted the new methodology (a canonical D-id). Legacy specs stay
        # advisory even under enforce. Referential unknown-ref against a non-empty id-set is
        # NOT routed here — it stays hard for all specs. `grandfather=False` is for gaps that
        # SELF-grandfather (the must-acceptance check only fires when a spec declares
        # structured `priority: must` criteria — a new-methodology choice — so it stays hard
        # under enforce regardless of D-ids).
        hard = enforced and (has_canonical_design or not grandfather)
        (errors if hard else coverage_warnings).append(message)

    def check_ref(known_ids: set[str], readable: bool, ref_id: str, message: str) -> None:
        """Referential integrity for one link reference, with the enforce airtightness
        gap closed. When the source id-set is NON-EMPTY, an absent reference stays the
        EXISTING hard error (severity unchanged). When the source is READABLE but its
        id-set is empty, an absent reference was previously skipped silently even under
        enforce; route that NEW case through coverage_gap (hard under enforce, advisory
        under warn — so default/warn behavior is unchanged). An UNREADABLE source
        (empty id-set, readable=False) keeps the prior silent skip; where an explicit
        `cannot be verified` coverage_gap exists it already reports that separately."""
        if ref_id in known_ids:
            return
        if known_ids:
            errors.append(message)
        elif readable and ref_id:
            coverage_gap(message)

    intent_ids, intent_id_errors = collect_intent_ids(spec_dir)
    if intent_id_errors and isinstance(data.get("intent_links"), list) and data.get("intent_links"):
        errors.append(f"{source_name}: intent link validation skipped because intent.yaml could not be read")
    requirement_ids, requirement_id_errors = _collect_requirement_ids(spec_dir)
    if requirement_id_errors:
        # A traceability artifact cannot prove coverage against requirements it
        # cannot read. Staged as a coverage gap (warn until enforce).
        coverage_gap(
            f"{source_name}: requirement coverage cannot be verified because requirements.yaml could not be read ({'; '.join(requirement_id_errors)})"
        )
    if design_parse_errors:
        coverage_gap(
            f"{source_name}: design coverage cannot be verified because design.yaml could not be read ({'; '.join(design_parse_errors)})"
        )
    task_ids, task_id_errors = _collect_task_ids(spec_dir)
    evidence_ids, evidence_id_errors = _collect_evidence_ids(spec_dir)

    # A source is READABLE when its collector returned no parse errors. A readable
    # source with an EMPTY id-set can still expose unknown references (the enforce
    # airtightness fix), whereas an unreadable source is left to its own
    # `cannot be verified` gap and its referential checks stay skipped.
    requirements_readable = not requirement_id_errors
    design_readable = not design_parse_errors
    intent_readable = not intent_id_errors
    tasks_readable = not task_id_errors
    evidence_readable = not evidence_id_errors

    # Referential integrity for the design->requirements back-references: every
    # requirement id listed on a core_changes entry must exist in requirements.yaml.
    for index, change in enumerate(core_changes, start=1):
        change_id = str(change.get("id") or "").strip() or "?"
        for req_ref in string_list(change.get("requirements")):
            check_ref(
                requirement_ids,
                requirements_readable,
                req_ref,
                f"{source_name}: design.yaml core_changes[{index}] ({change_id}) references unknown requirement `{req_ref}`",
            )

    intent_links_raw = data.get("intent_links")
    intent_links = intent_links_raw if isinstance(intent_links_raw, list) else []
    for index, entry in enumerate(intent_links, start=1):
        if not isinstance(entry, dict):
            continue
        intent_id = str(entry.get("intent_id", "")).strip()
        check_ref(
            intent_ids,
            intent_readable,
            intent_id,
            f"{source_name}: intent_links[{index}].intent_id references unknown intent `{intent_id}`",
        )
        for requirement_id in string_list(entry.get("requirement_ids")):
            check_ref(
                requirement_ids,
                requirements_readable,
                requirement_id,
                f"{source_name}: intent_links[{index}].requirement_ids references unknown requirement `{requirement_id}`",
            )

    requirement_links = data.get("requirement_links") if isinstance(data.get("requirement_links"), list) else []
    linked_requirement_ids: set[str] = set()
    for index, entry in enumerate(requirement_links, start=1):
        if not isinstance(entry, dict):
            continue
        requirement_id = str(entry.get("requirement_id", "")).strip()
        check_ref(
            requirement_ids,
            requirements_readable,
            requirement_id,
            f"{source_name}: requirement_links[{index}].requirement_id references unknown requirement `{requirement_id}`",
        )
        if requirement_id:
            linked_requirement_ids.add(requirement_id)
        design_id_refs = string_list(entry.get("design_ids"))
        if not design_id_refs:
            coverage_gap(
                f"{source_name}: requirement_links[{index}].design_ids is empty; requirement `{requirement_id}` is not covered by any design"
            )
        for design_id in design_id_refs:
            check_ref(
                design_ids,
                design_readable,
                design_id,
                f"{source_name}: requirement_links[{index}].design_ids references unknown design `{design_id}`",
            )

    # Coverage: every requirement must reach at least one design via requirement_links.
    for requirement_id in sorted(requirement_ids):
        if requirement_id not in linked_requirement_ids:
            coverage_gap(
                f"{source_name}: requirement `{requirement_id}` has no requirement_links entry; it is not traced to any design"
            )

    design_links = data.get("design_links") if isinstance(data.get("design_links"), list) else []
    linked_design_ids: set[str] = set()
    for index, entry in enumerate(design_links, start=1):
        if not isinstance(entry, dict):
            continue
        design_id = str(entry.get("design_id", "")).strip()
        check_ref(
            design_ids,
            design_readable,
            design_id,
            f"{source_name}: design_links[{index}].design_id references unknown design `{design_id}`",
        )
        if design_id:
            linked_design_ids.add(design_id)
        task_id_refs = string_list(entry.get("task_ids"))
        if not task_id_refs:
            coverage_gap(
                f"{source_name}: design_links[{index}].task_ids is empty; design `{design_id}` is not decomposed into any task"
            )
        for task_id in task_id_refs:
            check_ref(
                task_ids,
                tasks_readable,
                task_id,
                f"{source_name}: design_links[{index}].task_ids references unknown task `{task_id}`",
            )

    # Coverage: every design item must reach at least one task via design_links.
    for design_id in sorted(design_ids):
        if design_id not in linked_design_ids:
            coverage_gap(
                f"{source_name}: design `{design_id}` has no design_links entry; it is not decomposed into any task"
            )

    task_links = data.get("task_links") if isinstance(data.get("task_links"), list) else []
    for index, entry in enumerate(task_links, start=1):
        if not isinstance(entry, dict):
            continue
        task_id = str(entry.get("task_id", "")).strip()
        check_ref(
            task_ids,
            tasks_readable,
            task_id,
            f"{source_name}: task_links[{index}].task_id references unknown task `{task_id}`",
        )
        evidence_id_refs = string_list(entry.get("evidence_ids"))
        # Coverage: every linked task must carry at least one evidence id. Staged as a
        # coverage_gap so it is enforce-only and leaves default/warn behavior unchanged.
        if not evidence_id_refs:
            coverage_gap(
                f"{source_name}: task_links[{index}].evidence_ids is empty; task `{task_id}` has no evidence"
            )
        for evidence_id in evidence_id_refs:
            check_ref(
                evidence_ids,
                evidence_readable,
                evidence_id,
                f"{source_name}: task_links[{index}].evidence_ids references unknown evidence `{evidence_id}`",
            )

    # Acceptance coverage: every STRUCTURED acceptance criterion marked `priority: must`
    # must be proven by at least one task's verify[].proves. Enforce-only via
    # coverage_gap (advisory under warn, hard under enforce). A spec that uses only
    # bare-string acceptance exposes NO must-priority ids, so this is skipped entirely
    # for it — legacy string-only specs (the entire existing corpus) are unaffected.
    must_acceptance_ids, must_acceptance_errors = _collect_must_acceptance_ids(spec_dir)
    if must_acceptance_errors and not requirement_id_errors:
        # Only surface an acceptance-specific read failure the requirement collector did
        # not already report (avoids a duplicate `cannot be verified` gap).
        coverage_gap(
            f"{source_name}: acceptance coverage cannot be verified because requirements.yaml could not be read ({'; '.join(must_acceptance_errors)})"
        )
    elif must_acceptance_ids:
        proves_refs, proves_ref_errors = _collect_proves_refs(spec_dir)
        if proves_ref_errors:
            coverage_gap(
                f"{source_name}: acceptance coverage cannot be verified because tasks.yaml could not be read ({'; '.join(proves_ref_errors)})",
                grandfather=False,
            )
        else:
            for ac_id in sorted(must_acceptance_ids):
                if ac_id not in proves_refs:
                    coverage_gap(
                        f"{source_name}: acceptance criterion `{ac_id}` (priority: must) is not proven by any task verify[].proves",
                        grandfather=False,
                    )

    for message in coverage_warnings:
        print(f"WARN  {message} (BUILDER_TRACE_COVERAGE=warn)", file=sys.stderr)
    return errors