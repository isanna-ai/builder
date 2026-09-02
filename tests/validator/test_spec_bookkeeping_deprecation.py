"""`tasks_done` and its siblings are numbers nothing maintains.

No template writes them, no prompt emits them, no code derives or reads them. Before this
change the ONLY mention of `task_count`/`tasks_done`/`tasks_total`/`tasks_parallelizable`
anywhere in the repository was the allowlist in `_validators/legacy.py` that stopped strict
mode complaining about them. They are typed by hand, once, and never reconciled against
anything again.

That is not a tidiness problem. `beta-approve-funnel` sat at `status: planned`,
`tasks_done: 0 / 11` while every one of its headline deliverables was live in production --
the declared number said "nothing built" and a reader who trusted it would have rebuilt
shipped functionality. An unmaintained number is not neutral; it is read as fact.

Deprecated rather than hard-removed, because 33 spec.yaml files across five repositories
still carry these fields and 30 of them live in repositories this change does not own.
Staged exactly like BUILDER_TRACE_COVERAGE and BUILDER_VERIFY_LINT: `warn` by default
(advisory on stderr, never blocking), `enforce` promotes it to a hard error.
"""

from __future__ import annotations

import os

from scripts._validators.legacy import DEPRECATED_BOOKKEEPING_FIELDS, validate_spec_yaml_data


def _spec(**extra) -> dict:
    base = {
        "name": "demo",
        "created": "2026-07-29",
        "status": "planned",
        "current_phase": "plan",
        "next_action": "/isanna-5-implement demo",
    }
    base.update(extra)
    return base


def _with_env(value, fn):
    key = "BUILDER_SPEC_BOOKKEEPING"
    saved = os.environ.get(key)
    if value is None:
        os.environ.pop(key, None)
    else:
        os.environ[key] = value
    try:
        return fn()
    finally:
        if saved is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = saved


def test_the_deprecated_set_is_the_whole_unmaintained_family():
    assert set(DEPRECATED_BOOKKEEPING_FIELDS) == {
        "task_count", "tasks_done", "tasks_total", "tasks_parallelizable",
    }


def test_warn_is_the_default_and_never_blocks():
    # Default must stay advisory: 30 spec.yaml files in repositories this change does not
    # own carry these fields, and erroring by default would break their validation.
    errors = _with_env(None, lambda: validate_spec_yaml_data(_spec(tasks_done=3), "spec.yaml"))
    assert errors == []


def test_an_unrecognized_env_value_does_not_silently_enforce():
    errors = _with_env("enfroce", lambda: validate_spec_yaml_data(_spec(tasks_done=3), "spec.yaml"))
    assert errors == []


def test_enforce_promotes_the_deprecation_to_an_error():
    errors = _with_env("enforce", lambda: validate_spec_yaml_data(_spec(tasks_done=3), "spec.yaml"))
    assert len(errors) == 1 and "tasks_done" in errors[0]


def test_enforce_names_every_deprecated_field_present_in_one_finding():
    errors = _with_env("enforce", lambda: validate_spec_yaml_data(
        _spec(task_count=5, tasks_done=3, tasks_total=5), "spec.yaml"))
    assert len(errors) == 1
    for field in ("task_count", "tasks_done", "tasks_total"):
        assert field in errors[0]


def test_a_spec_without_the_fields_is_clean_under_enforce():
    assert _with_env("enforce", lambda: validate_spec_yaml_data(_spec(), "spec.yaml")) == []


def test_a_zero_value_still_counts_as_present():
    # `tasks_done: 0` is the exact shape that misled a reader on beta-approve-funnel. A
    # presence check written as a truthiness test would skip precisely that case.
    errors = _with_env("enforce", lambda: validate_spec_yaml_data(_spec(tasks_done=0), "spec.yaml"))
    assert len(errors) == 1 and "tasks_done" in errors[0]


def test_strict_mode_does_not_also_report_them_as_unknown_fields():
    # The fields stay in the strict-mode allowlist on purpose: the deprecation check owns
    # this message, and reporting the same field twice through two different channels trains
    # readers to skim both.
    errors = _with_env("enforce", lambda: validate_spec_yaml_data(
        _spec(tasks_done=3), "spec.yaml", strict=True))
    assert len(errors) == 1 and "unknown field" not in errors[0]
