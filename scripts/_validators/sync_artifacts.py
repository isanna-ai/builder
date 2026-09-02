from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from .canonical import validate_canonical_artifact
from .common import CheckResult, ValidationContext, mapping_list, parse_yaml_like_file

_SYNC_PHASE_REQUIRED_STATUSES = {"planned", "implementing", "implemented", "adversarially-reviewed", "verifying", "verified", "verified_with_tasks", "syncing", "synced"}
DELTA_CATEGORIES = ("capabilities", "behaviors", "journeys")
DELTA_CHANGES = {"create", "enrich", "rewire"}
_LOCKED_RESOLUTION_PATHS = (
    "amend the intent delta",
    "fix the SSOT",
    "file a narrowing task",
)


def _spec_status(spec_dir: Path) -> str:
    data, errors = parse_yaml_like_file(spec_dir / "spec.yaml")
    if errors:
        return ""
    return str(data.get("status", "")).strip()


def _owner_intent(spec_dir: Path):
    intent_root = spec_dir.parents[1] / "intents"
    for path in sorted(intent_root.glob("*/intent.yaml")):
        intent, errors = parse_yaml_like_file(path)
        if errors:
            continue
        specs = intent.get("specs") if isinstance(intent.get("specs"), list) else []
        if any(isinstance(ref, str) and ref.strip().split("/")[-1] == spec_dir.name for ref in specs):
            return intent
    return None


def _validate_delta_entries(data: dict[str, Any], source_name: str, owner_intent) -> list[str]:
    errors: list[str] = []
    declared = {
        category: {
            (str(item.get("target", "")).strip(), str(item.get("change", "")).strip())
            for item in ((owner_intent.get("ssot_delta") or {}).get(category) or [])
            if isinstance(item, dict)
        }
        for category in DELTA_CATEGORIES
    } if owner_intent is not None else None
    for category in DELTA_CATEGORIES:
        items = mapping_list(source_name, category, data.get(category), errors)
        seen: set[str] = set()
        for index, item in enumerate(items, start=1):
            target = str(item.get("target", "")).strip()
            change = str(item.get("change", "")).strip()
            if target in seen:
                errors.append(f"{source_name}: duplicate {category} target `{target}`")
            seen.add(target)
            if change not in DELTA_CHANGES:
                errors.append(f"{source_name}: invalid {category}[{index}].change `{change}`")
            if declared is not None and (target, change) not in declared[category]:
                errors.append(
                    f"{source_name}: {category}[{index}] `{target}`/{change} is not declared in the owning intent ssot_delta"
                )
    return errors


def validate_ssot_delta(data: dict[str, Any], source_name: str, context: ValidationContext) -> list[str]:
    owner_intent = _owner_intent(context.spec_dir)
    errors = _validate_delta_entries(data, source_name, owner_intent)
    if owner_intent is None:
        errors.append(f"{source_name}: owning intent object not found for spec `{context.spec_dir.name}`")
    return errors


