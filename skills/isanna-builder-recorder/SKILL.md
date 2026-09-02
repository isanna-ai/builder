---
name: isanna-builder-recorder
description: Read the truth in The Record, the static isanna-builder flight recorder and planner. Use when the user says "the record", "flight recorder", "run record", "what did the agent actually do", "did the host verify it", "the planner", "spec roadmap", "portfolio", "kanban", "blocked", "dependency arrows", "releases", "percent done", "isanna record build", "isanna coverage", "audit the gate record", "isanna model", or "living SSOT". Also use it when someone asks for a dashboard, a web control panel, or a server to run — there is none; The Record is a static site.
---

# isanna-builder recorder — The Record

`isanna record build` emits The Record: a self-contained, read-only static site over `.builder/`
trees. It has **no server** — nothing to start, nothing to leave running, no port. When someone
asks for the flight-recorder web control panel, they mean The Record: build it, then open the
emitted HTML from disk.

## Three surfaces

| Surface | Reads as | What it must make clear |
|---|---|---|
| **Planner** | The spec roadmap / portfolio. | Kanban by status plus dependency-arrow DAG. A right-to-left arrow exposes a more-done spec waiting on a less-done one. **Blocked is the loudest thing.** |
| **Run Record** | One spec's flight recorder. | The exact split between what an agent claimed and what the host verified. |
| **Releases** | Per-release members from the planning layer. | Host-verified `% done`, never agent-estimated progress. See [isanna-builder-roadmap](../isanna-builder-roadmap/SKILL.md). |

## The two-register rule

The Run Record has exactly two visual registers, assigned by **data provenance, never by content**:

1. What the agent claimed.
2. What the host observed.

Keeping those registers impossible to confuse is the product. The host runs verification and only
an exit-0 host observation is a gate verdict. Never report an agent claim as host verification;
`% done` is computed from host-observed events only.

## Adjacent truth tools

| Command | Use |
|---|---|
| `isanna record build` / `record export <spec>` | build the whole Record, or export one spec's Run Record as a single self-contained page. |
| `isanna coverage` | Audit the gate record itself. |
| `isanna model` / `isanna sync` | read/verify the living SSOT of what the system does; `sync` also checks behavioral-SSOT drift. |
| `isanna release` | Manage the Product → Release → Spec planning layer that feeds Releases. |

The Record explains state; it does not dispatch, mutate queue state, or substitute for a host gate.
For autonomous execution, see [isanna-builder-dispatcher](../isanna-builder-dispatcher/SKILL.md).

> **Runtime dir.** Artifacts live in `.builder/`. `runtime_dir()` always resolves that canonical path; legacy runtimes must be moved explicitly with `isanna migrate` while stopped.
