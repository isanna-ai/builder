# Builder Contract
Canonical source of truth for the Builder lifecycle state machine, artifact
schemas, evidence schema, prompt/skill metadata contract, portable reference
rules, and validator availability policy.
Installed location: `<target>/.builder/standards/builder-contract.md`.
This document defines the immediate-rollout artifact contract for every new
spec. There is no compatibility mode for markdown-only specs.
## Status State Machine
Valid `spec.yaml` statuses are:
- `specifying`
- `specified`
- `spec-reviewed`
- `designed`
- `reviewed`
- `planned`
- `implementing`
- `implemented`
- `adversarially-reviewed`
- `verifying`
- `verified`
- `verified_with_tasks`
- `syncing`
- `synced`
- `archived`
Allowed phase transitions:
- `specifying` -> `specified`
- `specified` -> `designed`
- `specified` -> `spec-reviewed`
- `spec-reviewed` -> `spec-reviewed` (optional second independent review)
- `spec-reviewed` -> `planned`
- `designed` -> `reviewed`
- `reviewed` -> `planned`
- `planned` -> `implementing`
- `implementing` -> `implemented`
- `implementing` -> `adversarially-reviewed`
- `adversarially-reviewed` -> `adversarially-reviewed` (optional second independent review)
- `adversarially-reviewed` -> `implementing` (review-fix)
- `implemented` -> `verifying`
- `verifying` -> `syncing`
- `verifying` -> `verified_with_tasks`
- `syncing` -> `synced`
- `syncing` -> `verified` (confirmed sync divergence only)
- `verified` -> `syncing` (human-resolved divergence rerun)
- `synced` -> `archived`
- `verified_with_tasks` -> `implementing`
## Canonical Artifact Families
Canonical spec data lives in YAML artifacts. Markdown companions are rendered
views for review, not alternate sources of truth.

Builder supports two artifact modes:

- `dual`: canonical YAML files are required and rendered Markdown companions are
  generated and drift-checked when the artifact family defines a rendered view.
- `ai_native`: canonical structured files are the required working artifacts.
  Markdown companions are derived exports for human review and are generated only
  when explicitly requested by artifact mode, a human gate, or an export command.
| Artifact family | Canonical file | Rendered view |
| --- | --- | --- |
| Planning | `tasks.yaml` | `tasks.md` |
| Review | `review-log.yaml` | `review-log.md` |
| Handoff | `handoff.yaml` | Chat handoff block |
| Intent | `intent.yaml` | none |
| Intent object | `.builder/intents/<intent-id>/intent.yaml` | none |
| Requirements | `requirements.yaml` | `requirements.md` |
| Design | `design.yaml` | `design.md` |
| Traceability | `traceability.yaml` | none |
| Setup | `setup-decisions.yaml` | none |
| Evidence | `evidence/task-<id>.yaml` | `phase-log.yaml` summary |
| Utility reports | `<command>-report.yaml` | Chat summary |
| Workflow events (`workflow-event`) | `.builder/telemetry/events/<YYYY-MM-DD>/<event-id>.yaml` | none |
| Telemetry analysis | `.builder/telemetry/reports/telemetry-report.yaml` | Chat summary |
Rules:
- Canonical YAML is the source of truth for every structured artifact family.
- In `dual` mode, when an artifact family has a rendered companion, prompts and
  tools MUST write both files in the same step.
- In `ai_native` mode, prompts and tools MUST write canonical artifacts first
  and MUST NOT depend on rendered Markdown as working input.
- Validator drift checks MUST re-render the markdown companion from canonical
  YAML and reject mismatches when rendered companions are required or explicitly
  checked.
- Immediate rollout applies to every new spec. Existing markdown-only specs are
  out of scope and are not migrated automatically.
## Artifact Schemas
### `spec.yaml`
Required fields:
- `name`
- `created`
- `status`
- `current_phase`
- `next_action`
Optional fields:
- `next_model_class`
- `used_model_class`
- `artifact_mode` (`dual` or `ai_native`)
- `summary`
Deprecated fields — do NOT add these to a new spec:
- `task_count`
- `tasks_done`
- `tasks_total`
- `tasks_parallelizable`

Nothing writes, derives, or reads these counts. They are typed by hand once and never
reconciled against anything again, so they drift silently and are then read as fact:
`beta-approve-funnel` sat at `status: planned`, `tasks_done: 0 / 11` while every one of its
headline deliverables was live in production. A reader trusting that number would have
rebuilt shipped functionality.

To find out how much of a spec actually exists, run its own acceptance commands —
`isanna verify --spec <name>` — rather than reading a declared count.

