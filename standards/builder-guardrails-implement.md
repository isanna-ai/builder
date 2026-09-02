# Builder — Implementation Guard Rails

Phase 5 implements approved tasks only.

- Treat `tasks.yaml` as canonical and `tasks.md` as rendered review output.
- Respect task dependency order and skip blocked tasks.
- For TDD-required tasks, write the failing test first, capture RED, implement, then capture GREEN.
- RED evidence MUST be produced by actually running the focused verify command — captured command, exit code, and timestamp, with `source: host` when host-captured. A fabricated or `source: agent` RED entry for a `tdd_mode: required` task is a guardrail violation. (Full mechanical host-capture of RED is a flagged dispatcher follow-up; the field and this rule apply now.)
- Verify commands are judged by EXIT CODE ONLY — encode success in exit 0 (`! grep` for zero-hit assertions, `cmd | grep -q` for output assertions); comments and stdout are not read by the gate.
- Production-destructive commands (drop/delete/truncate/migrate against real data) are NEVER verify commands; verify a destructive change only against a disposable copy or via the design's `migration_strategy.rollback_verification` commands.
- Run every task `Verify` command fresh; stale output is not evidence.
- Update evidence after each completed task with command, exit code, stdout tail, and files written.
- Do not expand scope without amending the spec through the workflow.
- If implementation contradicts requirements or design, stop and ask whether to amend spec or fix code.
- Keep edits inside the task file list unless the task explicitly authorizes supporting files.
- Use setup-decisions command defaults when present.

→ Back to standards/builder-standards.md
