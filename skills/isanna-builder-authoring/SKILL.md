---
name: isanna-builder-authoring
description: Author and hand-drive an isanna-builder spec through the /isanna-* workflow. Use when the user says "draft a spec", "write requirements", "design this spec", "review the plan", "make tasks.yaml", "implement this spec by hand", "verify this spec", "/isanna-1-specify", "/isanna-2-design", "/isanna-3-review", "/isanna-4-plan", "/isanna-5-implement", "/isanna-6-verify", "/isanna-archive", "/isanna-debug", "/isanna-ff", "/isanna-help", "/isanna-setup", "/isanna-sync", "reviews: 0", "reviews: 1", "reviews: 2", or asks how manual authoring differs from autonomous dispatch.
---

# isanna-builder authoring

Use this when a human or an interactive agent is driving a spec **by hand**. The canonical
artifacts live in `.builder/specs/<spec>/`; YAML is the source of truth. The host, not the
author, decides whether declared verify commands pass.

## The manual flow

| Command | Purpose | Canonical output / checkpoint |
|---|---|---|
| `/isanna-1-specify` | Turn intent into testable scope. | `system-model.yaml`, `intent.yaml`, `requirements.yaml`, `decisions.yaml` |
| `/isanna-2-design` | Design approved requirements. | `design.yaml` |
| `/isanna-3-review` | Run constitution, completeness, architecture, and verifiability review. | `review-log.yaml` |
| `/isanna-4-plan` | Make runner-ready, traceable work. | `tasks.yaml`, `traceability.yaml`, `runs/task-<id>.yaml` |
| `/isanna-5-implement` | Execute approved task packets with fresh evidence. | task evidence and traceability updates |
| `/isanna-6-verify` | Host-first final verification and bounded follow-up work. | verification verdict, `handoff.yaml` |
| `/isanna-archive` | Move a verified spec out of active specs. | archived full spec + `archive-report.yaml` |

`/isanna-debug` is the standalone root-cause-before-fix workflow. `/isanna-ff` fast-forwards all six
phases only for simple work; it does not waive phase rules. `/isanna-help` is command reference,
`/isanna-setup` records repository setup decisions, and `/isanna-sync` finds or repairs canonical-YAML /
rendered-companion drift.

> **Phase-naming note.** `/isanna-1-specify … /isanna-6-verify` write the LEGACY 7-phase `current_phase`
> (`1-specify`, `2-design`, …). The autonomous **dispatcher** drives a different, current 4-phase
> scheme — `spec → plan → implement → verify` (optionally review-augmented with `spec-review` /
> `adversarial-review`). So a dispatcher-authored `spec.yaml` shows `current_phase: implement`, not
> `5-implement`. Same lifecycle, two naming tracks; don't expect a hand-driven spec's phase names to
> match a dispatched one. (See the dispatcher skill for the 4-phase order.)

## Spec envelope

Start `spec.yaml` from `templates/spec.yaml`. Its normal shape is:

```yaml
name: <feature-name>
created: <ISO 8601 timestamp>
status: specifying
current_phase: 1-specify
next_action: "Run /isanna-1-specify"
# reviews: 0
```

The dispatch draft path can also record `summary`, `artifact_mode`, and an opt-in `plan_gate`.
Keep status and phase truthful; do not treat rendered Markdown as canonical in `ai_native` mode.

## Independent review count

Set `reviews: 0`, `1`, or `2` on the spec. It requests zero, one, or two independent reviewer
passes when the configured dispatcher has reviews enabled. Use the reviews skill for the choice
and protocol: [isanna-builder-reviews](../isanna-builder-reviews/SKILL.md). Do not claim an
independent review occurred merely because the field was set.

## Manual authoring vs autonomous dispatch

The slash commands are a human-driven workflow: a person/interactive agent chooses when to move
between phases and supplies decisions. The dispatcher is a separate autonomous execution path:
it consumes queue state and runs the pipeline. `isanna init` makes a repo drivable; it does not
start that path. See [isanna-builder-dispatcher](../isanna-builder-dispatcher/SKILL.md).

At every path boundary, retain the provenance rule: agent evidence is a claim; host verification
is a verdict only when the host ran the declared command and observed exit 0.

> **Runtime dir.** Artifacts live in `.builder/`. `runtime_dir()` always resolves that canonical path; legacy runtimes must be moved explicitly with `isanna migrate` while stopped.
