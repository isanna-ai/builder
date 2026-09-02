---
agent: agent
description: "Phase 5 — autonomous runner task implementation."
load_set:
  tiny_local:
    - standards/builder-guardrails-implement.md
    - standards/builder-tdd.md
    - standards/builder-workflow.md
  small_commercial:
    - standards/builder-guardrails-implement.md
    - standards/builder-tdd.md
    - standards/builder-workflow.md
    - standards/builder-contract.md
    - skills/planning/SKILL.md
  flagship_commercial:
    - standards/builder-guardrails-implement.md
    - standards/builder-tdd.md
    - standards/builder-workflow.md
    - standards/builder-contract.md
    - skills/planning/SKILL.md
---

# /isanna-5-implement

Execute the approved runner task packets for the requested spec. Runtime input is `.builder/specs/<feature>/runs/task-<id>.yaml`; do not read prompts as task instructions.

## Artifact Mode

Load canonical YAML as the source of truth. In `ai_native` mode, treat rendered Markdown as an optional export and do not require it as implementation input unless the packet or human gate explicitly asks for it.

## Per-Task Loop

1. Load the task packet, listed canonical YAML artifacts, and declared file slices only.
2. Load applicable constitution context when the task packet or spec includes a guardian review.
3. Confirm all `depends_on` task ids are complete before starting.
4. If `tdd_mode: required`, write the failing test first and capture RED stdout tail.
   If it PASSES against unmodified code, stop and read the target file: when the behavior
   is already implemented, STOP the task and report the `already-shipped` outcome naming the
   file, the symbol, and the passing command. Only when the code genuinely lacks the behavior
   is the test at fault — never edit a test until it fails without checking this first.
5. Implement the smallest code change that satisfies the task without expanding beyond approved constitution bounds.
6. Run every `verify_commands` entry fresh.
7. Capture GREEN and VERIFY evidence with command, exit code, stdout tail, and files written.
8. Update `traceability.yaml` and task evidence only after verification passes.

## Evidence Contract

After each attempted task, write evidence that includes:

- task id
- command run
- exit code
- stdout tail
- files written
- RED/GREEN/VERIFY labels when TDD is required
- `source` (`host` or `agent`) — provenance of the exit code
- timestamp — required for host-captured RED evidence

RED evidence MUST be produced by actually running the focused verify command (capture the command, exit code, and timestamp), with `source: host` when host-captured. A fabricated or `source: agent` RED entry for a `tdd_mode: required` task is a guardrail violation. Full mechanical host-capture of RED is a flagged dispatcher follow-up; the evidence field and this contract apply now.

## Outcomes

Use one outcome for the batch: `completed`, `partial`, `already-shipped`, or `rollback`.

## Telemetry

When recording an implementation result, include `reason_category` whenever the
outcome is partial or blocked, using a concise machine-readable cause such as
`test_failure`, `dependency`, or `scope_change`.

```sh
python3 .builder/scripts/record-workflow-event.py --phase 5-implement --outcome-category <outcome> --spec <spec-name>
```