Existing specs still carrying these fields are reported by `validate-spec` as a warning.
Set `BUILDER_SPEC_BOOKKEEPING=enforce` to promote it to a hard error, staged the same way as
`BUILDER_TRACE_COVERAGE` and `BUILDER_VERIFY_LINT`.
### `phase-log.yaml`
Required top-level field:
- `phases`
Each phase entry MUST include:
- `phase`
- `completed`
- `used_model`
- `files_written`
- `outcome`
Phase-specific optional fields MAY include:
- `notes`
- `requirement_count`
- `task_count`
- `amendment_rounds`
- `findings`
- `decisions_added`
- `tasks`
- `verification`
### `decisions.yaml`
Required top-level field:
- `decisions`
Each decision entry MUST include:
- `id`
- `phase`
- `timestamp`
- `question`
- `status` (`resolved` or `unresolved`)
Required only when `status: resolved`:
- `chosen`
- `rationale`
Optional fields:
- `alternatives`
Rules:
- `status: unresolved` records an open question that MUST be resolved in its
  owning `phase` before runner-ready approval (see `/isanna-4-plan` Gate 2).
- `chosen` and `rationale` are omitted (or empty) while `status: unresolved`.
- A decision entry with an ABSENT `status` is treated as `resolved` (back-compat
  for pre-existing decisions authored before `status` was required).
- `accepted` is accepted as a legacy synonym for `resolved`.
### `traceability.yaml`
Required top-level fields:
- `artifact` (must be the string `traceability`)
- `spec`
- `requirement_links`
- `design_links`
- `task_links`
Optional top-level fields:
- `intent_links`
Each `intent_links` entry MUST include:
- `intent_id`
- `requirement_ids` (list of linked requirement ids)
Each `requirement_links` entry MUST include:
- `requirement_id`
- `design_ids` (list of design item ids)
Each `design_links` entry MUST include:
- `design_id`
- `task_ids` (list of task ids)
Each `task_links` entry MUST include:
- `task_id`
- `files` (list of affected file paths)
- `evidence_ids` (list of evidence entry ids)
### `system-model.yaml`
Required top-level fields:
- `version`
- `what`
- `who`
- `when`
- `where`
- `why`
- `how`
- `upstream`
- `downstream`
Required nested fields:
- `what.entities`
- `what.capabilities`
- `who.actors`
- `when.events`
- `where.boundaries`
- `why.rules`
- `how.behaviors`
- `upstream.sources`
- `downstream.sinks`
Per-entry required fields:
- entity: `id`, `name`
- capability: `id`, `name`
- actor: `id`, `name`, `capabilities`
- event: `id`, `name`, `trigger`
- boundary: `id`, `name`, `purpose`
- rule: `id`, `statement`, `applies_to`
- behavior: `capability`, `success`, `failures`
- source: `id`, `name`, `contract`
- sink: `id`, `name`, `contract`
### `intent.yaml`
Required top-level fields:
- `artifact` (must be the string `intent`)
- `title`
- `spec`
- `outcome`
- `goal`
- `references`
- `constraints`
- `failure_conditions`
- `success_signals`
- `non_goals`
Required nested fields:
- `goal.summary`
- `references.system_model` (non-empty list)
Optional nested fields:
- `goal.implementation_spec`
- `goal.notes` (non-empty strings)
Per-entry required fields:
- constraint: `id`, `statement`
- failure condition: `id`, `statement`
- success signal: `id`, `statement`
Validation rules:
- `references.system_model` entries MUST resolve to ids that exist in `system-model.yaml`.
- Intent entry ids MUST be unique across `constraints`, `failure_conditions`, and `success_signals`.
- `constraints`, `failure_conditions`, `success_signals`, and `non_goals` MUST each contain at least one entry.
- `non_goals` entries MUST be non-empty strings.

