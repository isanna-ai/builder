---
name: planning
description: "Use when: creating a new spec, planning document, or task list for any feature, migration, refactor, or architectural change; writing tasks.md for a .builder/specs/* directory; converting a design doc, death plan, or issue into an executable task list; reviewing or updating an existing spec's tasks for agent-readiness. Produces the canonical autopilot-native planning format."
---

# Planning — Autopilot-Native Spec Format

All task lists produced for this workspace MUST use the format defined here.
It is optimised for autonomous execution by GitHub Copilot Autopilot and compatible agents.

---

## Spec Structure

A complete spec lives under `.builder/specs/<feature-name>/`:

| File                | Contains                                                                                                         |
| ------------------- | ---------------------------------------------------------------------------------------------------------------- |
| `system-model.yaml` | Structured system-definition layer: actors, capabilities, events, boundaries, rules, behaviors, and integrations |
| `requirements.yaml` | Canonical requirements data                                                                                      |
| `requirements.md`   | Rendered review view for `requirements.yaml`                                                                     |
| `design.yaml`       | Canonical design data                                                                                            |
| `design.md`         | Rendered review view for `design.yaml`                                                                           |
| `tasks.yaml`        | Canonical planning data                                                                                          |
| `tasks.md`          | Rendered review view for `tasks.yaml`                                                                            |
| `review-log.yaml`   | Canonical review findings, amendments, and verdicts                                                              |
| `review-log.md`     | Rendered review view for `review-log.yaml`                                                                       |
| `handoff.yaml`      | Canonical phase handoff data used to render the final chat block                                                 |

Canonical YAML is the source of truth for structured artifacts. Markdown files
are rendered views. New specs use this contract immediately; markdown-only
specs are out of scope.

---

## Format: `tasks.md`

### File Header

```markdown
# [Spec Title] — Tasks

Each task is self-contained: repo, files, steps, shell verification, and a binary done signal.
Dependencies are explicit. Tasks with no `Depends on` can start immediately.
Tasks marked **HUMAN GATE** require a human decision before the agent proceeds.

---
```

`tasks.yaml` is the canonical planning artifact. `tasks.md` is the rendered
human review view and must not diverge from the YAML source of truth.

### Per-Task Block

````markdown
- [ ] N. [Imperative-mood title]
  - **Repo:** `folder/`
  - **Files:** `src/path/to/primary.ts`, `tests/path/to/test.ts`
  - **TDD:** `required` or `exempt (<reason>)`
  - **Steps:**
    1. First concrete action — name the exact function/type/symbol to add, delete, or change.
    2. Second concrete action.
    3. ...
  - **Verify:**
    ```sh
    ! grep -r 'TargetSymbol' src/ tests/   # exit 0 only when zero hits remain
    cd /path/to/project && deno task check && deno task test:unit
    ```
  - **Done when:** Single binary condition — `! grep` exits 0 (zero hits); type check and unit tests pass.
  - **Depends on:** N, M (or: none)
  - **Parallel with:** N, M, O (or: none)
  - **HUMAN GATE:** The exact decision a human must make before this task or subsequent tasks proceed.
````

The `HUMAN GATE` line is omitted entirely when not needed.

---

## Field Rules

HUMAN GATE is banned when target_model_profile is set in spec.yaml. validate-spec.py runner_ready check exits nonzero if any task has a human_gate field and spec.yaml declares a profile. HUMAN GATE remains valid in legacy mode (no target_model_profile).

`environment_readiness` is an optional `spec.yaml` array; each entry has `id`, `description`, and `verify`, and `verify` must match `runner.schema.yaml` `shell_allow_list`. The runner executes all verifies before starting.

`post_runner_review` is optional metadata with `required` bool and `scope` enum `full|security_only|architecture_only|none`; it is surfaced in final handoff and does not gate merge.

