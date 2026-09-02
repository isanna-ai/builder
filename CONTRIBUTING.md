# Contributing to isanna Builder

Thanks for wanting to help. isanna Builder is a tool whose entire value is that its verdict can be
trusted, so contributions are held to the same standard the tool holds agents to: **the host has to
be able to verify it.**

## The one rule that matters

Every change must pass the host gate:

```bash
make gate      # the exact suite the dispatcher runs on this repo
make demo      # watch a lying agent get caught — must exit 0
```

**Install the test dependencies first.** The shipped runtime needs nothing, but the suite wants
PyYAML — without it 32 cases fail on parser divergence that has nothing to do with your change:

```bash
python -m pip install -e ".[test]"
```

**`make gate` runs a bundled test runner, not real pytest.** There is a `pytest/` package at the
repo root — a small hand-written runner — and because it sits on the path first, `python3 -m
pytest` from the repo root resolves to it even if you have real pytest installed. It exists so the
suite runs anywhere with no install, and it supports only what this suite uses: `test_*` functions,
the `tmp_path` fixture, `-k`, `-q`. It does **not** implement `conftest.py`, most fixtures, or plugins. It refuses outright on
`conftest.py` and `[tool.pytest.ini_options]`; other unsupported flags (`-p`, `-m`, `--maxfail`)
are accepted and ignored rather than erroring. If you need something it lacks, run real pytest
from outside the repo root, or add the capability to `pytest/__main__.py`.

**Run `make gate` on Linux.** Around 40 dispatcher tests read `/proc`, so the suite cannot be
green on a macOS host. The failures are about the platform, not your change. Measured natively on
one macOS host (Python 3.12.13, PyYAML installed), against 0 failures in a Linux container:

- **`44 failed`** with the default macOS `TMPDIR`, which lives under `/var` — a symlink to
  `/private/var`. Five of those are path-equality assertions failing on the symlink alone.
- **`39 failed`** with `TMPDIR` set to a non-symlinked directory. These are the `/proc` cases,
  and no amount of environment fixes them on macOS.
If you are on a Mac, run it in a container:

```bash
docker run --rm --init -v "$PWD":/repo -w /repo python:3.13 \
  bash -c 'pip install -q -e ".[test]" && useradd -m dev && chown -R dev /repo \
           && su dev -c "make gate"'
```

Both flags matter, and each one costs you failures that are about the environment rather than
your change:

- **`su dev`** — a test asserts that an unreadable file is reported as unreadable, and root
  defeats it by being able to read a `chmod 000` file. As root: `1 failed`.
- **`--init`** — three tests exercise process reaping and a grace window, and need a real reaper
  on PID 1. A bare `docker run` has none, so orphaned children are never reaped. Without `--init`: `3 failed, 1639 passed`.

With both, and the `[test]` extra installed: **`1642 passed, 15 skipped`**.

> On a **Linux** host, `chown -R dev /repo` rewrites ownership of your real working tree through
> the bind mount. Either run the suite natively (you do not need the container on Linux) or copy
> the repo into the container instead of mounting it.

CI runs it on 3.11, 3.12 and 3.13, and separately builds the public export and runs the whole
suite inside that too — see `.github/workflows/gate.yml`.

A green `make gate` is not a courtesy; it is the merge criterion. If you add behaviour, add a test
that fails before your change and passes after. A PR that changes production code to make a test pass
(rather than the other way round) will be sent back — that is the exact failure mode this project
exists to kill.

## Developer Certificate of Origin (DCO), not a CLA

We use the [Developer Certificate of Origin](https://developercertificate.org/). There is no CLA to
sign and no copyright assignment. You keep the copyright to your contribution; you simply certify
that you have the right to submit it under the project's licence.

Certify by adding a `Signed-off-by` line to each commit:

```
Signed-off-by: <your real name> <your real email>
```

Do not type this by hand — **`git commit -s` writes it for you** from your git config, which
is also what makes it match the commit author. The name and email must be real.

## Before you start

By taking part you agree to the [Code of Conduct](CODE_OF_CONDUCT.md). Found a security problem?
Do not open an issue — [SECURITY.md](SECURITY.md) explains private reporting.

## How to propose a change

1. Open an issue first for anything non-trivial, so we can agree on the shape before you build.
2. Branch from `main`. Keep the change focused.
3. `make gate` must be green, and any new behaviour must be host-verifiable (a real test, not a
   claim).
4. Write commit messages that explain **why**, not just what. Sign off (`-s`).
5. Open a PR. Expect an adversarial review — the reviewer's job is to find the way your change is
   wrong, and that is a feature, not hostility.

## What we especially want

- Test runners for the `isanna model` allowlist that genuinely discover-and-run test files (no
  project-authored build scripts).
- Adapters and integrations that keep the zero-dependency core intact — a gate verdict must never
  depend on an optional adapter.
- Sharper adversarial reviews of the trust-critical paths (the gates, the resolver, the numerator).

## Licence

By contributing, you agree that your contributions are licensed under the project's Apache-2.0
licence (see LICENSE), and you certify the DCO above. The name and marks stay protected — see
TRADEMARKS.md.
