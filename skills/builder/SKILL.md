---
name: builder
description: Run the Builder workflow in Codex. Use when the user mentions Builder, builder, spec phases, /isanna-* commands, creating a spec, designing a spec, reviewing a spec, planning tasks, implementing spec tasks, verifying a spec, fast-forwarding a spec, setup, sync, archive, validate, telemetry, or structured debugging with Builder artifacts under .builder/.
---

# isanna-builder for Codex

<!-- The `builder` directory slug is retained for backwards-compatible existing Codex installs. -->

## Overview

Use this skill to make Codex follow the same Builder phase prompts and artifact contract used by Copilot and Claude Code. The installed skill bundle is self-contained: `install.sh --ai codex` stages the current Builder prompts and standards next to this `SKILL.md`.

## Resource Loading

Before acting on a Builder request:

1. Locate the project or workspace root that contains `.builder/`, `.git/`, or a workspace file.
2. For a named phase or `/isanna-*` command, read the matching bundled prompt in `prompts/` completely before taking actions.
3. Read relevant standards when the phase touches their domain:
   - `standards/builder-contract.md` for artifact shape, state, validation, and handoff rules.
   - `standards/builder-workflow.md` for phase sequencing, handoff, question placement, model class, and fast-forward rules.
   - `standards/builder-tdd.md` for planning and implementation tasks.
   - `standards/builder-standards.md` for shared implementation, review, and verification rules.
4. For `/isanna-4-plan`, also read `references/planning-skill.md`.

The paths in 2-4 are relative to THIS skill's own installed directory, which the installer
populates with `prompts/`, `standards/`, `references/` and `agents/` alongside this file --
not to the project's `.builder/`. (An audit read them as project-relative, found no
`references/` in the source repo, and reported the planning reference as dangling; it
resolves.)
5. Read project-local `.builder/constitution.md`, `.builder/setup-decisions.yaml`, and the current spec artifacts only as needed for the requested phase.

If a bundled prompt is missing, say which prompt is missing and continue only when the command has an obvious deterministic fallback such as listing specs or running the validator.

## Command Map

- `/isanna-1-specify`: read `prompts/isanna-1-specify.prompt.md`.
- `/isanna-2-design`: read `prompts/isanna-2-design.prompt.md`.
- `/isanna-3-review`: read `prompts/isanna-3-review.prompt.md`.
- `/isanna-4-plan`: read `prompts/isanna-4-plan.prompt.md`.
- `/isanna-5-implement`: read `prompts/isanna-5-implement.prompt.md`.
- `/isanna-6-verify`: read `prompts/isanna-6-verify.prompt.md`.
- `/isanna-ff` or fast-forward: read `prompts/isanna-ff.prompt.md`.
- `/isanna-debug`: read `prompts/isanna-debug.prompt.md`.
- `/isanna-sync`: read `prompts/isanna-sync.prompt.md`.
- `/isanna-setup`: read `prompts/isanna-setup.prompt.md`.
- `/isanna-archive`: read `prompts/isanna-archive.prompt.md`.
- `/isanna-help`: read `prompts/isanna-help.prompt.md`.

Accept natural language equivalents. For example, "implement the next spec using Builder" maps to `/isanna-5-implement`; "plan phase" maps to `/isanna-4-plan`; "verify the spec" maps to `/isanna-6-verify`.

These are NOT slash commands — there is no prompt file behind them. They are scripts, and the
user runs them directly in an installed project:

- list specs by active/completed/archived state: `ls .builder/specs/`
- validate a spec: `python3 .builder/scripts/validate-spec.py <spec-id>`
- workflow telemetry: `python3 .builder/scripts/analyze-workflow-telemetry.py`

Offering them as `/isanna-list`, `/isanna-validate` or `/isanna-telemetry` sends the user to type
a command that does not exist. `prompts/isanna-help.prompt.md` states the same thing.

## Execution Rules

- Treat bundled prompt files as procedural instructions, not background documentation.
- Follow the phase order and human gates defined in the prompt and standards.
- Prefer canonical YAML artifacts as source of truth, then render Markdown when the workflow requires it.
- Preserve project-owned files and existing specs. Do not rewrite unrelated specs or configuration while running a phase.
- For implementation, execute one task at a time using TDD discipline: fail first where practical, implement the narrow change, run the specified checks, then record evidence.
- For validation, prefer deterministic scripts under `.builder/scripts/`; if a script or runtime is unavailable, record the fallback clearly in the handoff.
- Keep questions inside the active Builder artifact or handoff when the phase instructs it. Ask the user directly only when the next action would be risky or impossible without the answer.
- When resuming, read the current spec's `handoff.yaml`, `phase-log.yaml`, `tasks.yaml`, and rendered Markdown before choosing the next action.

## Completion

End each phase with a concise status: spec name, phase completed, artifacts changed, checks run, and the next recommended `/isanna-*` command.
