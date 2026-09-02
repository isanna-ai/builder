# Builder — Review Guard Rails

Phase 3 reviews requirements and design before planning.

- Review canonical YAML artifacts before rendered companions.
- Findings must name the affected requirement, design surface, or decision.
- Distinguish required fixes from suggestions.
- Do not invent implementation tasks before Phase 4.
- Preserve resolved decisions unless the user explicitly reopens them.
- Raise missing acceptance criteria, unclear invariants, and risky architecture choices.
- Run the four review passes: constitution compliance, completeness, architecture coverage, and the adversarial verifiability audit.
- Audit verifiability: every requirement MUST name a declared verify command that exits non-zero when the requirement is unmet.
- Flag any acceptance criterion with no failure-path check, and any verify command that would already pass on the current tree — it proves nothing, since the gate reads exit code only.
- When suggesting changes, state the smallest artifact edit that resolves the issue.

→ Back to standards/builder-standards.md
