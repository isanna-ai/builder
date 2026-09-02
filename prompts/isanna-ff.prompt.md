---
agent: agent
description: "Fast-forward — run all phases in sequence. For simple features or when you want to skip manual phase transitions."
load_set:
  tiny_local:
    - standards/builder-workflow.md
  small_commercial:
    - standards/builder-workflow.md
    - standards/builder-contract.md
  flagship_commercial:
    - standards/builder-workflow.md
    - standards/builder-contract.md
    - prompts/isanna-help.prompt.md
---

# /isanna-ff — Fast Forward

Drive a feature through all six phases in a single session. Use only for simple
features where model diversity and context resets are not required.

Follow `{{BUILDER_ROOT}}/standards/builder-workflow.md` §7 (Fast-Forward Behavior) — it does
not override any phase rule. TDD, evidence discipline, and task-schema
validation still apply.

**For complex or important work, prefer individual phase commands:**
`/isanna-1-specify` → `/isanna-2-design` → `/isanna-3-review` → `/isanna-4-plan` →
`/isanna-5-implement` → `/isanna-6-verify` — each in its own session, switching models
at review and verify boundaries.

---

## Resume Detection

Read `phase-log.yaml` and `spec.yaml` in the spec directory. `spec.yaml` gives
authoritative `current_phase` and `status`. Fall back to canonical artifact
existence (never rendered Markdown — `ai_native` mode omits the `.md` companions):

| Canonical file exists?                          | Phase completed |
| ----------------------------------------------- | --------------- |
| `requirements.yaml`                             | 1 — Specify     |
| `design.yaml`                                   | 2 — Design      |
| `review-log.yaml` with GREEN verdict            | 3 — Review      |
| `tasks.yaml`                                    | 4 — Plan        |
| All planned tasks evidenced in `phase-log.yaml` | 5 — Implement   |

Show progress and resume from the first incomplete phase. Ask the user to
continue from that phase without asking the user to confirm. Report the chosen
resume point before proceeding so the user can see what happened.

Also load `decisions.yaml` and `traceability.yaml` if they exist.
Load `system-model.yaml` too.

---

## Initialization

If no spec directory is active:

1. If user provided a description → derive kebab-case name, create `.builder/specs/<name>/`.
2. If `.builder/specs/` has in-progress specs → show them, ask which to continue.
3. If nothing → ask what to build.

Scope check: if the request spans multiple independent subsystems, decompose
into separate specs first.

---

## Phase Execution

Execute each phase following its dedicated prompt. Those prompts are the
authority — this file only orchestrates their sequence. If any phase hits a
hard stop, stop immediately.

- **Phase 1:** follow `/isanna-1-specify`. Write `system-model.yaml`, `requirements.yaml`, and rendered `requirements.md`. In fast-forward mode, treat the `/isanna-ff` command as approval to continue once the artifacts validate.
- **Phase 2:** follow `/isanna-2-design`. Write `design.yaml` and rendered `design.md` (+ user-visible behavior deltas when needed). In fast-forward mode, treat the `/isanna-ff` command as approval to continue once the artifacts validate.
- **Phase 3:** follow `/isanna-3-review`. Run the 4-pass review protocol defined in `/isanna-3-review`. Emit verdict.
  ⚠️ Context warning: you are reviewing your own spec. Recommend resuming
  `/isanna-3-review` in a new session with an `independent_reviewer`. In fast-forward mode, surface the warning but continue automatically unless a true hard stop applies.
- **Phase 4:** follow `/isanna-4-plan`. Write `tasks.yaml` and rendered `tasks.md`. In fast-forward mode, treat the `/isanna-ff` command as approval to continue once the artifacts validate.
- **Phase 5:** follow `/isanna-5-implement`. Execute in dependency order. For large
  task lists, propose batches with capability classes (workflow §4) and continue with the approved recommendation from the current fast-forward run unless a HUMAN GATE or hard blocker requires user input.
- **Phase 6:** follow `/isanna-6-verify`. Run the 7 verification categories defined in `/isanna-6-verify`.
  ⚠️ Context warning: you are verifying your own implementation. Recommend
  resuming `/isanna-6-verify` in a new session with an `independent_reviewer`. In fast-forward mode, surface the warning but continue automatically unless a true hard stop applies.

Fast-forward autonomy rule: the `/isanna-ff` command itself is authorization to continue across ordinary resume, approval, and review/verify warning boundaries. Stop only for a hard blocker, a true HUMAN GATE decision, or an unrecoverable validation failure.

Risk-class stop-gates (override the autonomy rule): if the spec is destructive/migratory, security-sensitive, cross-repo, public-API-changing, or data-migrating, `/isanna-ff` MUST STOP before Phase 3 and before Phase 6 and hand off to independent review/verify — a human, or a fresh session run with an `independent_reviewer`. For these classes the self-review (Phase 3) and self-verify (Phase 6) warnings are NOT downgraded to a "continue automatically" notice; the stop is a hard blocker until independent sign-off is recorded.

---

## Completion

Show final summary: files created/modified, tests passing (count), spec
directory contents, phase-log summary, any new tasks created during verify.

## Workflow Telemetry

After durable artifact writes complete and before the final completion handoff,
persist one `workflow-event` via `.builder/scripts/record-workflow-event.py`
or the helpers in `.builder/scripts/_telemetry/record.py`
(`write_phase_completion_event`, `write_decision_event`). Record `command`,
`phase` when applicable, `spec`, `used_model`, `used_model_class` when known,
`thinking_effort`, `reason_category`, `execution_path`, `outcome_category`,
`artifacts_read`, `artifacts_written`, `validation_refs`, and `next_command`.
If runtime-measured `input_tokens`, `output_tokens`, `total_tokens`,
`latency_ms`, or `tokens_per_second` are available from the host, include them
with `capture_source: runtime_measured`; otherwise set
`capture_source: unavailable`. Do not derive token counts, latency, or throughput manually.

---

## Phase-FF Deltas

**Completion handoff fields:**

Persist `handoff.yaml` before rendering the final completion block.

| Emoji | Field            | Value                                                   |
| ----- | ---------------- | ------------------------------------------------------- |
| 🏁    | Feature          | \<feature-name\>                                        |
| 📁    | Spec directory   | .builder/specs/\<feature\>/                           |
| ✅    | Phases completed | 1–6                                                     |
| 📌    | Status           | COMPLETE [or COMPLETE with follow-up tasks]             |
| 🤖    | Used model       | \<model + profile\>                                     |
| ▶     | Next command     | Merge when ready, then run /isanna-archive \<feature-name\> |
| 🧠    | Model advice     | Merge when ready, then run /isanna-archive.                 |

Use the **"Builder Complete"** header variant from
`builder-handoff-template.prompt.md`.

**`phase-log.yaml`:** individual phase executions maintain the log. No
additional entries from `/isanna-ff` itself.
