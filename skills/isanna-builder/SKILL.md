---
name: isanna-builder
description: The vocabulary, lifecycle, and CLI of isanna-builder (formerly SpecPilot) — the spec-driven system that drives specs autonomously across repositories (spec→plan→implement→verify), plus THE RECORD, its read-only flight recorder + planner. Use whenever the user says "builder", "the spec roadmap", "the roadmap", "the flight recorder", "the web control panel", "the record", "run record", "the planner", "the portfolio", "the kanban", "wire up a new repo for builder", "isanna init", "draft a spec", "dispatch", "the queue", "the lanes", "is it blocked", "what did the agent actually do", "did the HOST verify it", or asks to see build/spec status in a UI. If the user asks for "the flight recorder web control panel", a dashboard, or a server to run, they mean THE RECORD (`isanna record`) — a static site, not a service.
---

# isanna-builder

The spec-driven build system. Agents author and implement; **the host verifies.** That asymmetry is
the entire product thesis, and every surface below exists to keep it legible.

---

## 1. 🔴 Vocabulary — get this right, it is the most common confusion

| The user says | They mean | Command |
|---|---|---|
| **"builder"**, "builder" | This system. **Builder is also called `isanna-builder`.** | `isanna …` |
| **"the spec roadmap"**, "the roadmap", "the planner", "the portfolio", "the kanban" | **The Planner view of The Record** — the spec portfolio as a kanban by status, with a dependency DAG | `isanna record build` |
| **"the flight recorder"**, "the run record", "what did the agent actually do" | **The Run Record view of The Record** — one spec's run reconstructed, agent claims vs host verdicts | `isanna record build` |
| **"the web control panel"**, "the record" | **The Record.** A static-site generator over `.builder/` trees. | `isanna record` |

### ⚠️ There is no server. The Record is a static site.

Builder has no daemon to query, no API to call, and no web control panel. Roadmap and run history
come from **The Record**: `isanna record build` reads the gate evidence already on disk and emits
self-contained HTML you open from the filesystem.

**When the user asks for "the dashboard" or "the flight recorder web panel", they mean The Record.**
If you find yourself looking for a service to query to answer a roadmap question, you have taken the
wrong turn. Go to `isanna record`.

---

## 2. The Record — three surfaces, one generator

One static-site generator reads the canonical `.builder/` runtime tree and
emits self-contained HTML.

### The Planner — *"the spec roadmap"*
The spec portfolio as a kanban across five status columns: **Authoring · Ready · In flight · Verified
· Archived**. Status is the **x-axis**, so dependency arrows (from `dependencies.yaml`) are
*geometrically* meaningful: an arrow pointing **right-to-left** means a more-done spec is waiting on a
less-done one. That is the critical path, visible without reading anything. (**Blocked is not a column**
— it is a loud overlay on a card wherever it already sits; see below.)

**Blocked is the loudest thing on the page.** `BLOCKED_DEP` → red border + a chip naming the unmet
deps. `BLOCKED_HUMAN` → amber border + the reason from the event record.

> A repo with no `dependencies.yaml` still works — it is just a clean kanban with no arrows.

### The Run Record — *"the flight recorder"*
Reconstructs **one spec's run** so that **agent claims and host verdicts are impossible to confuse.**

**The two-register rule:** the page has exactly two visual registers, assigned by **data provenance**,
never by content. What the agent *said* it did, and what the **host** actually *observed*. Never blur
them. This is the product.

### The Releases surface — *"the roadmap as designed"*
Reads the planning layer (`.builder/releases/` + `product.yaml`) and renders each **release** with
its member specs grouped by state — host-verified / planned / self-reported / unknown (a dangling ref
renders as a loud BROKEN card) — and a
host-verified `% done` that **agents cannot inflate** (the numerator is host gate-coverage; the
denominator is the human-authored release file). A `planned` stub shows as an *intentional* member, so
authoring a roadmap makes its unbuilt specs visible before anyone builds them. See the **roadmap** skill
to author one.

---

## 2.5 Specific skills — go deeper

This is the index. For a task, load the specific skill:

| Skill | Load it when the work is… |
|---|---|
| **isanna-builder-authoring** | writing a spec by hand — the `/isanna-*` flow, spec.yaml, phase artifacts |
| **isanna-builder-dispatcher** | the dispatcher — lanes, the queue, plan-gate, and delivery |
| **isanna-builder-recorder** | reading the truth — `isanna record`, the Planner / Run Record / Releases, coverage, model |
| **isanna-builder-roadmap** | authoring a Product → Release → planned specs, visible in The Record as designed |
| **isanna-builder-reviews** | choosing a spec's reviewer count (0 / 1 / 2) by complexity |
| **isanna-builder-ssot** | the behavioral SSOT (system-behaviors.yaml) — what the system does, anchored to tests; `isanna sync`; bootstrapping it from an existing codebase |