### `.builder/intents/<intent-id>/intent.yaml`
Required top-level fields:
- `artifact` (must be the string `intent-object`)
- `intent`
- `title`
- `status`
- `problem`
- `why`
- `success_criteria`
- `non_goals`
- `ssot_delta`
- `specs`
Optional top-level fields:
- `reason` (required for `rejected` or `superseded`)
- `superseded_by` (allowed only for `superseded`)
Declared status values:
- `proposed`
- `accepted`
- `rejected`
- `superseded`
Computed visible lifecycle values:
- `proposed`
- `accepted`
- `decomposed`
- `in-flight`
- `fulfilled`
- `rejected`
- `superseded`
Rules:
- Backlog intent objects are file-native and separate from spec-local `intent.yaml`; a spec-local `intent.yaml` is not reinterpreted as a backlog object.
- `success_criteria` entries MUST be exact `{id, statement}` mappings with unique ids.
- `ssot_delta` MUST contain exactly `capabilities`, `behaviors`, and `journeys`; each entry MUST be exact `{target, change}` with `change` in `create|enrich|rewire` and `target` unique within its category.
- `specs` is the sole human-authored denominator for the intent. Members use the same bare or `<alias>/<spec-id>` grammar as release refs.
- Accepted intents with zero specs are valid visible backlog work.
- `decomposed`, `in-flight`, and `fulfilled` are computed display states, never human-declared CLI targets.
- `fulfilled` requires every member spec to be both `verification: host-verified` and canonical `spec.yaml.status: synced`; either signal missing keeps the intent below fulfilled.
- Invalid or unreadable intent files fail closed and render only as path-keyed diagnostics; unvalidated content is never shown as trusted backlog content.
- Intent discovery is additive in this slice: it does not change release manifest membership, release completeness math, dispatch queue behavior, or spec phase routing.
## Sync Preconditions
`sync` is the terminal phase of `SPEC_PHASE_ORDER` and the only step that reconciles a
spec's declared SSOT change against what actually changed. It has two repo-level
preconditions and one spec-level one. All three are hard: sync fails closed, never silently.
### Repo-level: the two files
- `.builder/sync-adapter.yaml` — `artifact: sync-adapter` plus a list of `mappings`.
- `docs/system-behaviors.yaml` — the curated behavioral SSOT.

Missing EITHER makes `isanna sync` return `bootstrap_required` (exit 2) for every spec in
the repo. This is per-spec and quiet: it only surfaces when someone runs sync on a spec that
reached the sync phase, so an unbootstrapped repo can accumulate finished, never-synced work
indefinitely with nothing reporting it. Use `isanna ssot audit` to see the state directly.
### Spec-level: `ssot-delta.yaml`
The spec DECLARES its intended SSOT change. A spec without one has nothing to reconcile, so
bootstrapping a repo alone produces no syncs — the specs must declare too.

This is enforced at ADVANCEMENT, not only by `validate-spec`. Completing a phase whose target
status is in the sync-phase required set (`planned` onward) requires `ssot-delta.yaml` to
exist, or `validate_phase_completion` refuses the completion. Staged via
`BUILDER_REQUIRE_SSOT_DELTA`: `warn` (default) reports on stderr and allows, `enforce` refuses,
any other value stays at warn.

It has to be enforced here because `validate-spec.py` is not an advancement gate — the
dispatcher never ran it — so specs reached `planned` with no delta and then could never sync:
no delta means no isolated worktree, which means verify cannot write `sync-scope.yaml`, which
means `validate_scope_evidence` rejects and sync refuses permanently. In a repo curated before
this gate existed, expect nearly every historical spec to carry no delta and therefore to be
unsyncable at spec level.
### The adapter must cover the WHOLE tree
`_sync/adapter.py:observed_tuples` matches each changed path against the adapter's `paths`
patterns (fnmatch; `*` crosses `/`). **Any path matching NO mapping becomes
`{capabilities, unmapped:<path>, enrich}`**, and sync flags every observed tuple the spec did
not declare as `divergence`.

The consequence is the opposite of the intuitive one, and it is the single most important
thing to know before authoring an adapter:

- An INCOMPLETE adapter does not weaken sync — it BLOCKS it. Every uncovered path becomes an
  undeclared tuple, so the spec diverges on files nobody made a claim about.
- An EMPTY adapter (`mappings: []`) loads successfully and clears `bootstrap_required`, then
  fails every sync. It is NOT a safe way to bootstrap a brownfield repo.

To cover a path WITHOUT asserting an SSOT change, map it with `tuples: []`. That is
"recognized, makes no claim" — the correct pattern for shared entry points and root chrome.
Note its cost: empty-tuple mappings under-observe real changes on those paths, so divergence
detection is genuinely weaker there. That is a deliberate, bounded trade, not an oversight.
### Sync goes forward only (owner decision, 2026-07-29)
Historical specs are reconciled at RELEASE level, never synced at spec level.

This is forced, not preferred. Readmission (`isanna sync-readmit`) rebuilds provenance from
the host-recorded verify-bundle chain, and `_sync/readmit.py` raises
`unsafe-evidence-directory` when a spec has no `gate-evidence/` directory. A spec verified
before the gate-evidence runtime existed, or outside it, has nothing to be readmitted ON — so
it can never sync at spec level, however well its repo's adapter is curated. Adopt the gate
early: the evidence cannot be reconstructed after the fact.

