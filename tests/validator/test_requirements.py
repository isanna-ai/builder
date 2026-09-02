from __future__ import annotations

from scripts._validators.requirements import (
    lint_acceptance_ears,
    lint_acceptance_structure,
    validate_requirements,
)


def _requirement(acceptance: list, *, req_id: str = "R1") -> dict:
    return {
        "id": req_id,
        "title": "Demo",
        "user_story": "As a maintainer, I want X so that Y.",
        "acceptance": acceptance,
    }


def _structured_ac(
    *,
    ac_id: str = "AC-R1-1",
    oracle_type: str = "automated_test",
    priority: str = "must",
    statement: str = "WHEN the job completes, the system SHALL mark it done.",
) -> dict:
    return {
        "id": ac_id,
        "statement": statement,
        "observable_at": "exit code of the check",
        "oracle": {"type": oracle_type, "expected": "the check passes"},
        "priority": priority,
    }


def test_validate_requirements_flags_duplicate_ids() -> None:
    data = {"requirements": [_requirement(["WHEN x, the system SHALL y."]), _requirement(["WHEN a, the system SHALL b."])]}
    errors = "\n".join(validate_requirements(data, "requirements.yaml"))
    assert "duplicate id `R1`" in errors


def test_validate_requirements_flags_missing_acceptance() -> None:
    data = {"requirements": [{"id": "R1", "title": "Demo", "user_story": "story", "acceptance": []}]}
    errors = "\n".join(validate_requirements(data, "requirements.yaml"))
    assert "missing acceptance criteria" in errors


def test_ears_lint_passes_clean_acceptance() -> None:
    data = {"requirements": [_requirement(["WHEN the job completes, the system SHALL mark it done."])]}
    assert lint_acceptance_ears(data, "requirements.yaml") == []


def test_ears_lint_passes_system_shall_opener() -> None:
    data = {"requirements": [_requirement(["The system SHALL persist the record before returning."])]}
    assert lint_acceptance_ears(data, "requirements.yaml") == []


def test_ears_lint_warns_on_non_ears_opener() -> None:
    data = {"requirements": [_requirement(["The feature saves the file."])]}
    warnings = "\n".join(lint_acceptance_ears(data, "requirements.yaml"))
    assert "SHOULD open with an EARS keyword" in warnings


def test_ears_lint_warns_on_vague_wording() -> None:
    data = {"requirements": [_requirement(["WHEN saving, the system SHALL work correctly."])]}
    warnings = "\n".join(lint_acceptance_ears(data, "requirements.yaml"))
    assert "vague wording" in warnings
    assert "correctly" in warnings or "work" in warnings


def test_ears_lint_is_non_blocking() -> None:
    # Vague, non-EARS acceptance still yields warnings but never hard errors.
    data = {"requirements": [_requirement(["It works properly."])]}
    assert validate_requirements(data, "requirements.yaml") == []
    assert lint_acceptance_ears(data, "requirements.yaml") != []


# --- structured (object-form) acceptance -------------------------------------------


def test_structured_acceptance_validates_without_hard_error() -> None:
    # The structured object form is accepted with no hard error (optional structure).
    data = {"requirements": [_requirement([_structured_ac()])]}
    assert validate_requirements(data, "requirements.yaml") == []


def test_string_acceptance_still_validates_non_breaking() -> None:
    # Legacy bare-string acceptance keeps validating -> the ~449 existing specs stay green.
    data = {"requirements": [_requirement(["WHEN x happens, the system SHALL record it."])]}
    assert validate_requirements(data, "requirements.yaml") == []


def test_mixed_string_and_structured_acceptance_validates() -> None:
    data = {"requirements": [_requirement(["WHEN x, the system SHALL y.", _structured_ac()])]}
    assert validate_requirements(data, "requirements.yaml") == []


def test_structure_lint_passes_clean_structured_acceptance() -> None:
    data = {"requirements": [_requirement([_structured_ac()])]}
    assert lint_acceptance_structure(data, "requirements.yaml") == []


def test_structure_lint_ignores_string_acceptance() -> None:
    # Bare strings expose no structured shape -> the structure lint stays silent for them.
    data = {"requirements": [_requirement(["WHEN x, the system SHALL y."])]}
    assert lint_acceptance_structure(data, "requirements.yaml") == []


def test_structure_lint_warns_on_bad_ac_id() -> None:
    data = {"requirements": [_requirement([_structured_ac(ac_id="R1-1")])]}
    warnings = "\n".join(lint_acceptance_structure(data, "requirements.yaml"))
    assert "should match AC-R<req>-<n>" in warnings


def test_structure_lint_warns_on_bad_oracle_type() -> None:
    data = {"requirements": [_requirement([_structured_ac(oracle_type="vibes")])]}
    warnings = "\n".join(lint_acceptance_structure(data, "requirements.yaml"))
    assert "oracle.type should be one of" in warnings


def test_structure_lint_warns_on_bad_priority() -> None:
    data = {"requirements": [_requirement([_structured_ac(priority="critical")])]}
    warnings = "\n".join(lint_acceptance_structure(data, "requirements.yaml"))
    assert "priority should be one of" in warnings


def test_structure_lint_is_non_blocking() -> None:
    # A malformed structured item warns but never becomes a hard error.
    data = {"requirements": [_requirement([_structured_ac(ac_id="nope", oracle_type="vibes")])]}
    assert validate_requirements(data, "requirements.yaml") == []
    assert lint_acceptance_structure(data, "requirements.yaml") != []