### Traceability Per-File Metadata (Runner Contract)
When a spec’s `traceability.yaml` references files under `task_links[].files`, each file entry SHALL be represented as an object with the fields below (as applicable to the validator/schema):

- `path` (string, required)
- `relevance` (primary|supporting|test, required)
- `estimated_tokens` (integer, optional)
- `load_priority` (must|should|optional, optional)
- `full_read_eligible` (boolean, optional — when false, only anchored slices are valid load mode)
- `anchors` (array, optional — each with `id`, `kind` in {literal_string|regex_v1|symbol_v1}, `locator` string, `estimated_tokens` integer)
- `summary_id` (string|null, optional — references `summaries.yaml`)
- `measured_tokens` (integer, optional — populated by runner from tokenizer)

(These fields are used by the autonomous runner to decide what portions of files it may load, and how it must anchor reads.)

### summaries.yaml Schema

`summaries.yaml` has top-level `summaries`. Each entry has `id`, `source_file`, `purpose` (`requirements_summary|design_summary|task_summary|verify_summary`), and `body` (string <=1600 chars). The runner reads `summaries.yaml` but never generates entries. Phase 4 writes this file when any run packet lists a `summary_id`.



| Field             | Rule                                                                                                                                                            |
| ----------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Repo**          | One of: `your-repo/` or any workspace repo folder name. Cross-repo tasks list all affected repos.                                                               |
| **Files**         | Every file the agent will read or modify — no surprises mid-task. `TDD: required` tasks MUST include a test file.                                               |
| **TDD**           | `required` for behavior changes and bug fixes. `exempt (<reason>)` only for: `refactor-only`, `delete-only`, `type-only`, `config-only`, `infrastructure-only`. |
| **Steps**         | Imperative and specific. Name the exact function/type/symbol. Never write "update the file."                                                                    |
| **Verify**        | Shell commands the runner executes. Success MUST be encoded entirely in exit code 0 — the host gate discards stdout/stderr and does not read comments. Zero-hit assertion: `! grep -r 'X' src/`. Output assertion: `cmd \| grep -q 'expected'`. |
| **Done when**     | A single binary predicate — no ambiguity. Standard form: `! grep exits 0 (zero hits); type check and tests pass`.                                               |
| **Depends on**    | Task numbers that must complete before this task starts. Write `none` explicitly.                                                                               |
| **Parallel with** | Task numbers that can run concurrently with this one.                                                                                                           |
| **HUMAN GATE**    | Only when a non-code decision blocks progress: product sign-off, architecture review, go/no-go. Write the exact question or decision.                           |

**Verify = exit code only.** The host-verify gate judges each command by its exit code alone; stdout, stderr, and `#` comments are discarded. A command MUST exit 0 only when the assertion holds. Zero-hit assertion, quick form: `! grep -r 'X' src/` (a bare `grep` exits 1 on zero matches — it inverts and fails exactly when the deletion/refactor succeeded). Caveat: `! grep` also treats grep's ERROR exit (2 — bad path, unreadable file, permission denied) as success, masking a broken check. Robust form: `grep -r 'X' src tests; test $? -eq 1` — passes ONLY on a clean no-match run (exit 0 = found → fail; exit 2 = error → fail), or wrap the check in a helper script that fails closed. Output assertion: `cmd | grep -q 'expected'`.

**Task sizing.** A task is ONE red→green cycle: one behavior, ≤3 non-test files, steps runnable in a single runner turn. If `done_when` needs "and" more than once, split. Plan gate: warn on >5 files or >8 steps; block on >8 files.

---

## Dependency Graph Rules

- Tasks with `Depends on: none` form the first wave — they can all start in parallel.
- Group by earliest unblocked start time, not by thematic cluster.
- **Never use phase headers** (`## Phase 1`, `## Phase 2`). Dependencies encode sequence implicitly.
- Gate tasks (regression checks, sign-off reviews) are ordinary tasks with accumulated `Depends on` entries.
- The dependency graph should be readable from the `Depends on` and `Parallel with` fields alone.

