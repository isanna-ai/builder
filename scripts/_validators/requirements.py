from __future__ import annotations

import re
import sys
from typing import Any

from .canonical import validate_canonical_artifact
from .common import ValidationContext
from .renderers import render_requirements


# EARS acceptance criteria open with one of these keywords. This is a SHOULD
# (warn-only) signal, not a blocking rule, so existing specs do not hard-break.
EARS_OPENER = re.compile(r"^\s*(WHEN|IF|WHILE|WHERE|THE SYSTEM SHALL)\b", re.IGNORECASE)

# Vague adverbs/verbs that describe intent instead of an observable behavior.
VAGUE_WORDS = ("works", "correctly", "properly", "appropriately")
VAGUE_PATTERN = re.compile(r"\b(" + "|".join(VAGUE_WORDS) + r")\b", re.IGNORECASE)

# Structured (object-form) acceptance criteria shape. These are OPTIONAL: an
# acceptance item may still be a bare string (legacy). When an author opts into the
# object form, these govern its id / oracle.type / priority — but only as WARN-level
# advisories (never a hard error), so adopting the richer form incrementally never
# breaks the gate for the ~449 existing string-form specs.
AC_ID_PATTERN = re.compile(r"^AC-R[0-9]+-[0-9]+$")
ORACLE_TYPES = ("automated_test", "bounded_probe", "human_only")
ACCEPTANCE_PRIORITIES = ("must", "should")


def run(context: ValidationContext):
    return validate_canonical_artifact(
        context,
        artifact_name="requirements",
        source_file="requirements.yaml",
        schema_file="requirements.schema.yaml",
        render=render_requirements,
        rendered_file="requirements.md",
        extra_validation=validate_requirements,
    )


def validate_requirements(data: dict[str, Any], source_name: str) -> list[str]:
    errors: list[str] = []
    known_ids: set[str] = set()
    for index, requirement in enumerate(data.get("requirements") or [], start=1):
        if not isinstance(requirement, dict):
            continue
        requirement_id = str(requirement.get("id", "")).strip()
        if requirement_id in known_ids:
            errors.append(f"{source_name}.requirements[{index}]: duplicate id `{requirement_id}`")
        known_ids.add(requirement_id)
        acceptance = requirement.get("acceptance") if isinstance(requirement.get("acceptance"), list) else []
        if not acceptance:
            errors.append(f"{source_name}.requirements[{index}]: missing acceptance criteria")

    # Non-blocking EARS lint. These are advisories, not errors: surface them on
    # stderr so authors see them without failing the gate on legacy phrasing.
    for warning in lint_acceptance_ears(data, source_name):
        print(f"WARN   {warning}", file=sys.stderr)

    # Non-blocking structured-acceptance lint. Only fires on the OBJECT form; bare
    # strings are left to the EARS lint above. Advisory (stderr) only — never a
    # hard error — so string-form acceptance and existing specs are non-breaking.
    for warning in lint_acceptance_structure(data, source_name):
        print(f"WARN   {warning}", file=sys.stderr)

    return errors


def lint_acceptance_ears(data: dict[str, Any], source_name: str) -> list[str]:
    """Warn-level shape check for acceptance criteria.

    Acceptance strings SHOULD open with an EARS keyword and MUST NOT lean on
    vague wording (`works`, `correctly`, `properly`, `appropriately`) that hides
    the observable behavior the criterion is supposed to pin down. Returns the
    list of advisory messages; the caller decides how to surface them.
    """
    warnings: list[str] = []
    for index, requirement in enumerate(data.get("requirements") or [], start=1):
        if not isinstance(requirement, dict):
            continue
        acceptance = requirement.get("acceptance") if isinstance(requirement.get("acceptance"), list) else []
        for position, item in enumerate(acceptance, start=1):
            if isinstance(item, dict):
                # Structured (object-form) acceptance is shape-checked by
                # lint_acceptance_structure; skip it here. Lint its `statement`
                # text with the same EARS/vagueness rules the string form gets.
                text = str(item.get("statement", "")).strip()
            else:
                text = str(item).strip()
            if not text:
                continue
            location = f"{source_name}.requirements[{index}].acceptance[{position}]"
            if not EARS_OPENER.match(text):
                warnings.append(
                    f"{location}: SHOULD open with an EARS keyword (WHEN/IF/WHILE/WHERE/The system SHALL): {text!r}"
                )
            vague = VAGUE_PATTERN.search(text)
            if vague:
                warnings.append(
                    f"{location}: avoid vague wording `{vague.group(1)}`; state the observable behavior instead: {text!r}"
                )
    return warnings


def lint_acceptance_structure(data: dict[str, Any], source_name: str) -> list[str]:
    """Warn-level shape check for STRUCTURED (object-form) acceptance criteria.

    An acceptance item may be a bare string (legacy, handled by the EARS lint) OR a
    structured object. When it is an object, its `id` SHOULD match `AC-R<req>-<n>`,
    its `oracle.type` (if present) SHOULD be one of the known oracle kinds, and its
    `priority` (if present) SHOULD be `must`/`should`. These are advisories only:
    the caller surfaces them on stderr and never fails the gate — so a spec with
    all-string acceptance produces zero output here and stays non-breaking.
    """
    warnings: list[str] = []
    for index, requirement in enumerate(data.get("requirements") or [], start=1):
        if not isinstance(requirement, dict):
            continue
        acceptance = requirement.get("acceptance") if isinstance(requirement.get("acceptance"), list) else []
        for position, item in enumerate(acceptance, start=1):
            if not isinstance(item, dict):
                continue  # bare string -> handled by lint_acceptance_ears
            location = f"{source_name}.requirements[{index}].acceptance[{position}]"
            ac_id = str(item.get("id", "")).strip()
            if not AC_ID_PATTERN.match(ac_id):
                warnings.append(
                    f"{location}: structured acceptance `id` should match AC-R<req>-<n> (got {ac_id!r})"
                )
            oracle = item.get("oracle")
            if isinstance(oracle, dict):
                oracle_type = str(oracle.get("type", "")).strip()
                if oracle_type and oracle_type not in ORACLE_TYPES:
                    warnings.append(
                        f"{location}: oracle.type should be one of {list(ORACLE_TYPES)} (got {oracle_type!r})"
                    )
            priority = str(item.get("priority", "")).strip()
            if priority and priority not in ACCEPTANCE_PRIORITIES:
                warnings.append(
                    f"{location}: priority should be one of {list(ACCEPTANCE_PRIORITIES)} (got {priority!r})"
                )
    return warnings
