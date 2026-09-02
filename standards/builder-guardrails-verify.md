# Builder — Verification Guard Rails

Phase 6 verifies completed work from host results first.

- Run validator, required file checks, project check, and project test before model-side analysis.
- Treat nonzero validator exit and missing required files as critical host failures.
- Verify task completion evidence for every task in `tasks.yaml`.
- Confirm TDD-required tasks have RED and GREEN evidence.
- Check requirement coverage, design alignment, and decisions compliance.
- Report a PASS or FAIL verdict across the seven verification categories; an uncovered requirement is a follow-up task, never PASS.
- Host `check`/`test` green is judged by exit code only and proves the final state — it does not prove any test ever failed, so trust RED only from host-sourced evidence.
- Auto-fix only lint, formatting, missing imports, unused code, and other unambiguous mechanical issues.
- Create follow-up tasks for missing behavior, test gaps, and incomplete edge cases.
- Halt for security, data safety, constitution violations, or missing RED evidence.
- Bound repeated auto-fix attempts and report persistent failures.

→ Back to standards/builder-standards.md
