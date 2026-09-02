# Changelog

Notable changes to isanna Builder. Reconstructed from the annotated release tags and the commit
history; entries below `0.3.1` are the release notes their own tags carry.

This file follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) loosely and
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

> **Licence history — read this if you obtained an earlier version.**
> `v0.1.0`, `v0.2.0` and `v0.3.0` were released under the **MIT** licence. The project relicensed
> to **Apache-2.0** after `v0.3.0`, before the first release of the current line. The copyright
> holder is unchanged and was the sole author of every commit, so the relicence is his to make —
> but it is not retroactive: if you have a copy of `v0.1.0`–`v0.3.0`, your rights **in that copy**
> remain MIT. Everything from `0.3.1` on is Apache-2.0, with the trademark carve-out described in
> [TRADEMARKS.md](TRADEMARKS.md).

---

## [Unreleased] — `0.3.1`

The version in `pyproject.toml`. Not yet tagged.

This is a very large gap: roughly 495 commits separate it from `v0.3.0`, and the project changed
shape rather than merely growing. `v0.3.0` was a set of slash-command prompts. This line adds a
host-executed verification gate, an autonomous dispatcher, and a read-only record of what the
host actually observed — while keeping the prompt workflow as the supported product.

### Added

- **The host gate.** Verification is performed by the host running the spec's declared commands
  and reading their exit codes, rather than by asking the agent whether it succeeded. `make demo`
  shows the difference in under a second, offline.
- **Gate evidence.** Each verify turn writes a hash-chained bundle recording the command, the exit
  code, and the diff the host observed, so a verdict can be audited after the fact.
- **The Record** (`isanna record build`) — a static, read-only flight recorder and planner
  rendered from that evidence. No server, no tokens, nothing left running. Percent-done moves only
  on host-observed events.
- **An autonomous dispatcher** that drives a spec through plan → implement → verify, with an
  independent reviewer on a different model from the author. Opt-in per repository, off until you
  run `isanna init`, and delivery (branch/PR) off until you enable it.
- **A behavioural SSOT** (`docs/system-behaviors.yaml`) — every behaviour the system claims,
  anchored to the tests that guard it, with `isanna sync` refusing to let the two drift apart.
- **Products, releases and intents** — a planning layer above individual specs, with release
  completeness computed from host-observed status.
- **Forward-only workflow telemetry**, measured at runtime; agents are never asked to estimate
  their own token use.
- **Optional decision memory** — off by default; see [docs/decision-memory.md](docs/decision-memory.md).
- **A pre-publish scrub gate** (`make scrub`) that fails the build on a secret, a personal path,
  or private infrastructure, so publication is a mechanical check rather than a one-time read.
- **CI** (`.github/workflows/gate.yml`): the gate on Python 3.11/3.12/3.13, plus lint, shell
  tests, scrub and demo — and a job that builds the public export and runs the whole suite
  *inside it*, because green in the source tree is not green for someone who cloned it.
- `SECURITY.md`, `CODE_OF_CONDUCT.md` and issue templates.

### Changed

- **Licence: MIT → Apache-2.0**, with a `NOTICE`, a `TRADEMARKS.md` describing the code-open /
  name-protected posture, and DCO sign-off (no CLA) for contributions.
- **Renamed: SpecPilot → isanna Builder.** Slash commands went `/sp-*` → `/isanna-*` and the
  runtime directory `.specpilot/` → `.builder/`. `isanna migrate --dir` moves a stopped repo's
  legacy runtime directory.
- Spec artifacts are canonical YAML with rendered Markdown; drift between the two is a hard
  failure rather than a warning.
- `install.sh` installs into `.builder/`, migrating a legacy `.oak/` directory when it finds one.

### Removed

- **`/sp-explore`** (shipped in `v0.3.0`) — pre-spec exploration needs no command; open a chat,
  scope the problem, then run `/isanna-1-specify`.
- **Mission Control**, a FastAPI/PWA control panel that required a live server, replaced by the
  static Record.

### Fixed

Too many to enumerate honestly; the commit history is the record. The ones that changed a
documented promise: the public export now passes its own gate, the standalone installer enforces
the same target guard as `install.sh` (it previously fabricated a marker to bypass it), and
`isanna record build` prints where it wrote instead of exiting in silence.

---

## [0.3.0] — 2026-04-28

MIT-licensed. From the release tag: *canonical artifacts, telemetry, sync, fast-forward, explore,
debug.* Introduced the canonical-YAML artifact contract, workflow telemetry, `sync`, and the
`/sp-ff`, `/sp-explore` and `/sp-debug` commands.

## [0.2.0] — 2026-04-20

MIT-licensed. From the release tag: *prose refactor + optional Python validator.* Added the
deterministic validator that mechanically checks task schema and RED/GREEN evidence, so test-first
became a gate rather than prose in a prompt.

## [0.1.0] — 2026-04-17

MIT-licensed. From the release tag: *14 spec-driven development prompts for AI coding agents.
Supports VS Code Copilot Chat and Claude Code.*

---

### A note on this repository's history

The public repository is published as a single squashed commit rather than the full development
history. That history contains private infrastructure, personal specs, and a live work queue, and
a scrub of it is something nobody can prove complete — one missed blob is permanent. Publishing a
fresh history means there is nothing there to leak. It also means the MIT-era commits described
above are not present here; this changelog is where that history is recorded.