---

## Format: `requirements.md`

`requirements.yaml` is the canonical requirements artifact. Render
`requirements.md` from that YAML for review.

Per-requirement block:

```markdown
### Requirement N — [Title]

**User story:** As a [role], I want [outcome] so that [value].

**EARS acceptance criteria:**

- WHEN [trigger], the system SHALL [response].
- IF [unexpected condition], THEN the system SHALL [response].
- WHILE [state], the system SHALL [response].
- WHERE [optional feature or mode is enabled], the system SHALL [response].
- The system SHALL [response].
```

Use the smallest set of EARS patterns that fits the behavior.
Combine patterns when needed, for example:
`WHILE [state], WHEN [trigger], the system SHALL [response].`

---

## Format: `system-model.yaml`

Use this file as the structured system-definition layer for the spec. It is not
a replacement for `requirements.yaml`; it reduces ambiguity before design and
planning.

Required shape:

```yaml
version: 1

what:
  entities: []
  capabilities: []

who:
  actors: []

when:
  events: []

where:
  boundaries: []

why:
  rules: []

how:
  behaviors: []

upstream:
  sources: []

downstream:
  sinks: []
```

Per-entry fields:

- `what.entities[]`: `id`, `name`
- `what.capabilities[]`: `id`, `name`
- `who.actors[]`: `id`, `name`, `capabilities`
- `when.events[]`: `id`, `name`, `trigger`
- `where.boundaries[]`: `id`, `name`, `purpose`
- `why.rules[]`: `id`, `statement`, `applies_to`
- `how.behaviors[]`: `capability`, `success`, `failures`
- `upstream.sources[]`: `id`, `name`, `contract`
- `downstream.sinks[]`: `id`, `name`, `contract`

Rules:

- Every top-level section MUST exist.
- Use empty lists (`[]`) when a section is intentionally not applicable.
- Do not omit a required section because the answer is unknown; clarify it first.
- Actor `capabilities` MUST reference defined capability ids.
- Behavior `capability` MUST reference a defined capability id.
- Rule `applies_to` MUST reference ids already defined elsewhere in the file.

Authoring guidance:

- Prefer grounded inference from the request, codebase, and docs over asking
  unnecessary questions.
- If a required entry would materially change permissions, triggers,
  integrations, trust boundaries, or failure behavior and cannot be grounded,
  ask one clarification question before finalizing the file.

---

## Format: `design.md`

`design.yaml` is the canonical design artifact. Render `design.md` from that
YAML for review.

Required sections:

1. **Responsibility Allocation** — table of Move / Keep / Delete decisions across repos/files.
2. **Core Changes** — new types, APIs, or pipeline modifications; before/after where helpful.
3. **Telemetry Strategy** — events retired, events added or promoted, migration mapping.
4. **Verification Strategy** — exact commands that prove the design is correctly implemented.

---

## Checklist Before Calling a tasks.md "Ready"

- [ ] Every task has all eight standard fields (Repo, Files, TDD, Steps, Verify, Done when, Depends on, Parallel with).
- [ ] "Done when" is binary — a human or agent can determine pass/fail without judgment.
- [ ] `TDD: required` tasks include a test file in Files and begin Steps with a RED step.
- [ ] `TDD: exempt` tasks name one of the five allowed exemption reasons.
- [ ] Verify block contains runnable shell commands whose success is encoded entirely in exit code 0 (no bare zero-hit `grep` — use `! grep`).
- [ ] No phase headers anywhere in the file.
- [ ] Dependency graph is acyclic and consistent (task A depends on B ↔ B does not depend on A).
- [ ] Every HUMAN GATE states the exact decision, not just "get approval."
- [ ] Cross-repo tasks reference the correct repo in **Repo** and use absolute workspace paths in Verify.

---

## Reference Example

`.builder/specs/your-feature/tasks.md` — example multi-task migration
using this format end-to-end.
