---
name: isanna-builder-ssot
description: How to author, maintain, and BOOTSTRAP the behavioral SSOT of isanna-builder — docs/system-behaviors.yaml, the test-anchored index of everything the system does. Use when the user says "system-behaviors", "the SSOT", "document what the system does", "what does this system do", "capture current behavior", "bootstrap the behaviors", "behavioral spec", "rebuild from the spec", "isanna sync", "the drift check", "behaviors drift", "add a behavior to the SSOT", or asks how to write down every functionality so you could rebuild the code from it. Also covers documenting an EXISTING codebase's behavior from its code + tests when adopting the builder on a new repo.
---

# isanna-builder — the behavioral SSOT

`docs/system-behaviors.yaml` is the single source of truth for **what the builder does** — a
human-readable index of every load-bearing behavior. Its defining property: **it cannot lie.** Every
entry names a `guarding_tests` set, and the drift check (`scripts/_validators/behaviors.py`, run in
`make gate`) FAILS the build if any named test does not exist or is not run by the gate.

This is the thesis applied to documentation. The product's whole claim is *the host verifies, not the
agent* — prose drifts, the host does not. So here too: **the test enforces, the prose only describes.**
A behavior documented with no live, gated test is a claim the host cannot verify — the exact lie this
system refuses. If you rebuilt the code from scratch, you'd make these tests pass: the tests are the
executable spec, this file is the map.

---

## 1. The schema

```yaml
schema: system-behaviors/v1
behaviors:
  - id: numerator-host-verified-only        # unique, kebab-case
    area: thesis                            # thesis | record | planning | review | readiness |
                                            # runtime | migrate | instrumentation | host-gate | cli
    behavior: "% done counts ONLY specs the host stamped host-verified."   # what it does, one sentence
    invariant: "An agent that sets status:verified moves the numerator by exactly zero."  # what must hold
    guarding_tests:                         # PATH::test_name — each MUST exist and be in `make gate`
      - tests/unit/test_record_releases.py::test_host_verified_member_counts_in_the_numerator_via_the_real_scan
    breaks_when: "planning._spec_verification matches the wrong scan-row key."  # the mutation the test must catch
```

`breaks_when` is not decoration: it records the exact mutation each guarding test must turn red on. It
is how you PROVE the entry is real, not a tautology (see §3).

---

## 2. Keep it honest — `isanna sync`

Run after every spec finishes:

```bash
isanna sync            # rebuilds the spec-derived capability model, then checks behavioral drift
```

- Rebuilds `system-model.yaml` (the *spec-derived* capabilities — one per spec, with their
  host-verified checks) via `isanna model`.
- Runs the drift check on the *curated* `system-behaviors.yaml`. **Exit 1 on drift** (a documented
  behavior whose guarding test is missing or ungated); a missing SSOT is a soft note (bootstrap it).

The drift check also runs unconditionally in `make gate` (via `tests/unit/test_system_behaviors.py`),
so a refactor that renames or deletes a guarding test out from under the SSOT fails CI.

Two layers, one truth: **isanna model** = spec-derived (auto-generated per spec); **system-behaviors.yaml**
= curated (the trust invariants and behaviors that were NOT authored as specs — most of them).

---

## 3. Bootstrapping the SSOT from an existing codebase

When a repo adopts the builder, capture what it *already does* — reverse-engineer the SSOT from code +
tests. **The test suite is the primary source: a test already encodes a behavior.** The method (this is
exactly how this repo's own SSOT was seeded):

1. **List the load-bearing behaviors, trust-critical first.** Not every test is an SSOT behavior. Ask of
   each area: *if this broke silently, would the product lie, lose data, or launder an unearned pass?*
   Those invariants are the ones worth documenting + the mutation discipline. Start there, then breadth.
   Sources: the test names/assertions, the README/design docs, the CLI surface, the security/traversal
   guards.
2. **For each behavior, find its guarding test(s)** — the test that would go red if the behavior broke.
   Group related tests under one behavior.
3. **Write the entry**: `behavior` (the sentence), `invariant` (what must hold), `guarding_tests`,
   `breaks_when` (the mutation).
4. **Mutation-verify every entry — this is what separates an SSOT from a test index.** For each behavior,
   apply `breaks_when` to the enforcing code, run the named test, and CONFIRM IT GOES RED; then revert.
   If the test does not go red, the behavior is NOT actually guarded — strengthen the test (or write one)
   until it catches the break. A behavior whose test can't fail is drift waiting to happen.
5. **Validate**: `isanna sync` (or run the drift check). Every entry must resolve to a real, gated test.
   Fix any ref that does not; never delete a behavior to make the check pass — correct the reference or
   add the missing test.
6. **Do not fabricate coverage.** If a real behavior has no test, say so and write the test first — do
   NOT document it as guarded. The SSOT's value is that it is trustworthy.

> A large mutation audit (dozens of invariants, each flipped and confirmed red) is the honest way to
> seed the SSOT for a mature codebase — parallel reviewers per area, each producing entries + the
> mutation that proves them. See the `isanna-builder-reviews` discipline for the rigor ladder.

---

## 4. When you add or change a behavior

- **New load-bearing behavior** → add an entry with a guarding test, mutation-verified. `isanna sync`
  must stay green.
- **You changed a behavior** → update the `behavior`/`invariant`/`breaks_when` AND the guarding test.
- **You deleted a behavior** → remove its entry (and, if truly dead, its test).
- **You renamed a test** → update every `guarding_tests` ref (the drift check will point you at breaks).

The one rule: **the SSOT may never claim a behavior the host cannot verify.** Everything else follows.
