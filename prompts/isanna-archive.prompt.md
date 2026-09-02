---
agent: agent
description: "Archive a completed Builder spec after verification and merge readiness checks."
load_set:
  tiny_local:
    - standards/builder-workflow.md
  small_commercial:
    - standards/builder-workflow.md
    - standards/builder-contract.md
  flagship_commercial:
    - standards/builder-workflow.md
    - standards/builder-contract.md
---

# /isanna-archive — Archive A Completed Spec

You archive a completed Builder spec after verification is done and the user is
ready to move it out of the active `.builder/specs/` queue.

This is a post-verify utility command, not a new lifecycle phase.

---

## Target Location

Archive completed specs to:

`.builder/specs/archive/YYYY-MM-DD-<feature>/`

Keep the full spec directory intact. Do not flatten or split files.

---

## Selection Rules

If the user names a spec, use it.

If the user does not name a spec:

1. List directories directly under `.builder/specs/`.
2. Exclude the `archive/` directory.
3. Prefer specs whose `spec.yaml` has `status: verified` or `status: verified_with_tasks`.
4. If multiple candidates exist, show the most relevant options and ask the user which one to archive.
5. Do not guess.

---

## Pre-Archive Checks

Before moving anything:

1. Read `spec.yaml`, `phase-log.yaml`, and (if present) `tasks.yaml` plus rendered `tasks.md`.
2. Confirm the spec has completed verification:
   - preferred: `status: verified`
   - acceptable with warning: `status: verified_with_tasks`
3. If tasks remain incomplete, warn and require explicit confirmation.
4. If verification is not complete, stop and tell the user to finish `/isanna-6-verify` first unless they explicitly want an override.
5. Check whether the archive destination already exists. If it does, stop and ask how to proceed.

Optional but recommended:

6. If the repo is a git repository and the spec appears unmerged, warn that archive is normally done after merge.
   Do not block on this warning unless the user wants to stop.

### Sync gate — a spec must have synced

Run `isanna ssot archive-check --root <repo> --spec <feature>` before moving anything.

- `OK` — the spec has a `sync-result.yaml`; proceed.
- `REFUSED` — **stop**. Do not archive, and do not fabricate a `sync-result.yaml`.
- `WARN` — allowed for now, but report the warning to the user in your output.

Archiving past sync loses the spec's SSOT update permanently: its declared `ssot-delta` is
never reconciled against what actually changed, and nothing downstream ever notices the gap.

The check is staged. `BUILDER_ARCHIVE_REQUIRE_SYNC=warn` is the default because most
builder-wired repos cannot sync at all yet — they are missing `.builder/sync-adapter.yaml`
and/or `docs/system-behaviors.yaml`, so `isanna sync` fails closed with `bootstrap_required`.
Set `BUILDER_ARCHIVE_REQUIRE_SYNC=enforce` per repo once its SSOT is curated.

A `REFUSED` naming the repo (rather than the spec) means the repo was never bootstrapped.
That is a different fix — bootstrap the repo — and it is not something archiving can resolve.

### Release membership

A spec named by a release manifest **is safe to archive**. Release membership resolves a
member at `.builder/specs/<id>` and, failing that, at its archived form
`.builder/specs/archive/YYYY-MM-DD-<id>` — so archiving does not orphan the release.

This was not always true. Membership used to resolve only the live path, so archiving a
release-referenced spec turned it into a dangling ref, and a spec whose own `next_action`
read "Run /isanna-archive <name>" could not have that action performed without breaking
`isanna release lint`. If you are working from older notes that say archiving breaks
releases, they are out of date.

Do not edit a release manifest to remove the member "so the archive is clean". The
manifest is the human-authored denominator; dropping a member silently changes what the
release claimed to ship.

---

## Archive Actions

When the user confirms archive:

1. Create `.builder/specs/archive/` if it does not exist.
2. Move `.builder/specs/<feature>/` to `.builder/specs/archive/YYYY-MM-DD-<feature>/`.
3. Update the archived copy of `spec.yaml`:

```yaml
status: archived
current_phase: archived
next_action: "None"
archived_at: <ISO 8601 timestamp>
```

4. Append an archive entry to `phase-log.yaml` in the archived directory:

```yaml
- phase: archive
  completed: <ISO 8601 timestamp>
   used_model: "<current model name and reasoning profile if known, e.g. GPT-5.4 Xhigh reasoning>"
  outcome: archived
  notes: "Archived after verification completion."
```

5. Preserve all other artifacts exactly as they were.
6. Persist `archive-report.yaml` inside the archived spec directory using the
   `utility-report` schema (`artifact: utility-report`, `command: /isanna-archive`,
   `mode: <archive | warned | overridden>`, `summary`, `next_command`,
   optional `details` such as previous status and tasks-incomplete count).
7. Verify the move did not orphan anything. Run `isanna release lint` and confirm it reports
   every release clean. A `dangling ref '<spec-id>'` finding means membership could not
   resolve the archived spec — report it and STOP. Do not repair it by editing the release
   manifest; that hides a resolution failure by deleting the evidence of it.

   Take this reading BEFORE the move as well as after. A release that was already failing
   lint for unrelated reasons will otherwise look like damage the archive caused.

---

## Output On Success

```text
Spec archived.

Feature: <feature-name>
Archived to: .builder/specs/archive/YYYY-MM-DD-<feature>/
Previous status: <verified status>
Current status: archived
```

If warnings applied, include them after the success summary.

---

## Guard Rails

- Never archive the `archive/` directory itself.
- Never overwrite an existing archive target.
- Never delete spec files during archive.
- Never edit a release manifest to drop a member because it was archived. Membership resolves
  archived specs; a manifest edit changes what the release claimed to ship.
- Do not invent merge state. Warn if uncertain.
- Treat archive as an administrative move that happens after verification, not instead of verification.

## Workflow Telemetry

After durable artifact writes complete and before the final handoff, persist one
`workflow-event` via `.builder/scripts/record-workflow-event.py` or the
helper `write_utility_event` in `.builder/scripts/_telemetry/record.py`.
Record `command`, `used_model`, `thinking_effort`, `capture_source`,
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

| Emoji | Field         | Value                                            |
| ----- | ------------- | ------------------------------------------------ |
| 🧭    | Command       | /isanna-archive                                      |
| 📁    | Archived spec | .builder/specs/archive/YYYY-MM-DD-\<feature\>/ |
| ✅    | Status        | ARCHIVED                                         |
| 🤖    | Used model    | \<model + profile\>                              |
| ▶     | Next command  | none                                             |
| 🧠    | Model advice  | Start a new spec or continue other active work.  |
