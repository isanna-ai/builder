# Builder Workflow

## Path convention: `{{BUILDER_ROOT}}`

Prompts refer to installed assets as `{{BUILDER_ROOT}}/standards/...`, `{{BUILDER_ROOT}}/skills/...`
and so on. **`{{BUILDER_ROOT}}` is the `.builder/` directory at the root of the project being
worked on.** So `{{BUILDER_ROOT}}/standards/builder-tdd.md` means `.builder/standards/builder-tdd.md`.

It is a token rather than a literal path on purpose: the prompts themselves are installed to a
different place for each agent (`.github/prompts/`, `.claude/commands/`, or a Codex skill
directory), so a relative path written from the prompt's own location would resolve differently in
each. `lint-builder-assets.py` enforces the token, and nothing substitutes it — resolve it against
the project root when you read it. Policy — Shared Rules

Canonical source for rules that used to be duplicated across every lifecycle
prompt: question placement, approval gate, Another Pass branch, model capability
classes, and handoff output contract.

When a prompt conflicts with this file, this file wins unless
`.builder/constitution.md` or `.builder/builder-standards.md` is stricter.

Loaded by: `/isanna-1-specify`, `/isanna-2-design`, `/isanna-3-review`, `/isanna-4-plan`,
`/isanna-5-implement`, `/isanna-6-verify`, `/isanna-ff`, `/isanna-archive`, `/isanna-help`.

> **See also: `builder-contract.md`** — normative definition of the status
> state machine, allowed phase transitions, and all artifact schemas.
> When this document and `builder-contract.md` conflict, `builder-contract.md`
> is authoritative.

---

## §0a Two-Mode Workflow
Phases 1–4 are interactive flagship chat sessions. Phases 5–6 are autonomous runner sessions executed under the locked target_model_profile. tiny_local fit is a Phase 4 validity condition — /isanna-4-plan MUST report fit status before the approval gate.

### Cross-mode handoff:
On approval, Phase 4 emits `.builder/specs/<feature>/runs/task-<id>.yaml` (runner-task contract per `schemas/runner-task.schema.yaml`) as the exclusive runtime interface for Phases 5–6. The runner reads `runs/task-<id>.yaml` only; `prompts/*.md` are not read at runtime.

---

## 0. Canonical Artifact Flow

Every lifecycle phase works from canonical YAML artifacts first.

| Phase output | Canonical file | Rendered view |
| --- | --- | --- |
| Planning | `tasks.yaml` | `tasks.md` |
| Review | `review-log.yaml` | `review-log.md` |
| Handoff | `handoff.yaml` | Canonical handoff block |
| Intent | `intent.yaml` | none |
| Intent object backlog | `.builder/intents/<intent-id>/intent.yaml` | none |
| Requirements | `requirements.yaml` | `requirements.md` |
| Design | `design.yaml` | `design.md` |
| Workflow telemetry | `.builder/telemetry/events/<YYYY-MM-DD>/<event-id>.yaml` | none |
| Telemetry analysis | `.builder/telemetry/reports/telemetry-report.yaml` | Canonical utility summary |

Operational rules:

- `intent.yaml` is an additive upstream artifact for outcome-first framing. It has no rendered markdown companion in this rollout.
- `.builder/intents/<intent-id>/intent.yaml` is the file-native backlog intent object for additive planner/Record visibility. Accepted-with-zero-specs is valid backlog work, invalid files render only as path-keyed diagnostics, and this slice does not change release completeness or dispatch behavior.
- dual-write is mandatory when a rendered companion exists: update canonical
  YAML and the rendered markdown or chat block in the same step.
- drift is a hard failure: validator checks must re-render the view from the
  YAML source of truth and reject mismatches.
- immediate rollout applies to every new spec. Existing markdown-only specs are
  out of scope and do not enable a compatibility mode for new work.

## 1. Question Placement

Whenever a Builder prompt asks the user a question (approval choice,
continue/skip, SUGGEST decision, RED-option, HUMAN GATE, divergence, resume
confirmation, cross-model handoff):

- Put all summaries, findings, evidence, previews, or recommendations **before**
  the question.
- The question block MUST be the **final output** in the message so it stays
  visible at the bottom of chat.
- Do not add any prose, bullets, handoff text, or "Next step:" paragraph after
  the question.