When counting a multi-repo portfolio, note that `--projects-root` excludes git WORKTREES from
the census and names them in the output. A worktree shares its main checkout's `.builder/specs`
tree, so counting both double-counts every spec in it. A worktree whose main checkout is NOT in
the audited set fails `--strict` — the only case where excluding one loses coverage rather than
removing a duplicate.

Curating an adapter is therefore necessary but NOT sufficient. Bootstrapping a brownfield repo
makes its FUTURE specs syncable; it does not retroactively make its finished ones syncable.

For work already merged and host-verified without a reconstructable sync ledger, use the
release-level owner-adoption mechanism (`adopted_intents`), which records the reconciliation
explicitly and refuses to fake spec-level sync artifacts. `isanna ssot audit` partitions
`finished_never_synced` into `unsynced_actionable` (has gate evidence — can still sync) and
`historical_no_provenance` (cannot, ever), so the actionable backlog stays a number that can
reach zero. A count that can never be driven down is dismissed, and then ignored when it
finally reports something real.

**Adoption has a precondition the historical population does not currently meet.**
`adoption_satisfied` (`scripts/planning.py`) requires EVERY member to be `host-verified` or
`synced`, and `host-verified` is gate-coverage's stamp — derived from runner-queue
`phase-complete` turns, a DIFFERENT source from the `gate-evidence/` directory that readmission
needs. A spec predating the runtime therefore reads `unknown`, not `host-verified`, so adoption
refuses it: writing owner-authorization text for such a spec produces a manifest entry that
changes no number. Adoption is also only consulted for releases in a LIVE status
(`draft`/`active`, which carry `intents`); a `shipped` release flattens to `specs` and its
`adopted_intents` is parsed and then ignored — so adoption must be recorded BEFORE shipping,
never as a retrofit.

Treat `historical_no_provenance` as a terminal state, not a backlog with a procedure. Owner
adoption does not resolve it, and the contract deliberately does not pretend otherwise: a spec
with neither a gate-evidence chain nor runner-queue provenance has no admissible basis on which
any tool here could stamp it verified. Reconcile that population at RELEASE level and say so
plainly in the audit.
### Archiving past sync
Archiving a spec that never synced discards its SSOT update permanently: the declared delta is
never reconciled and nothing downstream notices. `isanna ssot archive-check --root <repo>
--spec <id>` gates this, staged via `BUILDER_ARCHIVE_REQUIRE_SYNC` (`warn` default,
`enforce` refuses).

Enforcement can also be set PER REPO, in that repo's `.builder/dispatch.yaml`. Both SSOT gates
use the same key shape and the same resolver:

```yaml
pipeline:
  require_ssot_delta: enforce     # advancement gate — or warn (default)
  archive_require_sync: enforce   # archive gate — or warn (default)
```

`require_ssot_delta` corresponds to `BUILDER_REQUIRE_SSOT_DELTA`, `archive_require_sync` to
`BUILDER_ARCHIVE_REQUIRE_SYNC`. One resolver serves both
(`_dispatch_runtime/staged_gate.py:staged_gate_enforced`) so they cannot drift apart — two
copies of "env, then repo key, then warn" would eventually behave differently on the same repo.

Note what `require_ssot_delta: enforce` does and does not do. It fires on ADVANCEMENT, so
already-finished specs are unaffected where they stand — but they cannot be RE-advanced
(re-verified, reworked via `verified_with_tasks`, or synced) until they declare a delta. Before
enabling it on a repo, count the specs already at `planned` or beyond without one.

Resolution order is `BUILDER_ARCHIVE_REQUIRE_SYNC`, then the repo key, then `warn`. The env var
wins because it is the narrower scope — an operator can override for one invocation without
editing a committed file — but an EMPTY env value counts as unset, so a stray
`export BUILDER_ARCHIVE_REQUIRE_SYNC=` cannot silently disable every repo's committed setting.
The repo key exists because enforcement must travel with the REPO: across a portfolio, repos
sit at different stages of SSOT backfill, so "enforce once this repo is curated" is only
expressible per repo. A malformed `dispatch.yaml` degrades to `warn`, never to a refusal.
## Machine-readable Appendix
Parsed by `_validators/legacy.py:extract_contract_block` (spec.yaml status validation)
and `lint-builder-assets.py --check-status-source-of-truth` (status-literal drift
scan across `scripts/` and `prompts/`). Keep this block in exact sync with the
"Status State Machine" list above — it is the same enum, restated in a
machine-parseable form.
```yaml status-enum
- "specifying"
- "specified"
- "spec-reviewed"
- "designed"
- "reviewed"
- "planned"
- "implementing"
- "implemented"
- "adversarially-reviewed"
- "verifying"
- "verified"
- "verified_with_tasks"
- "syncing"
- "synced"
- "archived"
```
