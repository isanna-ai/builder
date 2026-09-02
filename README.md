# isanna-builder

[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-green.svg)](LICENSE) ![Works with](https://img.shields.io/badge/works%20with-Copilot%20Chat%20%C2%B7%20Claude%20Code%20%C2%B7%20Codex-blue)

**Stop your AI coding agent from writing code before you've agreed on what you want.**

isanna-builder installs a slash-command workflow into your repo — **specify → design → review → plan → implement → verify** — and stops for your approval at each authoring phase. No code gets written until the requirements and a plan exist. Works with **VS Code + GitHub Copilot Chat**, **Claude Code**, and **Codex**.

## See it work first — one command, offline

The whole argument is one command. It runs in under a second. No API key, no network, no install:

```sh
git clone https://github.com/isanna-ai/builder.git && cd builder && make demo
```

An agent reports `DECISION: SUCCEEDED`. The host runs the tests anyway, finds nothing was
implemented, and **rejects the phase**. Then the agent does the work, the same commands run, and
the stamp is issued. Same agent, same claim, different verdict — because something other than the
agent ran the tests.

**Jump to:** [Quick start](#quick-start) · [How it works](#how-it-works) · [Why isanna-builder](#why-isanna-builder) · [Commands](#commands) · [The Record](#the-record) · [Troubleshooting](#troubleshooting) · [Reference](#reference) · [Also in this repo](#also-in-this-repo)

## Quick start

**Prerequisites:** a git repo (or a `.code-workspace`), an AI coding agent (Copilot Chat, Claude Code, or Codex), and a POSIX shell. Python 3.11+ is optional — it unlocks the [deterministic validator](#deterministic-validator).

> **Platforms.** CI runs the full suite on **Linux**, and installs into a scratch repo on
> **macOS** (bash 3.2, BSD userland) on every change — see `.github/workflows/gate.yml`. The test
> suite itself is Linux-only by design: ~40 cases read `/proc`. **WSL** is Linux and should behave
> identically, though it has no job. **Git Bash on Windows** is untested — if you try it, an issue
> reporting what happened is welcome.

### 1. Install

Run this from the root of the project you want to add isanna-builder to:

```sh
curl -fsSL https://raw.githubusercontent.com/isanna-ai/builder/main/install.sh | bash -s -- --yes
```

The installer detects whether the target is a repo or a workspace, installs the prompts and project files, and prints your next steps.

Already have isanna-builder cloned? Install it into your project directly from the clone:
> ```sh
> bash /path/to/isanna-builder/install.sh --target /path/to/your/project --yes
> ```

Cautious? Drop `--yes` to review the full file plan and confirm before anything is written, or add `--dry-run` to only print the plan. Using **Claude Code** or **Codex**? See [Other install options](#other-install-options) below.

### 2. Configure your project

Open your agent's chat and run:

```text
/isanna-setup
```

`/isanna-setup` inspects your repo, proposes candidate test/check commands and edit boundaries, asks only the questions it can't answer itself, and records your choices in `.builder/setup-decisions.yaml`. Later phases read that file for project commands and boundaries. (Reload VS Code first, or restart your agent, so it discovers the new commands.)

### 3. Set your project's rules (optional, 2 minutes)

The installer creates `.builder/constitution.md` from a template, and `/isanna-setup` fills it
in with you. Either way it is **your** file, not isanna-builder's. It holds
the rules and invariants every phase must respect: what must never regress, which directories are
off limits, the conventions a reviewer should enforce. It is the file that makes the workflow
yours rather than generic, it is preserved verbatim on every reinstall, and it is worth the two
minutes now because every later phase reads it.

### 4. Start your first spec

```text
/isanna-1-specify add dark mode support
```

isanna-builder gathers a structured system model and requirements, then **stops for your approval** before it designs anything. Approve each phase (`/isanna-2-design` … `/isanna-6-verify`) at your own pace — or run `/isanna-ff <description>` to fast-forward the whole lifecycle in one session when you accept unattended progression.

<a id="other-install-options"></a>
<details>
<summary><strong>Other install options</strong> — Claude Code, Codex, pinned release, another repo, proxy-blocked, inspect-first</summary>

<br>

**Claude Code** (prompts go to `.claude/commands/`):

```sh
curl -fsSL https://raw.githubusercontent.com/isanna-ai/builder/main/install.sh | bash -s -- --ai claude --yes
```

**Codex** (installs a global skill at `${CODEX_HOME:-~/.codex}/skills/builder/`):

```sh
curl -fsSL https://raw.githubusercontent.com/isanna-ai/builder/main/install.sh | bash -s -- --ai codex --yes
```

**Into a specific project** (from anywhere):

```sh
curl -fsSL https://raw.githubusercontent.com/isanna-ai/builder/main/install.sh | bash -s -- --target /path/to/repo --yes
```

**Pinned to a release** (reproducible — pin *both* the URL and `--builder-ref` to the same tag):

```sh
curl -fsSL https://raw.githubusercontent.com/isanna-ai/builder/vX.Y.Z/install.sh | bash -s -- --builder-ref vX.Y.Z --yes
```

**`curl | bash` blocked / corporate proxy** — use the text-only standalone installer (the `.txt` extension slips past proxies that block `.sh`):

```sh
curl -fLO https://raw.githubusercontent.com/isanna-ai/builder/main/standalone-installer.sh.txt
sh -n standalone-installer.sh.txt          # syntax-check first
sh standalone-installer.sh.txt --yes       # supports --target, --ai, --codex-home, --dry-run, and --yes
```

See [Standalone installer](#standalone-installer) for the embedded-digest check, update behavior, and environment variables.

**Inspect before running:**

```sh
curl -fsSL https://raw.githubusercontent.com/isanna-ai/builder/main/install.sh -o isanna-builder-install.sh
less isanna-builder-install.sh
bash isanna-builder-install.sh --help
```

Every remote command above assumes the repository is publicly reachable. To install from a clone instead, use `bash install.sh --target …`.

</details>

## How it works

```
You:     /isanna-1-specify add dark mode support
Agent:   Walks you through the system model and requirements using structured prompts,
         then STOPS for your approval.
         → .builder/specs/add-dark-mode-support/system-model.yaml
         → .builder/specs/add-dark-mode-support/requirements.yaml + requirements.md

You:     /isanna-2-design            → design.yaml + design.md
You:     /isanna-3-review            → review-log.yaml + review-log.md  (gaps, risks, missing tests)
You:     /isanna-4-plan             → tasks.yaml + tasks.md  (atomic, TDD-enforced)
You:     /isanna-5-implement        → code, test by test — RED first, then GREEN, then verify
You:     /isanna-6-verify           → post-implementation verification against the spec
```

Each phase has one job, one output, and one handoff. **You control the pace** — nothing advances past a phase boundary without your approval. Pre-spec exploration needs no command: open a chat, scope the problem, then run `/isanna-1-specify`.

<details>
<summary>Full lifecycle, resuming, and iteration</summary>

<br>

```
/isanna-1-specify <description>   → system-model.yaml + requirements.yaml + requirements.md
/isanna-2-design                  → design.yaml + design.md
/isanna-3-review                  → review-log.yaml + review-log.md
/isanna-4-plan                    → tasks.yaml + tasks.md
/isanna-5-implement               → code, test by test
/isanna-6-verify                  → post-implementation verification
/isanna-archive                   → moves the completed spec to .builder/specs/archive/
```

**Passing a spec name:** start with `/isanna-1-specify add dark mode support` — the agent derives a kebab-case directory name. In a fresh session, pass that name to any phase command to resume (e.g. `/isanna-5-implement add-dark-mode-support`). If in-progress specs exist, the agent offers them for selection.

**Approval gates use `Another pass`** for iteration — clarify, polish, reframe, or ask for fresh eyes. It does not mean the artifact is wrong; it means "give it another pass." When you choose it, the agent recommends whether to keep the same model (local, additive changes) or switch (fresh perspective, challenged assumptions, stuck thread).

New specs use the canonical YAML artifact contract immediately; markdown-only specs are out of scope rather than supported through a compatibility mode.

</details>

## Why isanna-builder

- **No code before agreement.** Every authoring phase (specify → design → review → plan) ends with an explicit approval gate; implement and verify run on once you approve the plan. The agent does not jump ahead of you.
- **Explicit handoffs between phases.** Each slash command has one job, one output, one handoff format — no context drift between steps.
- **Canonical YAML, readable Markdown.** Structured spec data lives in YAML; review Markdown is rendered from that source of truth, and drift between the two is a hard failure.
- **Mechanically-enforced TDD.** An optional Python validator checks task schema and RED/GREEN evidence, so test-first is a gate, not just prose in a prompt.
- **Forward-only telemetry.** isanna-builder can record model, reasoning effort, outcome, and runtime-measured compute per spec — without asking agents to estimate their own token use.
- **Project-owned policy, shared workflow.** isanna-builder ships reusable prompts; your constitution, architecture rules, and planning overrides stay yours and survive updates.

See [Trust model](#trust-model) for exactly what the gates do and don't guarantee — or run `make demo` from a clone to watch a gate catch a lying agent.

## Commands

**Slash commands** ship as prompt files installed into your agent's prompt directory. **CLI utilities** have no prompt file and no slash command — they are Python scripts the installer places under `.builder/scripts/`, run directly with `python3`.

| Command | Kind | What it does |
|---------|------|-------------|
| `/isanna-1-specify <description>` | Slash command | Gather a structured system model and requirements into a spec |
| `/isanna-2-design` | Slash command | Produce a technical design from the requirements |
| `/isanna-3-review` | Slash command | Review requirements and design against constitution and standards |
| `/isanna-4-plan` | Slash command | Break the design into ordered, atomic, TDD-enforced tasks |
| `/isanna-5-implement` | Slash command | Execute tasks with TDD discipline |
| `/isanna-6-verify` | Slash command | Post-implementation verification against the spec |
| `/isanna-ff` | Slash command | Fast-forward specify → verify in one session (explicitly approves unattended progression) |
| `/isanna-setup` | Slash command | Configure local project rules and boundaries |
| `/isanna-sync` | Slash command | Detect drift between spec artifacts and implementation; can backfill specs from unspecced code |
| `/isanna-archive` | Slash command | Move a completed, verified spec to the archive |
| `/isanna-debug` | Slash command | Structured debugging workflow with root-cause tracing |
| `/isanna-help` | Slash command | Show the workflow reference and model guidance |
| `python3 .builder/scripts/list-specs.py` | Python CLI | List all specs — active and archived, with each one's phase |
| `python3 .builder/scripts/validate-spec.py <feature>` | Python CLI | Run the validator against a spec's tasks and `phase-log.yaml` |
| `python3 .builder/scripts/analyze-workflow-telemetry.py` | Python CLI | Aggregate forward-only workflow telemetry into a cross-spec report |

## The Record

**The Record** is a read-only, static flight recorder and planner for your specs. It emits a static site from gate evidence already on disk — no server, no token, nothing to run or leave running. Open the generated `index.html` to see spec status, dependency arrows, release completeness, and what the host actually verified for each spec, as opposed to what an agent merely claimed.

> **The Record ships with the repository, not with the installer.** It is part of the `isanna`
> CLI, which `install.sh` deliberately does not install — that CLI reaches across a whole
> portfolio of repos and is not packaged for external use yet (see
> [Also in this repo](#also-in-this-repo)). To use it, clone this repository and run it against
> your project:

```sh
git clone https://github.com/isanna-ai/builder.git
ln -s "$PWD/builder/bin/isanna" ~/.local/bin/isanna   # anywhere on your PATH
isanna record build --root /path/to/your/project
```

## Updating

Re-run the installer. If you installed with `curl | bash` you have no local copy — re-run it the same way, from the repo root you want to update:

```sh
curl -fsSL https://raw.githubusercontent.com/isanna-ai/builder/main/install.sh | bash -s -- --yes
```

From a clone, point it at the target instead:

```sh
bash install.sh --target /path/to/repo-or-workspace --yes
```

Core prompts and standards are refreshed; your constitution and project-owned files are preserved. Re-running is safe and idempotent — it never touches existing specs under `.builder/specs/`. If you're upgrading from a version that used `.oak/`, the installer migrates it to `.builder/` automatically.

## Troubleshooting

| Symptom | What it means / fix |
|---------|---------------------|
| `[NOTE] python3 not found on PATH` during install | Not fatal — the workflow runs, but nothing is mechanically validated. Install Python 3.11+ anytime; no reinstall needed, the validator files are already in place. |
| `/isanna-*` commands don't appear in your agent | Prompts went to a different target's directory. Re-run with the right `--ai` (`claude` → `.claude/commands/`, `codex` → `~/.codex/skills/builder/`; default Copilot → `.github/prompts/`), then **reload/restart the agent**. Re-running is idempotent and preserves your constitution. |
| `curl \| bash` is blocked by a proxy | Use `standalone-installer.sh.txt` — download it, `sh -n` to syntax-check, then run with its supported flags. If raw GitHub is also blocked, grab the same `.txt` from a tagged Release or an approved channel. |
| `ERROR: … must contain .git/ or a .code-workspace` | Run `git init` first, or point `--target` at a repo/workspace root. This guard is intentional — isanna-builder installs into projects, not home directories. |

Run `bash install.sh --help` for the full flag reference.

## Reference

<details>
<summary><strong>What gets installed</strong></summary>

<br>

| Location | Contents | Owned by |
|----------|----------|----------|
| `.github/prompts/isanna-*.prompt.md` + `builder-handoff-template.prompt.md` | Slash-command prompts (count declared in `asset-manifest.txt`) and the phase-handoff template | isanna-builder (refreshed on reinstall) |
| `.builder/builder-standards.md` | Implementation, review, and verification rules | isanna-builder |
| `.builder/builder-tdd.md` | Test-first discipline | isanna-builder |
| `.builder/builder-workflow.md` | Shared workflow rules (handoff, question placement, model classes) | isanna-builder |
| `.builder/builder-contract.md` | Authoritative artifact contract: state machine, schemas, validation policy | isanna-builder |
| `.builder/scripts/*.py` + `_validators/`, `_telemetry/`, `_constitution/`, `_dispatch_runtime/`, `_sync/` | Optional validator, YAML→Markdown renderer, telemetry recorder/aggregator, and the submodules they import | isanna-builder |
| `.builder/schemas/*.schema.yaml` | Schemas for canonical artifacts and telemetry events | isanna-builder |
| `.builder/templates/*.yaml` | Starter manifests (spec, requirements, design, tasks, handoff, setup-decisions) | isanna-builder |
| `.builder/install-state.json` | What this install wrote, so a reinstall knows what it owns | isanna-builder |
| `.builder/skills/planning/SKILL.md` | Planning skill, resolved by phase prompts | isanna-builder |
| `.builder/prompts/isanna-help.prompt.md` | The help prompt as a runtime asset — phases load it by path, separately from the slash command | isanna-builder |
| **Agent skills**, outside `.builder/` | Seven `isanna-builder-*` skills, into `.claude/skills/`, `.github/skills/`, or `$CODEX_HOME/skills/` depending on `--ai` | isanna-builder (refreshed on reinstall) |
| `.builder/constitution.md` | Project-specific rules and invariants | **Your project** (preserved on update) |

Prompts refer to these as `{{BUILDER_ROOT}}/...`, which means the `.builder/` directory at your
project root. It stays a token because the prompts themselves install to a different place for
each agent, so no single relative path would work everywhere.

Prompt destination depends on `--ai`: `.github/prompts/` (copilot, default), `.claude/commands/` (claude), or `${CODEX_HOME:-~/.codex}/skills/builder/` (codex); the agent skills follow the same choice. The codex layout also carries its own `standards/` and `agents/openai.yaml` under that skill directory, because Codex resolves them from there rather than from `.builder/`. Everything isanna-builder owns is refreshed on reinstall, and everything your project owns — the constitution, your architecture rules, your planning overrides, and every spec under `.builder/specs/` — is preserved.

</details>

<details>
<summary><strong>Canonical spec artifacts</strong></summary>

<br>

New specs use a **dual-write** contract by default: structured YAML is the source of truth, and human-readable Markdown is rendered from it. Rendered Markdown that doesn't match its YAML is a hard failure. A spec can instead be set to **`ai_native`**, which omits the Markdown companions entirely — the YAML is then the only artifact, and there is nothing to drift. The setting is `artifact_mode` in `.builder/setup-decisions.yaml` (`dual` or `ai_native`).

| Artifact | Purpose |
|----------|---------|
| `system-model.yaml` | Entities, capabilities, actors, events, boundaries, rules, behaviors. Required for every new spec. |
| `requirements.yaml` + `.md` | EARS-style acceptance criteria per requirement. |
| `design.yaml` + `.md` | Responsibility allocation, core changes, telemetry strategy, verification strategy. |
| `tasks.yaml` + `.md` | Atomic, TDD-enforced task list with RED/GREEN evidence. |
| `dependencies.yaml` | Cross-spec dependency edges; unknown, duplicate, and self edges fail validation. |
| `traceability.yaml` | Requirement → design → task → evidence trace. |
| `review-log.yaml` + `.md` | Constitution, completeness, architecture, and adversarial review passes. |
| `setup-decisions.yaml` | Versioned project config: repo roots, import aliases, owned/generated paths, command map, off-limits areas. |
| `phase-log.yaml` | Forward-only record of each phase: model, outcome, evidence. |
| `handoff.yaml` | Phase-boundary handoff, including the next command. |

</details>

<a id="deterministic-validator"></a>
<details>
<summary><strong>Deterministic validator</strong></summary>

<br>

An optional Python script at `.builder/scripts/validate-spec.py` mechanically validates task schema, TDD mode, RED/GREEN `exit_code` evidence, `used_model` on every phase, dependency edges, and cross-references across `evidence/task-<id>.yaml`, `handoff.yaml`, `review-log.yaml`, and `traceability.yaml`.

| Aspect | Detail |
|--------|--------|
| Runtime | Python 3.11+ standard library. No `pip install` required to USE it — the installed validator carries its own YAML fallback. (Working on isanna-builder itself is different: its test suite wants PyYAML — see CONTRIBUTING.) |
| Invocation | `python3 .builder/scripts/validate-spec.py <feature>` |
| Automatic | `/isanna-6-verify` runs it when available; a non-zero exit halts the phase. |
| Fallback | Without Python the phase prompts fall back to reading the artifacts themselves. Nothing is mechanically checked, so treat the result as unverified. |

When present, the validator is the **authority** for task schema and evidence discipline.

</details>

<details>
<summary><strong>Workflow telemetry</strong></summary>

<br>

isanna-builder can record forward-only workflow events per command run — model, reasoning effort, phase, outcome, and runtime-measured tokens and latency — under `.builder/telemetry/events/`. `python3 .builder/scripts/analyze-workflow-telemetry.py` aggregates them into `.builder/telemetry/reports/telemetry-report.yaml`. Capture is opt-in per repo and never asks the agent to estimate its own token use.

</details>

<details>
<a id="trust-model"></a>
<summary><strong>Trust model — what the gates do and don't guarantee</strong></summary>

<br>

Be clear-eyed: the gates make progress legible and auditable, but they don't make an agent correct.

- Phases 1–4 (specify, design, review, plan) are approval-gated; Phases 5–6 (implement, verify) run autonomously once you approve the plan.
- `/isanna-ff` treats its own invocation as approval across the ordinary phase gates — use it only when you accept unattended progression.
- The host-verify ENFORCE gate proves only that the declared verify commands exited 0. It does not judge whether those commands test the right thing; correctness depends on verify commands being well-authored and linked to acceptance criteria.
- TDD RED evidence is agent-reported until host-captured provenance lands, so a claimed failing-test-first step is trusted, not independently proven.

</details>

<details>
<summary><strong>Installer flags</strong></summary>

<br>

```
bash install.sh --help
```

| Flag | Effect |
|------|--------|
| `--target <path>` | Install into a specific repo or workspace root (default: current directory) |
| `--ai <tool>` | Target AI tool: `copilot` (default), `claude`, or `codex` |
| `--codex-home <path>` | Codex home for `--ai codex` (default: `CODEX_HOME` or `~/.codex`) |
| `--dry-run` | Print planned actions without writing files |
| `--yes` | Skip the confirmation prompt |
| `--builder-ref <ref>` | Fetch a specific branch, tag, or commit |

**Multi-repo workspaces:** point `--target` at the workspace root. Prompts and standards go there; each repo keeps its own constitution.

</details>

<details>
<summary><strong>Using other AI tools</strong></summary>

<br>

isanna-builder's prompts are plain Markdown — they work with any AI coding agent; only the destination differs.

| Tool | Prompt location | Install command |
|------|----------------|----------------|
| **VS Code + Copilot Chat** | `.github/prompts/isanna-*.prompt.md` | `bash install.sh` (default) |
| **Claude Code** | `.claude/commands/isanna-*.md` | `bash install.sh --ai claude` |
| **Codex** | `${CODEX_HOME:-~/.codex}/skills/builder/` | `bash install.sh --ai codex` |
| **Cursor / others** | Varies | Manual (below) |

**Manual setup** is prompt adaptation, not a supported install. The prompts themselves are
agent-agnostic, but each one declares a `load_set` of files it expects to resolve under
`.builder/` — the standards, the guardrails, the planning skill — and the phases also call the
validator under `.builder/scripts/`. Copying `prompts/` alone gives you commands whose first act
is to load files that are not there.

The reliable way to get the full layout for an unsupported tool is to run the real installer for
whichever destination is closest, then point your agent at the prompts it wrote:

```sh
bash install.sh --target . --yes          # writes the complete .builder/ runtime
```

Then copy or symlink `.github/prompts/isanna-*.prompt.md` into wherever your tool reads
instruction files. `asset-manifest.txt` is the authoritative inventory of everything that must
exist; anything less is unsupported.

</details>

<details>
<a id="standalone-installer"></a>
<summary><strong>Standalone installer</strong> (proxy-restricted / offline)</summary>

<br>

If `curl | bash` is blocked or you need a single text-only file, use `standalone-installer.sh.txt` from `main` or a tagged Release. The `.txt` extension is intentional — many corporate proxies block raw `.sh` downloads but permit `.txt`.

```sh
curl -fLO https://raw.githubusercontent.com/isanna-ai/builder/main/standalone-installer.sh.txt
sh -n standalone-installer.sh.txt                       # syntax-check
sh standalone-installer.sh.txt --target /path/to/repo   # supports --target, --ai, --codex-home, --dry-run, and --yes
```

The embedded release tag is fixed at build time; the runtime installer verifies the embedded payload's SHA-256 before extracting anything. At startup it also does a best-effort version check against `standalone-installer.version.txt`. If a newer version exists it asks for confirmation — so in a non-interactive context (a pipeline, `curl | sh`) it exits 0 having installed **nothing** unless you pass `--yes`. Always pass `--yes` when scripting it.

| Variable | Effect |
|----------|--------|
| `BUILDER_SKIP_UPDATE_CHECK=1` | Skip the version check entirely |
| `BUILDER_UPDATE_URL=<url>` | Override the version-marker URL |
| `BUILDER_DOWNLOAD_URL=<url>` | Override the download URL printed in the warning |

</details>

<details>
<summary><strong>Source layout</strong></summary>

<br>

```
bin/isanna                            → The `isanna` CLI entry point (repo-only; not installed)
prompts/                              → Slash-command prompts
standards/builder-*.md              → Artifact contract, workflow, standards, TDD rules
skills/planning/SKILL.md              → Planning skill
schemas/*.schema.yaml                 → Canonical artifact + telemetry schemas
scripts/validate-spec.py              → Optional deterministic validator (Python 3.11+)
scripts/render-spec-artifacts.py      → YAML → Markdown renderer
scripts/record-workflow-event.py      → Workflow-event recorder
scripts/analyze-workflow-telemetry.py → Telemetry report aggregator
scripts/_validators/, _telemetry/     → Validator + telemetry submodules
templates/                            → Starter constitution + artifact manifests
asset-manifest.txt                    → Authoritative install asset inventory
install.sh                            → Installer
```

The installer transforms these into the `.builder/` and prompt-directory layout used by your project.

</details>

## Further reading

| Doc | What it covers |
|-----|----------------|
| [`docs/system-behaviors.yaml`](docs/system-behaviors.yaml) | The behavioural SSOT — every behaviour this system claims, each one anchored to the tests that guard it. Start here if you want to know what Builder actually does. |
| [`docs/dispatch-delivery.md`](docs/dispatch-delivery.md) | What the autonomous dispatcher does with finished work: branch, PR, and the manual production gate. |
| [`docs/decision-memory.md`](docs/decision-memory.md) | The optional, off-by-default decision-memory layer and every flag that controls it. |
| [`CONTRIBUTING.md`](CONTRIBUTING.md) | The one rule that matters: `make gate` is the merge criterion. DCO, no CLA. |
| [`SECURITY.md`](SECURITY.md) | What counts as a vulnerability in a tool whose product *is* a trust boundary — and how to report one privately. |
| [`TRADEMARKS.md`](TRADEMARKS.md) | Code open, name protected. What you can do without asking. |
| [`CHANGELOG.md`](CHANGELOG.md) | What changed, and the licence history — v0.1.0–v0.3.0 were MIT. |
| [`docs/site/builder-page.html`](docs/site/builder-page.html) | A standalone one-page explainer of the three layers. Open it in a browser; it needs no server and loads nothing external. |

## Also in this repo

Beyond the slash-command workflow, this repository includes the `isanna` CLI — **The Record**
(above), **Builder Home** (an opt-in portfolio view across several builder-wired repos), and an
**experimental autonomous dispatcher**. It is not installed by `install.sh` and is not yet packaged for external use because it assumes a specific operator environment. **The supported product is the slash-command workflow above.**

## Influences & Attribution

isanna-builder is a mashup. It synthesizes ideas, patterns, and lessons from several projects in the spec-driven AI coding space. If you find value here, explore these too — each is more mature and has a larger community:

| Project | Author | What isanna-builder borrows |
|---------|--------|----------------------|
| [**OpenSpec**](https://github.com/Fission-AI/OpenSpec) | Fission AI / [@0xTab](https://x.com/0xTab) | The fluid, iterative spec workflow (propose → specs → design → tasks → apply → archive), slash-command UX pattern, and the philosophy that specs should be lightweight and brownfield-friendly. MIT license. |
| [**Kiro**](https://kiro.dev) | AWS | EARS notation for requirements ("when / the system shall"), the requirements → design → tasks artifact chain, and steering files that keep the agent aligned during implementation. Kiro is a proprietary AWS product; isanna-builder is not affiliated with or endorsed by AWS. |
| [**Spec Kit**](https://github.com/github/spec-kit) | GitHub | The constitution-first approach, structured spec artifacts (specify → plan → tasks → implement), and separating project policy from reusable workflow. MIT license. |
| [**Superpowers**](https://github.com/obra/superpowers) | Jesse Vincent / [Prime Radiant](https://primeradiant.com/) | The brainstorm → plan → execute lifecycle, composable skills architecture, and RED-GREEN-REFACTOR TDD discipline. MIT license. |

isanna-builder is not affiliated with any of these projects. It is an independent, opinionated remix. All original work in this repo is released under the [Apache-2.0 license](LICENSE) (the patent grant and trademark posture matter in a space filling with funded competitors — see [TRADEMARKS.md](TRADEMARKS.md)).

## License

**Apache-2.0** — Copyright 2026 Thiago Henrique de Carvalho and the isanna Builder contributors. See
[LICENSE](LICENSE) for the full terms, [NOTICE](NOTICE) for attribution, and
[TRADEMARKS.md](TRADEMARKS.md) for the name policy (code-open, name-protected). Contributions are
under the [DCO](CONTRIBUTING.md) — no CLA.
