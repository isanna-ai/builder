---
agent: agent
description: "Builder help and command reference."
load_set:
  tiny_local:
    - standards/builder-workflow.md
  small_commercial:
    - standards/builder-workflow.md
    - standards/builder-contract.md
  flagship_commercial:
    - standards/builder-workflow.md
    - standards/builder-contract.md
    - skills/planning/SKILL.md
---

# /isanna-help

## Two-Mode Workflow

Phases 1-4 are interactive flagship chat sessions. Phases 5-6 are autonomous runner sessions executed under a selected `target_model_profile`. tiny_local context fit is a Phase-4 validity condition, not an implementation-time concern.

`Target model profile: tiny_local | small_commercial | flagship_commercial?`

`tiny_local` is narrow, local, fresh-session execution. `small_commercial` allows wider packets and rendered markdown. `flagship_commercial` is the permissive default for legacy and complex plans.

## Thinking-Mode

For unstructured pre-spec exploration without a command, open a flagship chat session, describe the problem domain, and ask the model to help scope requirements. No `/isanna-explore` command is needed. Close the session and run `/isanna-1-specify` when scope is clear.

## Pure CLI Replacements

These are NOT slash commands -- there is no prompt file behind them, so typing one does
nothing. They are scripts, and in an installed project they live under `.builder/scripts/`:

- list specs: `python3 .builder/scripts/list-specs.py`
- validate a spec: `python3 .builder/scripts/validate-spec.py <spec> --root .`
- workflow telemetry: `python3 .builder/scripts/analyze-workflow-telemetry.py --root .`

Gate coverage is part of the `isanna` CLI, which ships with the builder repository rather than
the installer -- run it from a clone as `isanna coverage`.

## Phase Commands

- `/isanna-1-specify <spec>` writes requirements.
- `/isanna-2-design <spec>` writes design.
- `/isanna-3-review <spec>` reviews requirements and design.
- `/isanna-4-plan <spec>` writes runner-ready tasks and packets.
- `/isanna-5-implement <spec>` executes task packets.
- `/isanna-6-verify <spec>` runs host-first verification.

`/isanna-ff <spec>` is a workflow orchestrator (not a utility): it runs Phases 1-6 in one session.

## Utility Slash Commands

These ship as prompt files and emit a utility handoff block:

- `/isanna-setup` configures local project rules.
- `/isanna-sync` detects drift between spec artifacts and implementation.
- `/isanna-archive <spec>` archives a completed, verified spec.
- `/isanna-debug` runs a structured, root-cause debugging workflow.

Everything under **Pure CLI Replacements** above runs as a `python3` script. There is no prompt file behind any of them, so they are not slash commands.
