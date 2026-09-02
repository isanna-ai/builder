---
name: isanna-builder-dispatcher
description: Operate or explain isanna-builder autonomous dispatch. Use when the user says "dispatch this spec", "start the daemon", "dispatcher", "queue", "dispatch-queue", "lane", "claude lane", "codex lane", "default_lane", "plan gate", "approve the plan", "deliver.enabled", "autonomous execution", "isanna init", "watchdog", "enqueue", "dispatch status", or asks how reviews stay independent from authors.
---

# isanna-builder dispatcher

The dispatcher is the autonomous executor. It is separate from manual `/isanna-*` authoring; see
[isanna-builder-authoring](../isanna-builder-authoring/SKILL.md) for the hand-driven flow.

## Pipeline and lanes

The normal pipeline is `spec → plan → implement → verify`. When a spec has `reviews: 1` or
`reviews: 2` and pipeline reviews are enabled, it also runs independent review phases before
planning and after implementation, followed by an author-lane `review-fix` before final verify.

| Lane | Role |
|---|---|
| `claude` (`claude-code-cli`) | The runtime-default author/implement lane. |
| `codex` (`codex-cli`) | The runtime-default review lane. |

| `default_lane` | The configured lane used unless a dispatch explicitly chooses another. |

Which lane authors vs reviews is `pipeline.default_lane`, **configurable per repo**. The runtime
default is claude-authors / codex-reviews, but **`isanna init`'s generated `dispatch.yaml` sets
`default_lane: codex`**, so a freshly-wired repo has codex authoring too. Change that key to pick
the authoring lane you want.

The real invariant is **the review phase runs on a different MODEL than the author** (via
`model_registry`: reviewer → gpt-5.6-sol / claude-opus-4-8), not a fixed claude-vs-codex vendor split.
Without an independent review lane, dispatch **fails loudly** — `select_independent_review_lane`
raises rather than silently presenting a same-family review as cross-model review. Configure a
second provider, or disable reviews deliberately.

## Queue and control plane

`.builder/dispatch-queue/` is live **state**, not scratch. Do not clean, move, or recreate it
casually. From an initialized project's root, `isanna dispatch [--once]` runs the included
single-project dispatcher loop (`--once` runs one cycle and exits).

`plan_gate: true` holds a spec after planning for human approval; `approve <spec>` releases it to
the next phase. The default is no hold. `pipeline.deliver.enabled: false` is the safe default:
verified work stays in the working tree for human review. Do not enable delivery for a live product
without an explicit human request.

## Init is not autonomy

`isanna init` wires a repo for dispatch and The Record: configuration, specs, queue directory, and
dependency scaffold. It does **not** install the slash commands and does **not** start a daemon.
It makes a repository visible and drivable, not autonomous.

When a human requests dispatch, run the shipped CLI explicitly from that repository:

```sh
isanna dispatch --once
```

Do not run dispatch merely because a repo has `.builder/dispatch.yaml`.

## Truth boundary

Agents may write plans, code, and evidence. The host runs verify commands and gates on exit 0.
Never turn an agent completion claim into a host verdict; that separation is what The Record
renders. See [isanna-builder-recorder](../isanna-builder-recorder/SKILL.md).
