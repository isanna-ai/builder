---
agent: agent
description: "Detect and repair Builder artifact drift."
load_set:
  tiny_local:
    - standards/builder-workflow.md
  small_commercial:
    - standards/builder-workflow.md
    - standards/builder-contract.md
  flagship_commercial:
    - standards/builder-workflow.md
    - standards/builder-contract.md
    - prompts/isanna-help.prompt.md
---

# /isanna-sync

Check canonical YAML artifacts against rendered companions and report drift.

Use `standards/builder-workflow.md` for canonical artifact flow and `prompts/isanna-help.prompt.md` for command replacements. Do not restate capability classes or handoff format here.

## Sync Steps

1. Run `python3 .builder/scripts/validate-spec.py <spec> --root .` when available.
2. Re-render canonical artifacts with `.builder/scripts/render-spec-artifacts.py`.
3. Compare rendered output with checked-in companions.
4. Write or update `sync-report.yaml` with drift category, affected files, commands run, and recommended fix.
5. If drift is mechanical, update YAML and rendered view together.

## Output

Report PASS when no drift is found. Report FAIL with exact files and commands when drift remains.
