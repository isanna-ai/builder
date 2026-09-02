---
name: isanna-builder-reviews
description: How independent review works in isanna-builder and how to choose a spec's reviewer count (0, 1, or 2) by complexity. Use when the user says "how many reviewers", "should this be reviewed", "skip review", "two reviewers", "adversarial review", "reviews: 2", "spec-review", "review before plan", "review after implementation", "how rigorous should the review be", "set the review count", "this is trust-critical / security-sensitive", or asks whether/how a spec gets an independent reviewer. Explains that verify (the host gate) always runs regardless of the count.
---

# isanna-builder — review rigor

Independent review is a **per-spec** decision, not a global switch. A spec declares how many
independent reviewers each review gate gets — `0`, `1`, or `2` — and the pipeline is built for THAT
spec accordingly. **The host gate (`verify`) runs regardless of the count** — reviews catch design and
implementation defects a human reviewer would; the host still runs the tests and gates on exit 0 every
time. Review rigor and host verification are independent axes.

---

## 1. The counts

| `reviews:` | Pipeline for the spec | Use it for |
|---|---|---|
| **0** | `spec → plan → implement → verify` (no review phases) | trivial / mechanical: a typo, a copy tweak, a version bump, a rename with tests already green |
| **1** | adds `spec-review` (before plan) + `adversarial-review` + `review-fix` (after implement) | the normal case: any real feature or bugfix |
| **2** | doubles each review gate: `spec-review` + `spec-review-2`, `adversarial-review` + `adversarial-review-2`, with **complementary lenses** | trust-critical / security-sensitive / cross-cutting, or anything that touches the gates, the resolver, the numerator, auth/RLS, migrations, or money |

Set it in `spec.yaml` (`reviews: 0|1|2`), or at draft time:
`isanna dispatch draft --reviews 2 "…"`. The `isanna` CLI ships with the builder repository
rather than the installer, so the draft-time flag is available from a clone; in an installed
project set `reviews` in `spec.yaml`.
If a spec omits `reviews`, it takes the dispatcher default (`pipeline.reviews.default`, itself derived
from `reviews.enabled` for older configs). **Omitting the field is the safe choice** — it inherits the
default rather than opting a risky spec out of review.

`reviews: 3` (or more) is a hard error at lint, config, draft, and completion — never a silent
downgrade to 2.

---

## 2. How the phases run

- **`spec-review`** runs on the review lane (a lane ≠ the author) *before* planning, reviewing the
  formalized spec (requirements + design) for correctness, contract fit, and untestable acceptance
  criteria. It appends findings to `review-log.yaml`; the plan phase applies them.
- **`adversarial-review`** runs after implementation, hunting real defects in the changed code
  (file:line + concrete fix + severity), appending to `review-log.yaml`. **`review-fix`** then applies
  them. The reviewer does not fix; the fixer does not review.
- **`verify`** (the host gate) always runs last.

**Reviewer ≠ author is enforced by MODEL, not vendor lane.** `model_registry` resolves the review
phases to a different model than the author (reviewer → **gpt-5.6-sol** on codex / claude-opus-4-8),
regardless of which lane authors. Which lane authors is `pipeline.default_lane` (configurable; `isanna
init` generates it as codex). If no independent review lane exists, dispatch RAISES rather than
running the review on the author's own model — the independence is the point, so a configuration
that cannot deliver it is refused instead of downgraded.

---

## 3. What the second reviewer (count 2) actually adds

The `-2` passes are not a re-run or a vote. Each reviews the artifact **fresh** — it does *not* read
the first reviewer's log, so it can't anchor — through a **complementary lens**: where the first pass
covers correctness / logic / contract, the second covers **security, trust and authorization
boundaries, malformed inputs, concurrency and ordering, and failure modes**. `review-fix` then applies
the **union** of both logs.

Two honest properties to know:
- The `-2` phase must write a **distinct** artifact (`review-log-2.yaml`). A count-2 spec that only
  ran one reviewer *fails* the second gate — you cannot claim two reviewers while running one.
- If the dispatcher has only one review lane, the two passes are the **same model with different
  lenses**, and the review self-identifies as such. It does not claim two-model independence it doesn't
  have. Two distinct models is better when a second review lane exists.

---

## 4. Choosing the count (the discipline)

Ask: *if this ships subtly wrong, what breaks?*

- **Nothing user-visible, and tests already prove it** → `0`.
- **A feature or fix a competent reviewer should look over once** → `1` (the default; when unsure, this).
- **A human would insist on a second, security-minded pair of eyes** → `2`. This is the rigor this
  product was itself built with: anything touching the trust boundary (the host gate, the completeness
  numerator, the resolver, identity/RLS, billing, migrations) earns two independent lenses.

The count is visible in The Record, so a spec that ran unreviewed is legible as such — `0` is a
deliberate, auditable downgrade, not a hiding place.
