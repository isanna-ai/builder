---
agent: agent
description: "Phase 4 — plan runner-ready tasks."
load_set:
  tiny_local:
    - requirements.yaml
    - design.yaml
    - traceability.yaml
    - standards/builder-workflow.md
  small_commercial:
    - requirements.yaml
    - design.yaml
    - traceability.yaml
    - skills/planning/SKILL.md
    - standards/builder-contract.md
    - standards/builder-workflow.md
  flagship_commercial:
    - requirements.yaml
    - design.yaml
    - traceability.yaml
    - skills/planning/SKILL.md
    - standards/builder-contract.md
    - standards/builder-workflow.md
---

# /isanna-4-plan

Create runner-ready `tasks.yaml`, `tasks.md`, `traceability.yaml`, and `runs/task-<id>.yaml` packets.

## Artifact Mode

Write canonical YAML first. In `ai_native` mode, keep `tasks.md` and any human review packet as derived exports unless the user explicitly requests readable output.

## Target Profile

Accept `--target-model-profile tiny_local|small_commercial|flagship_commercial`. WHEN `/isanna-4-plan` is invoked without `--target-model-profile`, ask as the final line before generating tasks:

`Target model profile: tiny_local | small_commercial | flagship_commercial?`

Lock the answer for the plan session and write it to `spec.yaml` as `target_model_profile`, to `handoff.yaml`, and to `decisions.yaml` as a resolved decision.

## Runner-Ready Gates

Gate 0 (constitution guardian): run or account for `validate-constitution.py <spec-name> --root <root> --no-model`. Report the guardian verdict before approval. `block` prevents approval; `requires-human-decision` requires an explicit decision in `decisions.yaml`; `warn` must be listed with mitigation or acceptance.

Gate 1 (R7): if `target_model_profile` is set in `spec.yaml`, scan `tasks.yaml` for any task with `human_gate`; list task ids and block approval.

Gate 2 (R8): if `decisions.yaml` has any entry with `status=unresolved`, list ids and block approval.

For each anchor in `traceability.yaml` `files[].anchors`, grep the source file for the locator. Zero hits is `ANCHOR_MISSING` and blocks approval. Report file path, anchor id, and locator.

Gate 3 (verify-command lint): the host gate judges verify by exit code only (stdout/stderr/comments discarded). Block approval if any task's verify list contains a non-probative command — matching a denylist of `true`, `exit 0`, bare `echo`/`ls`/`cat`, or a bare `grep` used for a zero-hits assertion (must be negated `! grep`) — or if any packet would emit empty `verify_commands`.

Gate 4 (red baseline): for every `tdd_mode: required` task, the focused verify command MUST fail (exit non-zero) on the pre-implementation tree — a focused verify that already passes is `NON_PROBATIVE` and blocks approval; rewrite it so it can only pass once the task behavior exists. MECHANICAL enforcement (dispatcher host-runs this at plan approval) is a flagged follow-up, not yet wired.

Gate 5 (task sizing): each task is ONE red→green cycle — one behavior, ≤3 non-test files, steps runnable in a single runner turn. If `done_when` needs "and" more than once, split. Warn on >5 files or >8 steps; block on >8 files.

## packet_fit

For each recommended run, compute and emit `packet_fit`:

- `status` is `fit`, `requires_fallback`, or `not_fit`
- `initial_packet_tokens` equals the sum of `estimated_tokens` for all files declared in the run packet per `traceability.yaml`
- if `initial_packet_tokens > profile.effective_context_tokens`, status is `not_fit`
- if `initial_packet_tokens > profile.initial_packet_cap_tokens`, status is `requires_fallback`
- if full-read count exceeds `profile.max_full_read_files` or slice-read count exceeds `profile.max_slice_files`, status is `requires_fallback`

`not_fit` blocks approval. `requires_fallback` requires explicit acknowledgement.

## verify_fit

For each recommended run, compute and emit `verify_fit` (symmetric to `packet_fit`). `BUILDER_HOST_VERIFY_TIMEOUT` (default 240) is an AGGREGATE budget for the WHOLE gated turn, not a per-command limit — and the host gate APPENDS the setup-decisions default `test` + `check` commands to every gated turn, so those run on top of the task's own verify commands.

- each verify command carries `estimated_runtime_seconds`
- `turn_verify_seconds` equals the sum of those estimates for the task's own verify commands PLUS the setup-decisions default `test` and `check` command estimates (which the gate always appends)
- if `turn_verify_seconds > 210`, status is `not_fit` — block below `BUILDER_HOST_VERIFY_TIMEOUT` (240) to leave headroom for process-start and cleanup margin, so a turn measured at ~230s does not time out at the gate

`not_fit` blocks approval. Prefer focused test invocations over suite-wide commands; the full project suite belongs in the spec's final verification task, not every task.

## Runner Packet Emission

After approval passes, emit `.builder/specs/<feature>/runs/task-<id>.yaml` for each task conforming to `schemas/runner-task.schema.yaml`. Include file load plan with `load_priority` and anchors from `traceability.yaml`, summaries refs when `summaries.yaml` exists, task Verify commands, `red_tail_token_budget = profile.verify_stdout_tail_lines * 4`, and mapped `tdd_mode`.

### Self-contained contract (P0.1)

The runner packet is the implementer's EXCLUSIVE runtime interface — the runner sees the packet, not `tasks.yaml`. It MUST therefore carry a normative description of WHAT to build, copied VERBATIM from the approved task (never inferred, never summarized away):

- `objective` — the task's `title`.
- `steps` — the task's `steps[].text`, in order.
- `done_when` — the task's binary `done_when` predicate(s): each a predicate string, or a structured `{acceptance_id, predicate}`. The runner is "done" only when ALL hold.
- `allowed_change_files` — the task's `files` (the permitted change set).
- `requirement_ids` / `design_ids` / `acceptance_ids` — the resolved ids the task satisfies/realizes/proves, from `traceability.yaml` links and the task's `proves`.
- `required_diff_classes` — inferred from `tdd_mode` (a behavior task, `tdd_mode: required`, gets `[production, test]`).

A packet is INVALID if the runner would have to INFER the desired behavior from the source files: emitting a packet with an empty or missing `objective`, `steps`, `done_when`, or `allowed_change_files` (while its task declares them) is a defect. Copy exactly what the task declares — do not invent objectives, steps, or predicates the task does not state.

These fields are OPTIONAL in the schema (legacy packets keep validating) and the dispatcher fills them from the approved task at dispatch time, but the planner MUST author them so the packet is self-describing on its own. Under `BUILDER_PACKET_CONTRACT=enforce` (default off), an implement turn whose packet lacks a non-empty `objective` + `steps` + `done_when` + `allowed_change_files` is REJECTED.
