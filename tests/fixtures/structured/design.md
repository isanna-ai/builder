# Structured Artifact Modernization — Design

## Responsibility Allocation

| Surface | Keep | Change | Why |
| --- | --- | --- | --- |
| prompts/isanna-4-plan.prompt.md | Phase sequence and approval gate | Write tasks.yaml and render tasks.md | Planning data should be canonical and machine-readable. |

## Core Changes

### Dual-write planning flow

Planning writes tasks.yaml first and renders tasks.md from the same source.

## Telemetry Strategy

- Use canonical artifacts as the operational signal instead of prose summaries.

## Verification Strategy

```sh
bash /path/to/project/tests/test_structured_artifacts.sh
```
