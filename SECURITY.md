# Security policy

## Reporting a vulnerability

**Do not open a public issue for a security problem.** Use GitHub's private reporting instead:
**Security → Report a vulnerability** on this repository. That opens a private advisory only the
maintainers can see.

Please include what you did, what happened, and what you expected — a minimal reproduction is
worth more than a long description. You will get an acknowledgement within a week.

## What is in scope

isanna-builder is a local developer tool. It runs no server and exposes no network service, and
the installed validator and core modules are Python standard library only — no `pip install` to
use them. It
is not dependency-free, though: the CLI entry point is a Bash script, the installer is `sh` and
fetches over `curl`, and optional delivery shells out to `git` and `gh`. So the interesting
surface is narrower than usual and mostly about **trust boundaries the tool is supposed to
enforce**:

- **A way to make the host gate pass without the commands actually passing.** The project's
  entire claim is that a verdict comes from the host running the declared commands and reading
  their exit codes. Anything that lets an agent, a spec, or an artifact forge, bypass, or
  fail-open that gate is the most serious class of bug here — report it even if it needs an
  unusual setup.
- **Escaping the execution allowlist** (`isanna model`) — getting arbitrary commands run through
  a path that is supposed to resolve only test runners.
- **The installer.** `install.sh` writes into a user's repository and is documented as a
  `curl | bash`. Path traversal, writes outside the declared destinations, or a way to make it
  fetch and execute something other than the pinned assets.
- **The pre-publish scrub gate** (`scripts/pre-publish-scan.py`). A way to get a secret or a
  private path past it is a real finding: this repository's own publication depends on it, and
  so may yours.
- **Artifact parsing.** A crafted `.builder/` artifact that causes code execution rather than a
  validation error.

## What is not in scope

- An agent producing wrong or low-quality code. The gates make progress auditable; they do not
  make a model correct, and the [Trust model](README.md#trust-model) says so explicitly.
- A weak or badly-authored verify command passing. A host-run weak test is still a weak test —
  that is a property of the commands the project declared, not a vulnerability here.
- Anything requiring an attacker who already has write access to the repository, or the ability
  to edit `.builder/setup-decisions.yaml`. Both are equivalent to being a maintainer.
- Findings from an automated scanner with no demonstrated impact.

## Supported versions

Fixes land on `main`. There are no maintained release branches — upgrade by re-running the
installer, as described in [Updating](README.md#updating).
