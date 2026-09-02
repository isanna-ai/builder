# Builder Standards Index

This index routes each phase to the smallest guard-rail packet that still preserves canonical artifact discipline.

| File | Load condition |
| --- | --- |
| `standards/builder-guardrails-implement.md` | Phase 5 loads implementation rules. |
| `standards/builder-guardrails-verify.md` | Phase 6 loads verification rules. |
| `standards/builder-guardrails-review.md` | Phase 3 loads review rules. |

## Cross-Cutting Rules

- Canonical YAML artifacts are source of truth; rendered markdown is a view.
- Dual-write canonical YAML and rendered companions in `dual` mode.
- In `ai_native` mode, write canonical structured artifacts first and render Markdown only for explicit human review or export.
- Drift between YAML and rendered views is a validation failure.
- `traceability.yaml` links requirements, design surfaces, tasks, files, and evidence.
- `tasks.yaml` dependencies must be acyclic and every referenced task id must exist.
- Evidence must name the command, exit code, stdout tail, and files written.
- Telemetry payloads must be redacted before persistence and retained only for the configured window.
- Setup decisions provide default project check/test commands when present.
- New specs use the current contract; markdown-only compatibility is out of scope.

## Constitution Checklist

- Preserve user data and avoid destructive operations unless explicitly requested.
- Keep changes scoped to the active spec and task.
- Run the task Verify commands fresh before marking completion.
- Record unresolved product or architecture questions in `decisions.yaml`.
