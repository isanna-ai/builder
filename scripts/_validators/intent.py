from __future__ import annotations

from pathlib import Path
from typing import Any

from .canonical import validate_canonical_artifact
from .common import ValidationContext, parse_yaml_like_file


def run(context: ValidationContext):
    return validate_canonical_artifact(
        context,
        artifact_name="intent",
        source_file="intent.yaml",
        schema_file="intent.schema.yaml",
        render=None,
        rendered_file=None,
        extra_validation=lambda data, source_name: validate_intent(data, source_name, context),
    )


def collect_intent_ids(spec_dir: Path) -> tuple[set[str], list[str]]:
    data, errors = parse_yaml_like_file(spec_dir / "intent.yaml")
    if errors:
        return set(), errors
    collected: set[str] = set()
    for block_name in ("constraints", "failure_conditions", "success_signals"):
        block = data.get(block_name)
        entries = block if isinstance(block, list) else []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            item_id = str(entry.get("id", "")).strip()
            if item_id:
                collected.add(item_id)
    return collected, []


def validate_intent(data: dict[str, Any], source_name: str, context: ValidationContext) -> list[str]:
    errors: list[str] = []
    seen_ids: set[str] = set()

    for block_name in ("constraints", "failure_conditions", "success_signals"):
        block = data.get(block_name) if isinstance(data.get(block_name), list) else []
        for index, entry in enumerate(block, start=1):
            if not isinstance(entry, dict):
                continue
            item_id = str(entry.get("id", "")).strip()
            if item_id in seen_ids:
                errors.append(f"{source_name}.{block_name}[{index}]: duplicate id `{item_id}`")
            seen_ids.add(item_id)

    system_model_data, system_model_errors = parse_yaml_like_file(context.spec_dir / "system-model.yaml")
    if system_model_errors:
        errors.append(
            f"{source_name}: system-model reference check skipped because system-model.yaml could not be read"
        )
        return errors

    known_refs = _collect_system_model_ids(system_model_data)
    references = data.get("references")
    reference_map = references if isinstance(references, dict) else {}
    system_model_refs = reference_map.get("system_model")
    ref_entries = system_model_refs if isinstance(system_model_refs, list) else []
    for ref in ref_entries:
        ref_id = str(ref).strip()
        if ref_id and ref_id not in known_refs:
            errors.append(f"{source_name}: unknown system-model reference `{ref_id}`")
    return errors


def _collect_system_model_ids(data: dict[str, Any]) -> set[str]:
    collected: set[str] = set()
    blocks: tuple[tuple[str, str], ...] = (
        ("what", "entities"),
        ("what", "capabilities"),
        ("who", "actors"),
        ("when", "events"),
        ("where", "boundaries"),
        ("why", "rules"),
        ("upstream", "sources"),
        ("downstream", "sinks"),
    )
    for parent_name, block_name in blocks:
        parent_value = data.get(parent_name)
        parent = parent_value if isinstance(parent_value, dict) else {}
        block = parent.get(block_name)
        entries = block if isinstance(block, list) else []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            item_id = str(entry.get("id", "")).strip()
            if item_id:
                collected.add(item_id)
    return collected