If a phase also emits a handoff (post-approval), the handoff block replaces the
question — it is never appended after a question.

---

## 2. Approval Gate Protocol

Every approval-gated phase uses this protocol. Only the final-line text differs
per phase (defined in each phase prompt).

1. Produce the phase artifact(s).
  Write canonical YAML first and the rendered companion in the same step when
  the artifact family has one.
2. Present the summary, findings, and any recommendations.
3. End the message with the phase-specific approval question as the final line.
4. Treat plain user replies `approve`, `approved`, `looks good`, `lgtm`, `ship
   it`, or equivalent approval language as approval.
5. On approval: complete any metadata updates (`spec.yaml`, `phase-log.yaml`,
   `decisions.yaml`, `traceability.yaml`) **before** emitting the handoff block.
6. The final user-facing response after approval MUST be the canonical handoff
   block from `builder-handoff-template.prompt.md`. Never replace it with
   prose or a plain `Next step:` paragraph.

Per-phase approval final lines:

| Phase              | Final line                                                 |
| ------------------ | ---------------------------------------------------------- |
| 1-specify          | `Approve / Another pass / Add more requirements?`          |
| 2-design           | `Approve design / Another pass?`                           |
| 3-review SUGGEST   | `Apply suggestion / Skip / Amend differently?`             |
| 3-review RED       | `<phase-specific options from /isanna-3-review>`           |
| 4-plan             | `Approve tasks / Another pass / Add tasks?`                |
| 5-implement        | `<HUMAN GATE question>` or `<divergence question>`         |
| 6-verify SUGGEST   | `Apply fix / Defer to new task / Skip?`                    |

---

## 3. Another Pass Branch

Triggered when the user answers `Another pass` in Phase 1, 2, or 4.

Rules:

- `Another pass` means "iterate on the current artifact to review, find and fix gaps, and substantively improve what has just been done."
- Do not revise in the same message as the trigger.
- If the user named a concrete defect, address it. Otherwise, proactively review the current phase's work, identify any gaps, weaknesses, or areas for improvement, and address them to produce a stronger iteration.
- Distinguish `Another pass` (iterate current) from `Add more requirements` /
  `Add tasks` (expand scope).
- First summarize the requested revision directions.
- Then recommend one model path using **capability classes** (§4):
  - `Keep same model` when the change is local, additive, or continuity-heavy.
  - `Switch to <capability class>` when the user wants fresh eyes, challenged
    assumptions, or the thread seems stuck. Name a concrete model from the
    class's current mapping in §4.
- End with the final line: `Another pass here / Generate cross-model handoff to <model>?`
- If user picks `Another pass here`: revise the phase's artifacts and return to
  the normal approval gate.
- If user picks `Generate cross-model handoff to <model>?`: emit the cross-model
  handoff packet (§5).

---

## 4. Model Capability Classes

Prompts refer to capability classes, not named models. Mapping is maintained
here so it can be updated in one place.

The runtime source of truth for capability-class to model pairing and per-lane effort levels is
`scripts/_dispatch_runtime/model_registry.py` (`CAPABILITY_MODEL_MAP` /
`CAPABILITY_EFFORT_MAP`). When the pairs change, update **both** this table and
the registry together.

Read the table below, not this paragraph, for the pairs — it is the surface the
linter verifies against the registry. Both lanes are live: `isanna init` generates
`pipeline.default_lane: codex`, and either lane can author, with `--lane` choosing
per dispatch. Every class runs at >= `high` on the Claude lane; Haiku is never used.

The one pairing worth explaining is `independent_reviewer`. It is the
**cross-vendor adversarial reviewer**, and reviewer ≠ author is the discipline that
most reliably catches a frontier model's blind spots — so it is deliberately kept on
a strong model on both lanes rather than being consolidated down with the rest. In
practice that has paid: the reviewer has caught a fail-open gate dodge, an indefinite
hang, a double-executed verify command and an fd leak, none of which a passing test
suite surfaced.

> This table is verified against `model_registry.py` by
> `lint-builder-assets.py --check-model-registry-drift` (CI `make lint`); keep
> the two in sync — the linter fails the build on drift.