---

## 3. The lifecycle

```
intent ──draft──> spec ──(approve)──> enqueue ──> spec → plan → implement → verify
                                                              │
                                          host runs verify commands, gates on exit 0
                                                              │
                                                    verified → delivered → merged → available
```

- **Lanes.** Which lane authors vs reviews is set by `pipeline.default_lane`, **configurable per repo**.
  The runtime default is `claude` (claude-code-cli) authors/implements + `codex` (codex-cli) reviews —
  but `isanna init`'s generated `dispatch.yaml` sets `default_lane: codex`, so on a freshly-wired
  repo codex authors too — change that key if you want the claude lane authoring. The invariant that actually holds is
  **the review phase runs on a different MODEL than the author** (via `model_registry`: reviewer =
  gpt-5.6-sol / claude-opus-4-8), not a fixed claude-vs-codex split. Without an independent review lane,
  dispatch **fails loudly**: `select_independent_review_lane` raises rather than let a same-family
  review be presented as cross-model review. Configure a second provider, or turn reviews off
  deliberately — what it will not do is quietly review the work with the model that wrote it.
- **`plan_gate: false`** → fully autonomous, spec→verify with no stop. Set `plan_gate: true` (or draft
  with `--plan-gate`) to hold one spec for human plan approval.
- **`deliver.enabled: false`** is the safe default: verified work stays in the working tree for human
  review. **Never flip it on for a repo with a live product without asking.**

---

## 4. Wiring a NEW repo — `isanna init`

```bash
isanna init --target /path/to/repo             # idempotent; --dry-run to preview
```

Creates only what is missing: `.builder/dispatch.yaml` (claude + codex lanes, reviews on, deliver
**off**), `dispatch-queue/`, `specs/`, and a `dependencies.yaml` scaffold.

**🔴 `init` does NOT start anything.** It makes a repo **visible and drivable** — not **autonomous**.
Those are two separate decisions. When a human asks to run dispatch, use the shipped CLI from the
initialized repository and keep the invocation explicit:

```bash
isanna dispatch --once
```

`install.sh` is a **different thing** — it installs the slash-command workflow (`/isanna-1-specify` …).
`init` makes a repo dispatch-capable and Record-visible. A repo generally wants both.

---

## 5. The CLI

These are the REAL `isanna` verbs (`scripts/isanna.py`).

| Command | What it does |
|---|---|
| `isanna init` | wire a new repo (§4) |
| `isanna dispatch [--once]` | from an initialized project's root, run the included single-project dispatcher loop; `--once` runs one cycle and exits |
| `isanna record build` / `record export <spec>` | build **The Record** (roadmap + releases + flight recorder), or export one spec |
| `isanna verify` | run this project's verify commands **host-side** and gate on exit 0 |
| `isanna model build\|verify\|drift` | the living SSOT — what this system still does |
| `isanna sync` | after a spec: refresh the model + fail if the behavioral SSOT drifted (see the ssot skill) |
| `isanna release create\|status\|lint\|ship` | Product → Release → Spec; **% done from host-observed events only** |
| `isanna migrate --dir` | atomically move a **stopped** repo's legacy `.specpilot/` runtime dir to `.builder/` |
| `isanna coverage` | audit the gate record itself |
| `isanna demo` | watch a lying agent get caught |

Use `isanna dispatch [--once]` from the initialized project's root to run the included dispatcher.

---

## 6. Rules that matter

1. **Never confuse an agent claim with a host verdict.** The whole system exists to keep them apart.
   If you are reporting status, say which one you are quoting.
2. **`% done` comes only from host-observed events.** Do not compute completeness from what an agent
   said. Agents inflate; the host does not.
3. **Prefer `git worktree remove <path>` over `git worktree prune`.** Dispatch runs specs in
   worktrees under `.builder/worktrees/`; a prune that fires while one is live deregisters work
   in progress.
4. **The runtime dir is live** (dispatcher config + queue state). Do not casually move, rename, or clean
   it. A `dispatch-queue/` is *state*, not scratch. Its canonical location is `.builder/`.
   Move one **stopped** legacy repo with `isanna migrate --dir` (never a live `mv`).
5. **Dispatch is always an explicit, per-repo human act** (§4).

---

## 7. When the user asks "show me the roadmap"

1. Is the repo wired? (`.builder/dispatch.yaml` present) → if not, `isanna init`.
2. `isanna record build` → the roadmap + releases + flight recorder as self-contained HTML. Open the
   emitted files directly in a browser (The Record is a static site — there is no server to run).

**Do not** look for a server to query — there is none, and there is no `isanna mc serve` (§1). The
Record is the whole read surface.

> **Runtime dir.** Artifacts live in `.builder/`. `runtime_dir()` always resolves that canonical path; a legacy `.specpilot/` runtime must be moved explicitly with `isanna migrate` while stopped.
