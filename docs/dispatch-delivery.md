# Dispatch delivery: verified → PR → CI-green merge → protected production

The Builder dispatcher can deliver a verified spec as a pull request and let the
repo's own CI be the acceptance gate, with a **manual production gate** after merge.
Delivery is **off by default** — enable it per project, and only
after the GitHub-side protections below are in place.

## 1. Enable delivery for a project

In that project's `.builder/dispatch.yaml`:

```yaml
pipeline:
  deliver:
    enabled: true
    base: main            # optional; defaults to the repo's origin/HEAD
    branch_prefix: "builder/"   # optional
    squash: true          # squash-merge (default) vs merge commit
    auto_merge: true      # arm GitHub native auto-merge (merges when CI is green)
```

When `6-verify` succeeds the daemon runs, in the project repo:

```
git checkout -B builder/<spec>  →  git add -A  →  git commit  →  git push -u origin …
gh pr create --base <base> --head builder/<spec>
gh pr merge --auto --squash         # GitHub merges automatically once CI passes
```

It then notifies `pr_opened` and advances to archive. If any step fails it notifies
`blocked_human` and does **not** archive — a human resolves it. By default a notification is a
compact packet written to a file under the queue, which works anywhere; a Telegram block in
`pipeline.notify` is opt-in per repo.

**Prereqs:** `gh` authenticated in the dispatch environment; the project is a Git repo with an
`origin` remote and a CI workflow.

## 2. Make CI-green the real gate (branch protection)

So `gh pr merge --auto` only merges on green, protect the base branch:

- Settings → Branches → add a rule for `main`:
  - **Require status checks to pass before merging** → select your CI workflow's checks.
  - **Require a pull request before merging** (the dispatcher opens one).
  - Enable **Allow auto-merge** in repo Settings → General.

Without required checks, auto-merge would merge immediately — defeating the gate.

## 3. The protected `production` gate (manual approval before prod)

The merge to `main` is automatic on green; **shipping to prod is not**. Put the prod
deploy behind a protected GitHub **Environment**:

- Settings → Environments → **New environment** `production`:
  - **Required reviewers** → add yourself / the team. Deploys to this environment
    pause until a reviewer approves.
  - (optional) wait timer, deployment branch rule = `main` only.

Then the deploy job references it (`.github/workflows/deploy.yml`):

```yaml
name: deploy
on:
  push:
    branches: [main]
jobs:
  deploy-production:
    runs-on: ubuntu-latest
    environment: production      # <-- requires manual approval before this job runs
    steps:
      - uses: actions/checkout@v4
      - run: ./scripts/deploy.sh   # your real deploy
```

Flow end-to-end: dispatcher opens PR → CI runs → **CI green ⇒ auto-merge to main**
(no human) → deploy workflow triggers → **`production` environment pauses for your
approval** → approve ⇒ prod deploy. That's "auto-merge when CI green, with a
protected production environment requiring manual approval."

## 4. Turn it on safely

1. Wire steps 2–3 on the target repo first.
2. Set `pipeline.deliver.enabled: true` in that project's `dispatch.yaml`.
3. Draft a trivial spec, approve the plan, and watch the PR open + auto-merge arm.
   Keep `production` required-reviewers on so nothing ships without you.