| Class                    | Purpose                                                     | Codex lane (`codex-cli`) | Codex effort | Claude lane (`claude-code-cli`) | Claude effort |
| ------------------------ | ----------------------------------------------------------- | ------------------------ | ------------ | ------------------------------- | ------------- |
| `deep_reasoner`          | Ambiguity resolution, architecture, adversarial review      | `gpt-5.6-sol`            | `high`       | `opus-4.8`                      | `high`        |
| `independent_reviewer`   | Phase 3 / Phase 6 — MUST differ from author/implementer     | `gpt-5.6-sol`            | `high`       | `opus-4.8`                      | `xhigh`       |
| `structured_planner`     | Phase 4 — structured output, batch/dependency reasoning     | `gpt-5.4`                | `medium`     | `opus-4.8`                      | `high`        |
| `fast_editor`            | Mechanical edits, scaffolding, renames, docs                | `gpt-5.6-sol`            | `medium`      | `sonnet-5`                      | `high`        |
| `broad_context_explorer` | Discovery, codebase exploration                             | `gpt-5.4`                | `medium`     | `opus-4.8`                      | `xhigh`       |

Phase → class mapping:

| Phase         | Primary class            | Notes                                                                |
| ------------- | ------------------------ | -------------------------------------------------------------------- |
| Explore       | `broad_context_explorer` |                                                                      |
| 1 — Specify   | `deep_reasoner`          |                                                                      |
| 2 — Design    | `deep_reasoner`          |                                                                      |
| 3 — Review    | `independent_reviewer`   | MUST differ from authoring model                                     |
| 4 — Plan      | `structured_planner`     |                                                                      |
| 5 — Implement | Per-batch (see Phase 4)  | `mechanical`→`fast_editor`, `mixed`→`structured_planner`, `complex`→`deep_reasoner` |
| 6 — Verify    | `independent_reviewer`   | MUST differ from implementer; a clean host verdict advances to sync  |
| Sync          | `deep_reasoner`          | Host-driven reconciliation; `synced` is terminal completion           |

Rules:

- **Reviewer/verifier independence on a single-model lane.** The active Claude
  lane intentionally maps `structured_planner`, `independent_reviewer`, and
  `broad_context_explorer` onto the **same** model (`opus-4.8`), differing only
  by effort tier. On such a lane the "MUST differ from author/implementer"
  requirement for Phase 3 (review) and Phase 6 (verify) is satisfied by a
  **fresh session** — a clean context with no memory of the authoring or
  mixed-batch implementation reasoning — running at the `independent_reviewer`
  effort tier (`xhigh`), **not** by a necessarily different model. Same-session
  self-review or self-verification never satisfies independence. Use a different
  concrete model only when the lane provides one (e.g. the Codex lane, or an
  explicit cross-model handoff per §5).
- When a prompt says "switch model" without more context, it means "switch to a
  different concrete model that still matches the target capability class."
- When the current model is unknown, pick the top entry in the target class's
  current mapping that is not the most recently logged `used_model` in
  `phase-log.yaml`.
- Never choose a model by subjective "strongest you can justify" criteria.
  Resolve via the mapping above.

`spec.yaml` and `phase-log.yaml` MAY record `next_model_class` and
`used_model_class` fields in addition to `used_model` to make routing data,
not prose.

---

## 5. Cross-Model Handoff Packet

Emitted when the user picks `Generate cross-model handoff to <model>?` in any
Another Pass branch, or when a phase transition requires a fresh session with a
different capability class (Phases 3, 5, 6).

Structure (fenced copy-paste block):

1. One-paragraph summary of the requested adjustments, open questions, or
   revision goals.
2. Explicit list of files the next model MUST read:
  - Always: `requirements.yaml`, `requirements.md`, `spec.yaml`, `phase-log.yaml`,
    `decisions.yaml`, `.builder/constitution.md`.
  - Phase 1+: add `system-model.yaml`.
  - Phase 2+: add `design.yaml`, `design.md` (including any user-visible behavior deltas when present).
  - Phase 3+: add `review-log.yaml`, `review-log.md` (if present).
  - Phase 4+: add `tasks.yaml`, `tasks.md`, `traceability.yaml`.
   - Phase 5/6: add prior phase's evidence from `phase-log.yaml`.