def run_ssot_delta(context: ValidationContext) -> CheckResult:
    path = context.spec_dir / "ssot-delta.yaml"
    status = _spec_status(context.spec_dir)
    if not path.exists():
        if status in _SYNC_PHASE_REQUIRED_STATUSES:
            return CheckResult("ssot-delta.yaml", [f"ssot-delta.yaml: required for spec status `{status}`"])
        return CheckResult("ssot-delta.yaml", [], skipped=True, skip_message=f"ssot-delta.yaml not found at {path}")

    return validate_canonical_artifact(
        context,
        artifact_name="ssot-delta",
        source_file="ssot-delta.yaml",
        schema_file="ssot-delta.schema.yaml",
        extra_validation=lambda data, source_name: validate_ssot_delta(data, source_name, context),
    )


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sync_result_extra(data: dict[str, Any], source_name: str, context: ValidationContext) -> list[str]:
    from _sync.evidence import result_is_corroborated

    errors: list[str] = []
    result = str(data.get("result", "")).strip()
    paths = data.get("resolution_paths")
    if list(paths or []) != list(_LOCKED_RESOLUTION_PATHS):
        errors.append(f"{source_name}: resolution_paths must be exactly {list(_LOCKED_RESOLUTION_PATHS)}")
    if result == "divergence":
        if not data.get("undeclared_tuples"):
            errors.append(f"{source_name}: divergence requires undeclared_tuples")
    if result == "synced" and data.get("undeclared_tuples"):
        errors.append(f"{source_name}: synced result cannot carry undeclared_tuples")
    if result == "synced" and (data.get("hook_exit_code") != 0 or data.get("publish_state") != "published"):
        errors.append(f"{source_name}: synced requires hook_exit_code 0 and publish_state published")
    if result != "synced" and data.get("publish_state") != "staged-only":
        errors.append(f"{source_name}: non-synced result must use publish_state staged-only")
    if data.get("provenance") == "bootstrap-exception":
        if not data.get("owner_authorization") or not data.get("derived_baseline"):
            errors.append(f"{source_name}: bootstrap-exception requires owner_authorization and derived_baseline")
    elif any(key in data for key in ("owner_authorization", "derived_baseline")):
        errors.append(f"{source_name}: bootstrap disclosure fields require provenance bootstrap-exception")
    delta_path = context.spec_dir / "ssot-delta.yaml"
    if delta_path.exists():
        if str(data.get("declared_delta_digest", "")).strip() != _digest(delta_path):
            errors.append(f"{source_name}: declared_delta_digest does not match ssot-delta.yaml")
    if not result_is_corroborated(context.spec_dir, data):
        errors.append(f"{source_name}: result is not corroborated by matching host sync scope and verify gate evidence")
    return errors


def run_sync_result(context: ValidationContext) -> CheckResult:
    path = context.spec_dir / "sync-result.yaml"
    if not path.exists():
        spec, errors = parse_yaml_like_file(context.spec_dir / "spec.yaml")
        status = str(spec.get("status", "")).strip() if not errors else ""
        current_phase = str(spec.get("current_phase", "")).strip()
        if status == "synced" or (status == "verified" and current_phase == "sync"):
            return CheckResult("sync-result.yaml", [f"sync-result.yaml: required for status `{status}`/phase `{current_phase}`"])
        return CheckResult("sync-result.yaml", [], skipped=True, skip_message=f"sync-result.yaml not found at {path}")
    return validate_canonical_artifact(
        context,
        artifact_name="sync-result",
        source_file="sync-result.yaml",
        schema_file="sync-result.schema.yaml",
        extra_validation=lambda data, source_name: _sync_result_extra(data, source_name, context),
    )


def run_sync_scope(context: ValidationContext) -> CheckResult:
    path = context.spec_dir / "sync-scope.yaml"
    if not path.exists():
        return CheckResult("sync-scope.yaml", [], skipped=True, skip_message=f"sync-scope.yaml not found at {path}")
    from _sync.evidence import validate_scope_evidence

    return validate_canonical_artifact(
        context,
        artifact_name="sync-scope",
        source_file="sync-scope.yaml",
        schema_file="sync-scope.schema.yaml",
        extra_validation=lambda _data, _source: validate_scope_evidence(
            Path(str((_data or {}).get("worktree_root", ""))), context.spec_dir, verify_snapshot=False
        )[1],
    )


def run_implementation_baseline(context: ValidationContext) -> CheckResult:
    path = context.spec_dir / "implementation-baseline.yaml"
    if not path.exists():
        return CheckResult(
            "implementation-baseline.yaml", [], skipped=True,
            skip_message=f"implementation-baseline.yaml not found at {path}",
        )
    return validate_canonical_artifact(
        context,
        artifact_name="implementation-baseline",
        source_file="implementation-baseline.yaml",
        schema_file="implementation-baseline.schema.yaml",
    )


