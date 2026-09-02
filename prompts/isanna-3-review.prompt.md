---
agent: agent
description: "Phase 3 — review requirements and design."
load_set:
  tiny_local:
    - requirements.yaml
    - design.yaml
    - standards/builder-workflow.md
    - standards/builder-guardrails-review.md
  small_commercial:
    - requirements.yaml
    - design.yaml
    - skills/planning/SKILL.md
    - standards/builder-contract.md
    - standards/builder-workflow.md
    - standards/builder-guardrails-review.md
    - prompts/isanna-help.prompt.md
  flagship_commercial:
    - requirements.yaml
    - design.yaml
    - skills/planning/SKILL.md
    - standards/builder-contract.md
    - standards/builder-workflow.md
    - standards/builder-guardrails-review.md
    - prompts/isanna-help.prompt.md
---

# /isanna-3-review

Review `requirements.yaml`, `design.yaml`, and resolved decisions. Use `standards/builder-guardrails-review.md` for review rules. Write findings to `review-log.yaml` and render `review-log.md` when enabled.

## Review Passes

Run four ordered passes and record findings per pass in `review-log.yaml`. A `RED` or `HALT` finding in any pass blocks the GREEN verdict.

1. **Pass 1 — constitution compliance.** Every requirement and design item conforms to the active constitution. Treat a `block` principle as HALT and a `requires-human-decision` principle as unresolved until captured in `decisions.yaml`.
2. **Pass 2 — completeness.** Every intent elicited in Phase 1 is answered: no open question is left dangling in `decisions.yaml`, every item in the `/isanna-1-specify` elicitation checklist (failure behavior, invalid input, no-regression, out-of-scope, destructive/rollback, NFRs) is answered or explicitly marked not-applicable with a rationale, and every requirement carries at least one failure-path (unhappy-path) acceptance criterion, not only a happy-path one.
3. **Pass 3 — architecture.** Every requirement id is covered by at least one design item, and each design item states its risks and the contracts (schemas, interfaces, data flows) it affects.
4. **Pass 4 — adversarial verifiability audit.** For each requirement, name the exact declared verify command that would exit non-zero if the requirement were unmet. If no such command exists, or the command would already pass on today's tree (before implementation), file a `RED` finding. The host-verify gate reads exit code only, so a command that cannot fail proves nothing. Additionally, every structured acceptance criterion marked `priority: must` MUST be traceable to a `verify[].proves` reference on at least one task — file a `RED` finding for any uncovered `must` criterion (this is exactly what the enforce-mode acceptance-coverage check will block). A `must` criterion whose only honest oracle is `human_only` must instead be a `should`, or its human check captured as a task `human_gate`.

## Constitution Guardian

Run or account for the constitution guardian before writing the final verdict. Treat a constitution violation as **HALT** when the principle severity is `block`; treat `requires-human-decision` as unresolved until the decision is captured in `decisions.yaml`.

## Artifact Mode

Write canonical YAML first. In `ai_native` mode, treat `review-log.md` as a derived export and render it only when required by a human gate or explicit export.

Telemetry:

```sh
python3 .builder/scripts/record-workflow-event.py --phase 3-review --outcome-category completed --spec <spec-name> --next-command "/isanna-4-plan <spec-name>"
```