3. Instruction set:
   - Revise only the target phase's artifacts.
   - Preserve approved earlier-phase scope unless the user explicitly expanded
     it.
   - Do not advance to later phases in the same session.
   - End that model's message with the target phase's approval final line
     (§2 table).

---

## 6. Handoff Output Contract

Applies to every phase that emits a handoff (Phases 1-6, `/isanna-ff`,
`/isanna-archive`).

- Use the canonical visual format in
  `builder-handoff-template.prompt.md`.
- Persist `handoff.yaml` as the source of truth for the handoff fields before
  rendering the chat block.
- Output as a **fenced code block**. Never a markdown table.
- Always include `Used model`, formatted as `<model name> <reasoning profile if
  known>`. If the profile is unavailable, report only the model name.
- Always include `Model advice` derived from §4 for the next phase.
- Never append prose, "Next step:" lines, or bullet summaries after the handoff
  block. A brief status line **before** the block is allowed.
- When a handoff names a next command targeting a specific spec, append the
  explicit spec name suffix (e.g. `/isanna-3-review my-feature`).
- An approval response and its handoff may both appear in the same message, but
  the handoff block MUST be the last thing in the message.

Field values per phase are defined in each phase prompt's "Handoff" section as a
minimal delta table. This file owns the *contract*; each phase owns its
*fields*.

---

## 7. Fast-Forward Behavior

`/isanna-ff` orchestrates Phases 1-6 in one session. It does not override any rule
in this file. When crossing the Phase 3 or Phase 6 boundary inside the same
session, emit a `⚠️ Context warning` before proceeding: the reviewer is the
author, and the verifier is the implementer. Higher-quality runs stop there and
resume the phase in a new session with an `independent_reviewer`.

Within `/isanna-ff`, fast-forward mode itself is the user's approval to continue
from the first incomplete phase and across ordinary phase approval gates. The
agent should report the detected resume point and any advisory warnings, but
those warnings do not pause fast-forward execution. Stop only for a true
human-only gate, a hard blocker, or an unrecoverable validation failure.

Fast-forward never waives TDD, evidence discipline, or task-schema validation.

---

## 8. Utility Output Contract

Applies to every non-lifecycle **utility slash command** (those that ship a
prompt file): `/isanna-setup`, `/isanna-sync`, `/isanna-archive`, `/isanna-debug`, and any future
utility slash command.

`isanna-list`, `isanna-telemetry`, and `isanna-validate` are **pure CLI utilities** run via
`python3 .builder/scripts/…` (see `/isanna-help`); they print reports directly and
do **not** emit a handoff block, so this contract does not apply to them. There
is no `/isanna-explore` command — pre-spec exploration happens in a flagship chat
session (see `/isanna-help` → Thinking-Mode).

- Utility commands MAY render their payload as tables, ASCII diagrams, or
  reports (drift reports, spec lists, debug traces, validator output, etc.).
- When a utility command writes durable state, it SHOULD persist a canonical
  `<command>-report.yaml` alongside the chat summary.
- The **final output** of every utility command MUST still be a fenced
  **BUILDER UTILITY** handoff block from
  `builder-handoff-template.prompt.md`.
- The utility handoff MUST include: `🧭 Command`, `🤖 Used model`,
  `▶ Next command`, `🧠 Model advice`, plus command-specific fields.
- Utility commands follow the same question-placement rule (§1): any user
  question is the final line instead of the handoff; after the answer, emit the
  handoff as the final output.
- Utility commands do not write to `phase-log.yaml` unless they explicitly
  modify a spec (e.g. `/isanna-archive`).

## 8A. Workflow Telemetry

Lifecycle and utility commands SHOULD persist one canonical `workflow-event`
after durable writes complete and before the final handoff block is rendered.

Rules:

- Record only fields the agent trivially knows: command, phase or utility mode,
  spec when applicable, model, model class when known, thinking_effort,
  reason_category, execution_path, outcome_category, artifact references, and
  next_command.
- Runtime-measured compute fields (`input_tokens`, `output_tokens`,
  `total_tokens`, `latency_ms`, `tokens_per_second`) MAY be included only when
  the host provides them explicitly.
- Agents MUST NOT estimate token counts, latency, or throughput.
- `intent_summary` and `outcome_detail` stay bounded and sanitized.
- Telemetry is forward-only for new work. Do not reconstruct events from older
  artifacts.
