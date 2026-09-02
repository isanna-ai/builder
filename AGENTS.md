# isanna-builder (formerly SpecPilot) — Agent Instructions

## 🔴 Vocabulary — read before answering any "roadmap" or "status" question

**The Record** (`isanna record`) is the read-only **flight recorder + planner**. One static-site
generator over `.builder/` trees, two views:

- **The Planner** — *"the spec roadmap"*. The spec portfolio as a kanban by status. Status is the
  x-axis, so dependency arrows (from `dependencies.yaml`) are geometrically meaningful: an arrow
  pointing right-to-left means a more-done spec is waiting on a less-done one. That is the critical
  path, visible without reading. **Blocked is the loudest thing on the page.**
- **The Run Record** — *"the flight recorder"*. One spec's run reconstructed, under the **two-register
  rule**: the page has exactly two visual registers, assigned by **data provenance, never by content**
  — what the agent *claimed*, and what the **host** *verified*. Keeping those impossible to confuse
  **is the product**.

There is no server and no web control panel. If someone asks for "the dashboard" or "the
control panel", they mean **The Record** — a static site you generate and open from disk.

## Non-negotiables

1. **Never present an agent claim as a host verdict.** The host runs the verify commands and gates on
   exit 0. `% done` is computed **only** from host-observed events. Agents inflate; the host does not.
2. **`.builder/` is live runtime.** `dispatch-queue/` is *state*, not scratch. Do not clean it.
3. **Dispatch is an explicit, per-repo human act.** `isanna init` makes a repo visible and drivable —
   **not** autonomous. Run `isanna dispatch [--once]` only when asked.
4. **`pipeline.deliver.enabled: false`** is the safe default — verified work stays in the working tree.
   Never flip it on for a repo with a live product without asking.
5. **Prefer `git worktree remove <path>` over `git worktree prune`.** Dispatch runs specs in
   worktrees under `.builder/worktrees/`; a prune that runs while one is live deregisters work
   in progress.

## Release Checklist

When tagging a new release, **always** perform these steps in order:

1. **Check the README install URLs.** The documented installs deliberately pin `/main/`, so they
   need no edit. Only the *pinned-release* example carries a version placeholder — keep it as
   `vX.Y.Z` rather than hard-coding a tag that goes stale the next release.

2. **Commit** any release changes.

3. **Tag** with an annotated tag:
   ```
   git tag -a vX.Y.Z -m "vX.Y.Z — <short summary>"
   ```

4. **Push** the commit and tag:
   ```
   git push origin main && git push origin vX.Y.Z
   ```

5. **Verify** the install URL resolves:
   ```
   curl -fsSI https://raw.githubusercontent.com/isanna-ai/builder/vX.Y.Z/install.sh
   ```

## Prompt Inventory

The shipped prompt inventory is declared in `asset-manifest.txt`. The installer
validates the on-disk count against the manifest automatically. When adding or
removing a prompt, update `asset-manifest.txt` — the count check in `install.sh`
is manifest-driven.

## Branching

- Contributors: branch from `main` and open a PR — see `CONTRIBUTING.md`, which is the
  authority here. `make gate` must be green before review.
- Tag releases with semver: `v0.1.0`, `v0.2.0`, etc.

## File Layout

```
prompts/          isanna-* prompts declared in asset-manifest.txt + handoff template
standards/        builder-standards.md, builder-tdd.md, builder-workflow.md, builder-contract.md,
                  builder-guardrails-{implement,review,verify}.md
templates/        spec.yaml, constitution.md
skills/planning/  SKILL.md (installed for every --ai target)
skills/           the shipped agent skills; skills/builder/ adds agents metadata for Codex
install.sh        Bootstrap/update installer
```

## Decision Memory

Optional and off by default. With a memory provider configured, the dispatcher recalls prior
`decision`/`learned` memories at plan time and writes new ones at verify time. The
token-efficiency layer (distill-at-write, budget/relevance gate, dedup, optional pull-mode) is
flag-gated and documented in **`docs/decision-memory.md`** — read it before touching
`_dispatch_runtime/memory_hook.py`, `phase_runtime.py`'s `build_phase_goal`, or
`lane_claude_code_cli.py`'s pull path.
