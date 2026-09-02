---
name: isanna-builder-roadmap
description: How to author a ROADMAP in isanna-builder — a Product → Release → set of specs — and see it in The Record exactly as it was initially designed, before any of it is built. Use when the user says "plan a roadmap", "author a release", "the next roadmap", "product and release", "what's on the roadmap", "plan the next quarter of specs", "scaffold a release", "isanna release", "create a release", "add planned specs", "show the roadmap as designed", "% done of the release", "is the release shippable", or asks to lay out several upcoming specs as a unit and track their completion. Pairs with the recorder skill (the Releases surface renders this) and the authoring skill (each member becomes a real spec).
---

# isanna-builder — roadmap authoring

A **roadmap** is a `Product → Release → Spec` tree. You author it up front — the release lists the
specs it will contain, some of which do not exist yet — and it becomes visible in The Record **as
designed**, with a `% done` that **only the host can move**. Agents cannot inflate it.

The rule that makes this trustworthy: **the release file is the human-authored denominator; the host
gate-coverage is the numerator.** An agent editing a spec's `status:` cannot change either. The single
self-declared status the layer honors is `planned`, and it can only keep a member *out* of the
numerator — never add to it.

---

## 1. The three files

```
.builder/product.yaml            # one product per repo (name, title, repo aliases)
.builder/releases/<id>.yaml      # a release: title, status, and its member spec ids
.builder/specs/<spec-id>/spec.yaml   # each member; a not-yet-built one is status: planned
```

A release references specs by id (same-repo) or `<alias>/<spec-id>` (cross-repo). A member that the
release lists but that has no spec dir is **dangling** — a broken plan, surfaced loudly, never counted
as done.

---

## 2. Scaffold a roadmap — one command

```bash
isanna release create <release-id> --specs auth-core,billing,webhooks \
    [--title "Ledger v2"] [--product ledger] [--root /path/to/repo]
```

This is idempotent and writes, under `.builder/`:
- `product.yaml` (kept if it already exists),
- `releases/<release-id>.yaml` listing the members,
- a **stub spec dir per member** with `spec.yaml: {id, title, status: planned}`,
- `intents/<release-id>-intent/intent.yaml` — one intent standing for the release. A release
  you create this way is **live**, and live releases are intent-denominated, which is what
  decides how §4 renders its completeness line.

It refuses to overwrite an existing release, refuses to clobber an already-built spec (it prints
`kept existing spec`), and refuses to write through a symlinked path. **Membership lives only in the
release file** — a stub never carries a `release:` back-reference (that boundary is deliberate: a spec
must not be able to enroll itself into a release it isn't part of).

---

## 3. What `planned` means

A `status: planned` stub is an **intentional, not-yet-built** member — the roadmap as designed. It is
distinct from *dangling* (a member with no spec at all, which is an error). `completeness` reports it
in its own segment so it reads as intentional.

Which segments you get depends on the release's **status**, because membership itself does
(`release_membership_field`, `scripts/_builder_project_model/common.py:16`): a live release is
denominated in intents, a historical one in specs.

```
live (draft, active, …)   0/1 intents fulfilled    [fulfilled N · in-flight N · decomposed N · accepted N · blocked N]
historical (shipped, …)   0/3 specs host-verified  [host-verified N · planned N · self-reported N · unknown N]
```

A release you just created in §2 is live, so it renders the FIRST form. The second is what it
renders once it has shipped.

`planned` is resolved (it exists) but **never host-verified** (it counts toward the denominator, never
the numerator). An agent can set `status: planned` on its own spec, but doing so can only *lower* the
spec out of the verified count — it can never raise `% done`. That asymmetry is the safety.

---

## 4. See it, drive it, ship it

```bash
isanna release status -v <id>     # the completeness line and its segments (see above)
isanna release lint               # dangling refs, cross-repo cycles, duplicate products
isanna record build               # the roadmap is now a Record surface (see the recorder skill)
isanna release ship <id>          # HUMAN-ONLY, and refused unless fully host-verified
```

- In **The Record**, the release appears on the **Releases** surface (members grouped by host-verified
  / planned / self-reported / unknown — a dangling ref renders as a loud BROKEN card — with the host
  `% done`), and each planned stub shows in the
  **Planner**'s ready column — so the moment you author the roadmap, the planned specs are visible
  before anyone builds them. That is the "as designed" view.
- **`ship` is always a human act.** The system computes *shippable* (every member host-verified, zero
  dangling); it will not transition a release itself. `shipped` is never something an agent declares.

---

## 5. From planned → built

A planned member becomes real by being authored and driven like any spec (see the **authoring** and
**dispatcher** skills): its `status` advances off `planned` through the pipeline, and when the **host**
verifies it, and only then, it joins the numerator and the release's `% done` moves. Decide each
member's review rigor with the **reviews** skill (trivial → 0, normal → 1, trust-critical → 2).

> **Runtime dir.** Artifacts live in `.builder/`. `runtime_dir()` always resolves that canonical path; legacy runtimes must be moved explicitly with `isanna migrate` while stopped.
