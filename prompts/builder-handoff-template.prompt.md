---
agent: agent
description: "Canonical Builder handoff visual format — referenced by Builder prompts"
---

# Builder Handoff Template

# Omit Target profile and Fallback chain lines when target_model_profile is not set in spec.yaml.
📦 Target profile: {{target_model_profile | legacy (flagship_commercial)}}
🔁 Fallback chain: {{autonomous_fallback_profiles | n/a}}

This is the canonical visual template for Builder handoff blocks.
Use top and bottom dividers only. Do not use left or right borders.

> You are not invoked directly. Builder prompts reference this file for the
> handoff output format.

---

## Visual Format

Use this layout for standard handoffs:

````
```
━━━━━━━━━━ BUILDER HANDOFF ━━━━━━━━━━
{{FIELD_LINES}}
🤖 Used model     : {{USED_MODEL}}
🧠 Model advice   : {{MODEL_ADVICE}}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```
````

Use this layout for batch or incremental handoffs:

````
```
━━━━━━━━━━ BUILDER HANDOFF ━━━━━━━━━━
{{FIELD_LINES}}
🤖 Used model     : {{USED_MODEL}}
🧠 Model advice   : {{MODEL_ADVICE}}
📈 Progress       : {{PROGRESS_BAR}}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```
````

Use this layout for full completion:

````
```
━━━━━━━━━━ BUILDER COMPLETE ━━━━━━━━━
{{FIELD_LINES}}
🤖 Used model     : {{USED_MODEL}}
🧠 Model advice   : {{MODEL_ADVICE}}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```
````

Use this layout for utility commands (`/isanna-setup`, `/isanna-sync`, `/isanna-archive`,
`/isanna-debug`). Note that `isanna-list`, `isanna-validate` and `isanna-telemetry` are NOT
slash commands -- they have no prompt file and run as scripts under `.builder/scripts/`.

````
```
━━━━━━━━━━ BUILDER UTILITY ━━━━━━━━━━
🧭 Command        : {{COMMAND_NAME}}
{{FIELD_LINES}}
🤖 Used model     : {{USED_MODEL}}
▶  Next command   : {{NEXT_COMMAND}}
🧠 Model advice   : {{MODEL_ADVICE}}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```
````

---

## Icon Guide

Use semantic icons instead of color markers:

- `🧭` phase or stage
- `📁` spec directory or location
- `📝` files written or artifacts
- `✅` completed work, pass state, approved outcome
- `⛔` blocked work or hard stop
- `📌` remaining work, status, or neutral state
- `▶` next command or next phase
- `🤖` model used for the completed run
- `🧠` model advice
- `📈` progress bar or completion ratio
- `🏁` feature completion summary

---

## Formatting Rules

- Align the `:` characters vertically when practical.
- Wrap long values onto indented continuation lines.
- Keep the block compact and scannable.
- Preserve the output inside a fenced code block.
- Never render the handoff as a markdown table. The output format is always the fenced code block with emoji-prefixed lines and aligned colons, as shown in the examples below.
- Use no side borders at all.
- Always include a `Used model` line.
- Format `Used model` as `<model name> <reasoning profile if known>`.
- If the reasoning profile is unavailable, report only the model name.
- When a next step targets a specific spec, append the explicit spec name to `Next command`.
- For approval transitions, emit the handoff block as the final user-facing response after any metadata updates.
- Do not replace an approval handoff with plain prose such as `Next step:` or `Run /isanna-3-review` outside the fenced handoff block.
- If verification or metadata updates are needed before approval is recorded, do that work first and then still output the canonical handoff block.

---

## Examples

### Standard phase handoff

```
━━━━━━━━━━ BUILDER HANDOFF ━━━━━━━━━━
🧭 Phase completed : 1 — Specify
📁 Spec directory  : .builder/specs/<feature>/
📝 Files written   : system-model.yaml, requirements.yaml, requirements.md, spec.yaml, decisions.yaml, handoff.yaml
✅ Outcome         : APPROVED
▶ Next phase       : 2 — Design
▶ Next command     : /isanna-2-design <feature-name>
🤖 Used model      : GPT-5.4 Xhigh reasoning
🧠 Model advice    : Continue with the same model,
                     or switch for a fresh perspective.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

### Batch handoff

```
━━━━━━━━━━ BUILDER HANDOFF ━━━━━━━━━━
🧭 Phase           : 5 — Implement
📁 Spec directory  : .builder/specs/<feature>/
✅ Tasks completed : 6, 7, 9 of 15
⛔ Tasks blocked   : 8 (depends on 7)
📌 Tasks remaining : 10, 11, 12, 13, 14, 15
▶ Next command     : /isanna-5-implement 10-15 <feature-name>
🤖 Used model      : GPT-5.4 Xhigh reasoning
🧠 Model advice    : Follow the Phase 4 recommendation for `/isanna-5-implement 10-15`
                     using GPT 5.4 or Opus.
📈 Progress        : ██████░░░░░░░░░░  6/15 tasks done
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

### Plan handoff (Phase 4 with run dependencies)

```
━━━━━━━━━━ BUILDER HANDOFF ━━━━━━━━━━
🧭 Phase completed  : 4 — Plan
📁 Spec directory   : .builder/specs/<feature>/
📝 Files written    : tasks.yaml, tasks.md, traceability.yaml, handoff.yaml
✅ Outcome          : APPROVED
📌 Task count       : 12 tasks (8 parallelizable)
📌 Execution mode   : batched
📌 Recommended runs :
   1) /isanna-5-implement 1-3 <feature> — mixed — Sonnet
      depends: none — parallel: run 2
   2) /isanna-5-implement 4-5 <feature> — mechanical — Sonnet
      depends: none — parallel: run 1
   3) /isanna-5-implement 6-8 <feature> — complex — Opus
      depends: run 1 — parallel: none
   4) /isanna-5-implement 9-12 <feature> — mixed — Sonnet
      depends: run 3 — parallel: none
📌 Batching advice  : Pipeline work in run 3 needs Opus reasoning.
                      Runs 1 & 2 are independent and can start together.
▶ Next phase        : 5 — Implement
▶ Next command      : /isanna-5-implement 1-3 <feature>,
                      /isanna-5-implement 4-5 <feature>
🤖 Used model       : GPT-5.4
🧠 Model advice     : Start runs 1 & 2 in parallel (both Sonnet).
                      Run 3 requires Opus. Run 4 can use Sonnet.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

### Completion handoff

```
━━━━━━━━━━ BUILDER COMPLETE ━━━━━━━━━
🏁 Feature         : <feature-name>
📁 Spec directory  : .builder/specs/<feature>/
✅ Phases complete : 1-6
📌 Status          : COMPLETE
▶ Next command     : /isanna-archive <feature-name> (after merge)
🤖 Used model      : GPT-5.4 Xhigh reasoning
🧠 Model advice    : Merge when ready. Archive the spec after merge.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

### Utility handoff (example: `/isanna-archive`)

```
━━━━━━━━━━ BUILDER UTILITY ━━━━━━━━━━
🧭 Command        : /isanna-archive
📁 Spec directory : .builder/specs/csv-import-validation/
✅ tasks.md       : 12 tasks, 0 errors
✅ phase-log.yaml : 5 phases, 0 errors
🤖 Used model     : GPT-5.4
▶  Next command   : /isanna-5-implement 1-3 csv-import-validation
🧠 Model advice   : Plan is valid; proceed to implementation.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```