def run_sync_readmission_report(context: ValidationContext) -> CheckResult:
    path = context.spec_dir / "sync-readmission-report.yaml"
    if not path.exists():
        return CheckResult(
            "sync-readmission-report.yaml", [], skipped=True,
            skip_message=f"sync-readmission-report.yaml not found at {path}",
        )

    def extra(data: dict[str, Any], source_name: str) -> list[str]:
        from _dispatch_runtime import gate_evidence

        errors: list[str] = []
        if data.get("spec") != context.spec_dir.name:
            errors.append(f"{source_name}: spec does not match the owning spec directory")
        if data.get("status") == "blocked":
            forbidden = {
                "transaction_id", "provenance", "implementation_baseline", "verified_head",
                "verified_tree", "changed_paths", "verify_gate_bundle", "verify_gate_sha256",
                "source_evidence", "owner_authorization", "derived_baseline",
            }
            if data.get("result_code") == "ok" or not data.get("detail"):
                errors.append(f"{source_name}: blocked report requires a non-ok result_code and detail")
            if forbidden.intersection(data):
                errors.append(f"{source_name}: blocked report cannot contain success evidence")
            return errors
        required_ready = {
            "transaction_id", "provenance", "implementation_baseline", "verified_head",
            "verified_tree", "changed_paths", "verify_gate_bundle", "verify_gate_sha256", "source_evidence",
        }
        missing_ready = sorted(required_ready.difference(data))
        if data.get("status") != "ready" or data.get("result_code") != "ok" or missing_ready:
            errors.append(
                f"{source_name}: ready report requires result_code ok and fields {sorted(required_ready)}"
            )
            if missing_ready:
                errors.append(f"{source_name}: ready report missing {missing_ready}")
            return errors
        if "detail" in data:
            errors.append(f"{source_name}: ready report cannot contain failure detail")
        if data.get("provenance") == "bootstrap-exception":
            from _sync.readmit import BOOTSTRAP_AUTHORIZATION

            if data.get("owner_authorization") != BOOTSTRAP_AUTHORIZATION:
                errors.append(f"{source_name}: owner_authorization is not the exact locked authorization")
            if not isinstance((data.get("source_evidence") or {}).get("dirty_tree_summary"), dict):
                errors.append(f"{source_name}: bootstrap provenance requires the host dirty-tree summary")
        elif any(key in data for key in ("owner_authorization", "derived_baseline")):
            errors.append(f"{source_name}: disclosure fields require bootstrap-exception provenance")
        source = data.get("source_evidence") if isinstance(data.get("source_evidence"), dict) else {}
        for prefix, expected_gate in (("baseline", "red_baseline"), ("verified", "host_verify")):
            ref = str(source.get(f"{prefix}_bundle") or "")
            expected_sha = str(source.get(f"{prefix}_sha256") or "")
            rel = Path(ref)
            path = context.spec_dir / rel
            try:
                safe = (
                    len(rel.parts) == 2 and rel.parts[0] == "gate-evidence" and ".." not in rel.parts
                    and not path.is_symlink() and path.is_file()
                    and path.resolve().parent == (context.spec_dir / "gate-evidence").resolve()
                )
            except OSError:
                safe = False
            if not safe:
                errors.append(f"{source_name}: source {prefix} bundle is missing or unsafe")
                continue
            bundle, bundle_errors = parse_yaml_like_file(path)
            errors.extend(bundle_errors)
            if bundle_errors:
                continue
            if (
                bundle.get("schema") != gate_evidence.SCHEMA
                or bundle.get("spec_id") != context.spec_dir.name
                or bundle.get("gate") != expected_gate
                or bundle.get("verdict") != "pass"
                or bundle.get("bundle_sha256") != expected_sha
                or gate_evidence.bundle_sha(bundle) != expected_sha
            ):
                errors.append(f"{source_name}: source {prefix} bundle is not corroborated")
            if prefix == "verified" and bundle.get("phase") != "verify":
                errors.append(f"{source_name}: source verified bundle is not a verify-phase verdict")
        if data.get("provenance") != "bootstrap-exception" and "dirty_tree_summary" in source:
            errors.append(f"{source_name}: immutable provenance cannot claim the bootstrap dirty-tree exception")
        return errors

    return validate_canonical_artifact(
        context,
        artifact_name="sync-readmission-report",
        source_file="sync-readmission-report.yaml",
        schema_file="sync-readmission-report.schema.yaml",
        extra_validation=extra,
    )
