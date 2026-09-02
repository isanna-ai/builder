---
agent: agent
description: "Phase 2 — design the approved requirements."
load_set:
  tiny_local:
    - requirements.yaml
    - design.yaml
    - system-model.yaml
    - intent.yaml
    - standards/builder-workflow.md
  small_commercial:
    - requirements.yaml
    - design.yaml
    - system-model.yaml
    - intent.yaml
    - skills/planning/SKILL.md
    - standards/builder-contract.md
    - standards/builder-workflow.md
    - prompts/isanna-help.prompt.md
  flagship_commercial:
    - requirements.yaml
    - design.yaml
    - system-model.yaml
    - intent.yaml
    - skills/planning/SKILL.md
    - standards/builder-contract.md
    - standards/builder-workflow.md
    - prompts/isanna-help.prompt.md
---

# /isanna-2-design

Create or revise `design.yaml` from approved requirements. Resolve architecture decisions when possible and record remaining choices in `decisions.yaml`.

## Constitution Guardian

Read the applicable project constitution before finalizing design. Run or account for `validate-constitution.py <spec-name> --root <root> --no-model` when available, and ensure `design.yaml` explicitly preserves blocking principles. A blocking guardian finding prevents phase completion; an intentional principle bend requires a recorded human decision.

## Artifact Mode

Write canonical YAML first. In `ai_native` mode, treat `design.md` as a derived export and render it only when required by a human gate or explicit export.

Render `design.md` when rendered markdown is enabled. Keep design surfaces traceable to requirement ids.

## Requirement Coverage

Give every `responsibility_allocation` and `core_changes` entry a stable `id` (`D<number>`). Every requirement id from `requirements.yaml` MUST appear in at least one `core_changes` entry's `requirements` list — a requirement with no design owner is a coverage gap and the traceability validator will reject it. For each new surface, state the error-handling behavior (what happens on invalid input, missing dependency, or failure) directly in the `change`/`summary` text; do not leave failure paths implicit. Record notable `risks` (with mitigations) and the `affected_contracts` (APIs, schemas, migrations touched) so downstream planning and verification can see the blast radius.

## Destructive-Change Rollback Contract

If the intent's `change_risk` is `destructive` or `irreversible`, the design MUST include a `migration_strategy` with a forward command (`forward_command`) and a rollback command (`rollback_command`), plus `rollback_verification` commands proving recovery. The rollback MUST be represented as zero-on-success commands runnable in a disposable/test environment (never against real production data) — a destructive change without a tested, environment-safe recovery path is not a shippable design. Also record the `preservation_invariants` the change must not violate and the `rollback_window` during which recovery stays possible.

Telemetry:

```sh
python3 .builder/scripts/record-workflow-event.py --phase 2-design --outcome-category completed --spec <spec-name> --next-command "/isanna-3-review <spec-name>"
```
