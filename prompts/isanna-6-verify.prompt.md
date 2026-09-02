---
agent: agent
description: "Phase 6 — host-first autonomous verification."
load_set:
  tiny_local:
    - standards/builder-guardrails-verify.md
    - standards/builder-tdd.md
    - standards/builder-workflow.md
  small_commercial:
    - standards/builder-guardrails-verify.md
    - standards/builder-tdd.md
    - standards/builder-workflow.md
    - standards/builder-contract.md
    - skills/planning/SKILL.md
  flagship_commercial:
    - standards/builder-guardrails-verify.md
    - standards/builder-tdd.md
    - standards/builder-workflow.md
    - standards/builder-contract.md
    - skills/planning/SKILL.md
---

# /isanna-6-verify

Verify implemented tasks against the spec with bounded auto-fix.

## Verification Categories

Produce a PASS or FAIL verdict for each of these seven categories. Any FAIL that is not closed by bounded auto-fix becomes a follow-up task; an unresolved coverage or evidence FAIL means the spec does not PASS.

1. **Requirement coverage** — every requirement id maps to implemented, evidenced work. An uncovered requirement is a follow-up task, never a PASS.
2. **Acceptance-criterion pass** — every acceptance criterion, including failure-path criteria, is demonstrated by a focused verify command that exits 0.
3. **Host-verify green** — `validate-spec.py`, required-file checks, project check, and project test all exit 0 on the host. The gate reads exit code only and proves the FINAL green state; it does not prove any test ever failed.
4. **Traceability coverage** — `traceability.yaml` links requirement → design → task → evidence with no orphan or dangling links.
5. **No-regression** — the full project test/check suite passes; no previously green behavior is broken.
6. **Evidence completeness** — every task has evidence (command, exit code, stdout tail, files). Every `tdd_mode: required` task has RED and GREEN entries (command, exit code, and timestamp); ideally `source: host`. Because host-verify proves only the final green state, this RED evidence is the only proof a test could fail. Until host-capture is wired, `source: host` is aspirational: evidence written before the `source` field existed (absent `source`) is treated as legacy and MUST NOT trigger a HALT on that basis alone — only a fabricated or contradictory RED entry is a violation. A present `source: agent` is accepted with a trust caveat, not a hard fail, until host-captured provenance lands. Keep the intent (RED must really have run); a missing RED entry for a `tdd_mode: required` task is still a FAIL.
7. **Constitution & guardrail compliance** — the constitution guardian verdict is neither `block` nor `requires-human-decision`, and implementation honored scope, evidence, and the exit-code-only guardrails.

## Artifact Mode

Run verification against canonical YAML first. In `ai_native` mode, treat rendered Markdown as a derived export and only require it when the verification packet or human gate explicitly asks for it.

## Host-First Verify

Run `validate-spec.py`, file-existence checks, and project check/test on the HOST before any model invocation. CRITICAL host failures HALT verification without invoking the model. Send only a compact verify packet to the model:

1. category verdicts: PASS or FAIL per check
2. first-failure line per failing category
3. stdout tail no longer than `profile.verify_stdout_tail_lines` for each failing command

If the assembled verify packet would exceed `effective_context_tokens - (session_growth_reserve + headroom)`, emit `fallback_verify_overflow`.

## Constitution Guardian

Run `validate-constitution.py <spec-name> --root <root>` with changed-file or diff context when available. A `block` verdict HALTs verification. A `requires-human-decision` verdict cannot be auto-fixed; record the needed decision and stop before archival. Include only the compact guardian verdict and first finding summaries in model packets.

## Bounded Auto-Fix

- Auto-fix only lint, formatting, missing imports, unused code, and obvious mechanical issues.
- Do not change product logic, architecture, tests, migrations, or sensitive flows as an auto-fix.
- Re-run host checks after each fix.
- Maximum auto-fix iterations: 3.
- If the same failure remains after 3 iterations, create or recommend a follow-up task.

## Follow-Up Tasks

Create follow-up tasks for missing behavior, missing tests, incomplete edge cases, or design mismatch. Include files, TDD mode, verify commands, dependencies, and traceability links.

## Compact Verify Packet

The model packet contains only host verdicts, first failures, and bounded tails. Full stdout stays on host.

## Telemetry

Record `execution_path` with the verification event so the host can distinguish
the normal suite, a focused rerun, and a blocked verification path.

```sh
python3 .builder/scripts/record-workflow-event.py --phase 6-verify --outcome-category <outcome> --reason-category <reason> --spec <spec-name>
```
