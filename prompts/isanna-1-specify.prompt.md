---
agent: agent
description: "Phase 1 — specify requirements."
load_set:
  tiny_local:
    - requirements.yaml
    - design.yaml
    - system-model.yaml
    - intent.yaml
    - decisions.yaml
    - standards/builder-workflow.md
  small_commercial:
    - requirements.yaml
    - design.yaml
    - system-model.yaml
    - intent.yaml
    - decisions.yaml
    - skills/planning/SKILL.md
    - standards/builder-contract.md
    - standards/builder-workflow.md
    - prompts/isanna-help.prompt.md
  flagship_commercial:
    - requirements.yaml
    - design.yaml
    - system-model.yaml
    - intent.yaml
    - decisions.yaml
    - skills/planning/SKILL.md
    - standards/builder-contract.md
    - standards/builder-workflow.md
    - prompts/isanna-help.prompt.md
---

# /isanna-1-specify

Ground the user's feature intent into structured artifacts and concrete, testable
requirements. Do NOT approve until the elicitation checklist below is answered:
the host-verify gate builds exactly what you write, so a vague or ungrounded
requirement ships the wrong thing.

## Required Artifacts

Author (create or revise) all four before requesting approval:

- `system-model.yaml` — entities, capabilities, actors, and boundaries the feature
  touches (per contract). Intent references these ids.
- `intent.yaml` — `outcome`, `constraints`, `non_goals`, `failure_conditions`,
  `success_signals`, with `references.system_model` pointing at real model ids.
- `requirements.yaml` — EARS-style requirements with testable acceptance criteria.
- `decisions.yaml` — every open question as an entry with `status: unresolved`;
  resolved choices as `status: resolved` with `chosen` + `rationale`.

## Elicitation (mandatory before approval)

For EACH capability, ground the answer from context or ASK the user — never
assume. Record answers in the artifacts, not just chat:

- Failure behavior: what happens when it fails? → `intent.failure_conditions` +
  a requirement acceptance criterion.
- Invalid input: which inputs are invalid and what happens then? → a requirement.
- No-regression: what existing behavior must NOT change? → `intent.constraints`.
- Out of scope: what is explicitly excluded? → `intent.non_goals`.
- Destructive/migratory ops: is any operation destructive or a migration? If so,
  what is the rollback? → `intent.constraints` (+ a decision if unresolved).
- NFRs: which non-functional requirements bind (latency, security, compat) and
  which requirement carries each? → named requirements.

Anything you can neither ground nor get from the user becomes a
`status: unresolved` entry in `decisions.yaml` — do not invent an answer.

## Testable Acceptance Criteria

Every acceptance criterion MUST name an observable outcome checkable by a shell
command's exit code. Criteria containing `works`, `correctly`, `properly`, or
`appropriately` are flagged as a warning and must be revised before the
coverage/EARS checks are enforced — rewrite them as concrete, observable checks.

**Prefer the structured form.** Write each acceptance criterion as an object, not a
bare string:

```yaml
acceptance:
  - id: AC-R1-1                     # AC-R<requirement-number>-<n>, stable across passes
    statement: WHEN <trigger>, the system SHALL <observable behavior>.
    observable_at: <surface/log/endpoint/exit-code where the behavior is observable>
    oracle:
      type: automated_test          # automated_test | bounded_probe | human_only
      expected: <what a passing check asserts>
    priority: must                  # must | should
```

The legacy bare-string form (`- WHEN …, the system SHALL …`) is **still accepted**,
but the structured form is preferred and is REQUIRED before the acceptance-coverage
check is enforced: once `BUILDER_TRACE_COVERAGE=enforce`, every `priority: must`
criterion must be proven by at least one task's `verify[].proves` (wired in
`/isanna-4-plan`). Give each `must` criterion an `oracle` that a task can actually
discharge. A criterion whose oracle can only be `human_only` cannot be a `must` under
the automated gate — mark it `should` or record the human check as a task `human_gate`.

## Intent Wiring

Populate `intent.yaml` so `/isanna-4-plan` can build `traceability.yaml`
`intent_links` (each links an `intent_id` to the requirement ids that satisfy it).
Keep `intent.yaml` ids stable across passes.

## Artifact Mode

Write canonical YAML first. If the spec declares `artifact_mode: ai_native`, treat `requirements.md` as a derived export and render it only when a human gate or explicit export requests it.

Render `requirements.md` when rendered markdown is enabled. Preserve `spec.yaml` status and phase metadata.

Telemetry:

```sh
python3 .builder/scripts/record-workflow-event.py --phase 1-specify --outcome-category completed --spec <spec-name> --next-command "/isanna-2-design <spec-name>"
```
