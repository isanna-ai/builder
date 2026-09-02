---
agent: agent
description: "Systematic debugging workflow — find root cause before fixing, then verify with fresh evidence."
load_set:
  tiny_local:
    - standards/builder-tdd.md
    - standards/builder-workflow.md
  small_commercial:
    - standards/builder-tdd.md
    - standards/builder-workflow.md
    - standards/builder-contract.md
  flagship_commercial:
    - standards/builder-tdd.md
    - standards/builder-workflow.md
    - standards/builder-contract.md
---

# /isanna-debug — Systematic Debugging

Use this for bugs, failing tests, unexpected behavior, performance problems, or
integration issues. Can be invoked standalone or escalated from `/isanna-5-implement`
or `/isanna-6-verify` when tasks keep failing.

Load `{{BUILDER_ROOT}}/standards/builder-tdd.md` (TDD discipline) and `{{BUILDER_ROOT}}/standards/builder-standards.md` (quality checks)
as reference. Debugging follows TDD: reproduce with a failing test before fixing.

If `.builder/setup-decisions.yaml` exists, load it as well. Use its
configured test/check commands, off-limits paths, repo roots, import aliases,
owned paths, and generated paths as the default debugging environment unless
the user explicitly overrides them.

Resolve commands in this exact order when setup decisions exist: repo-specific
override for the active repo, then the default command, then an explicitly
reported fallback derived from repo config.

Core rule: do not propose or apply fixes until you understand the root cause.
If you cannot create a failing test or reliable reproduction for a behavior bug,
you are not allowed to implement a fix.

---

## Phase 1: Root Cause Investigation

1. Read the full error output carefully. Do not skim stack traces.
2. Reproduce the issue consistently. If you cannot reproduce it, gather more evidence.
3. Check recent changes that could explain the regression.
4. In multi-component flows, trace data across component boundaries and identify
   where behavior first diverges from expectations.

If you do not understand what is broken and why, stop here. Do not guess.

---

## Phase 2: Pattern Analysis

1. Find similar working code in the same codebase.
2. Compare the broken path against the working one.
3. List all meaningful differences.
4. Identify config, environment, or dependency assumptions.

---

## Phase 3: Hypothesis and Minimal Test

1. State one specific hypothesis: "I think X is the root cause because Y."
2. Test that hypothesis with the smallest possible change or diagnostic.
3. If it fails, form a new hypothesis. Do not stack guesses.

If the bug changes behavior and you still have no failing test or reproduction,
stop. Do not continue to Phase 4.

If 3 fix attempts fail, stop and question the architecture instead of attempting a fourth patch.

---

## Phase 4: Fix and Verify

1. Create a failing test or reliable reproduction first (per `{{BUILDER_ROOT}}/standards/builder-tdd.md`).
2. Implement one fix that addresses the root cause.
3. Run fresh verification commands and read the full output.
4. Confirm the original issue is resolved and nothing else regressed.
5. Run the TDD verification checklist from `{{BUILDER_ROOT}}/standards/builder-tdd.md`.

---

## Guard Rails

- No fixes without root-cause investigation first.
- No completion claims without fresh verification evidence.
- One hypothesis at a time.
- No behavior fix without a failing test or reliable reproduction first.
- If the issue survives 3 fix attempts, escalate to architecture review or human discussion.

## Workflow Telemetry

Before the final handoff, persist one `workflow-event` via
`.builder/scripts/record-workflow-event.py` or the helper
`write_utility_event` in `.builder/scripts/_telemetry/record.py`. Record
`command`, `used_model`, `thinking_effort`, `capture_source`,
`reason_category`, `execution_path`, `outcome_category`, `artifacts_read`,
`artifacts_written`, `validation_refs`, and `next_command`. If runtime-measured
`input_tokens`, `output_tokens`, `total_tokens`, `latency_ms`, or
`tokens_per_second` are available from the host, include them with
`capture_source: runtime_measured`; otherwise set `capture_source: unavailable`.
Do not derive token counts, latency, or throughput manually.

---

## Handoff

Follow `{{BUILDER_ROOT}}/standards/builder-workflow.md` §8 (Utility Output Contract). Emit a
**BUILDER UTILITY** block (see `builder-handoff-template.prompt.md`) as the
final output with these fields:

| Emoji | Field        | Value                                                                                                                           |
| ----- | ------------ | ------------------------------------------------------------------------------------------------------------------------------- |
| 🧭    | Command      | /isanna-debug                                                                                                                       |
| 📝    | Issue        | \<brief description\>                                                                                                           |
| 🟢    | Root cause   | \<what was wrong\>                                                                                                              |
| 🟢    | Fix          | \<what was changed\>                                                                                                            |
| 🟢    | Verification | \<command run + result\>                                                                                                        |
| 🤖    | Used model   | \<model + profile\>                                                                                                             |
| ▶     | Next command | Return to the blocked workflow command, for example /isanna-5-implement \<range\> \<feature-name\> or /isanna-6-verify \<feature-name\> |
| 🧠    | Model advice | Resume your previous session or switch capability class (workflow §4).                                                          |
