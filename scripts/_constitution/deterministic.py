from __future__ import annotations

from pathlib import Path
from typing import Any

from _validators.common import string_list


def _path_matches(path: str, pattern: str) -> bool:
    normalized = path.replace("\\", "/")
    pattern = pattern.replace("\\", "/").strip()
    if not pattern:
        return False
    if pattern.endswith("/**"):
        return normalized.startswith(pattern[:-3].rstrip("/") + "/")
    if pattern.endswith("*"):
        return normalized.startswith(pattern[:-1])
    return normalized == pattern or normalized.startswith(pattern.rstrip("/") + "/")


def _read_optional(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def evaluate(
    constitution: dict[str, Any],
    project_root: Path,
    changed_files: list[str],
    spec_text: str,
    phase: str = "",
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    principles = constitution.get("principles") if isinstance(constitution.get("principles"), list) else []
    for principle in principles:
        if not isinstance(principle, dict):
            continue
        principle_id = str(principle.get("id", "")).strip()
        severity = str(principle.get("severity", "warn")).strip()
        applies_to = string_list(principle.get("applies_to"))
        if phase and applies_to and phase not in applies_to:
            results.append(
                {
                    "principle_id": principle_id,
                    "status": "skipped",
                    "severity": severity,
                    "summary": f"Skipped outside applies_to scope for phase `{phase}`.",
                    "evidence": [],
                    "remediation": "",
                }
            )
            continue
        evidence: list[str] = []
        status = "pass"

        for pattern in string_list(principle.get("forbidden_paths")):
            for changed in changed_files:
                if _path_matches(changed, pattern):
                    status = "block" if severity == "block" else "warn"
                    evidence.append(f"changed file `{changed}` matches forbidden path `{pattern}`")

        for pattern in string_list(principle.get("required_paths")):
            if not any(_path_matches(changed, pattern) for changed in changed_files):
                status = "block" if severity == "block" else "warn"
                evidence.append(f"no changed file matches required path `{pattern}`")

        searchable = spec_text
        for changed in changed_files:
            searchable += "\n" + _read_optional(project_root / changed)

        for term in string_list(principle.get("forbidden_terms")):
            if term and term.lower() in searchable.lower():
                status = "block" if severity == "block" else "warn"
                evidence.append(f"term `{term}` appears in spec or changed file content")

        for term in string_list(principle.get("required_evidence")):
            if term and term.lower() not in searchable.lower():
                status = "block" if severity == "block" else "warn"
                evidence.append(f"required evidence term `{term}` was not found")

        if severity == "decision" and status != "pass":
            status = "requires-human-decision"

        results.append(
            {
                "principle_id": principle_id,
                "status": status,
                "severity": severity,
                "summary": "No deterministic issue found." if status == "pass" else "; ".join(evidence),
                "evidence": evidence,
                "remediation": "Revise the spec/change or record an explicit human decision." if status != "pass" else "",
            }
        )
    return results
