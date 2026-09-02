# The public target surface. `make help` lists them.

# bash, not sh: the recipes below use `set -o pipefail` semantics and process substitution.
# On a distro without bash (Alpine), install it or run the underlying commands directly --
# the Python entry points themselves need only a POSIX shell.
SHELL := /bin/bash
LINTROOT ?= .

.PHONY: test lint help demo gate scrub shell-tests

help:  ## list these targets
	@grep -E '^[a-zA-Z_-]+:.*?##' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  %-12s %s\n",$$1,$$2}'

demo:  ## watch a lying agent get caught (no API key, no network)
	@PYTHONPATH=scripts python3 scripts/isanna.py demo

scrub:  ## pre-publish scrub gate -- must be GREEN before anything publishes
	@python3 scripts/pre-publish-scan.py --root .

gate:  ## the suite the host gate runs on THIS repo -- what CI must be green on
	@# RUN THIS ON LINUX. The suite is green in the canonical container and CANNOT be
	@# green on a macOS host: `_proc_identity()` (lane_common.py) reads /proc, which its
	@# own docstring documents as "Linux /proc only (the container runtime)", so ~40
	@# governor/daemon cases fail there for the platform and not for the code. Measured natively
	@# on one macOS host: 44 failed there (39 with a non-symlinked TMPDIR -- the other 5 are
	@# path assertions tripping over /var being a symlink to /private/var), 0 in a Linux container. A red run here
	@# is not a backlog -- re-run it in a Linux container before triaging anything. Use the
	@# recipe in CONTRIBUTING.md ("Run make gate on Linux"), NOT a bare `docker run`: without
	@# --init three process-reaping tests fail, and as root one more fails because root can read
	@# a chmod 000 file. Both are about the environment, not the code.
	@# Whole test roots -- never a hand-picked file list, which silently drops new
	@# tests (the "green by omission" antipattern this project exists to refuse). That is not
	@# hypothetical: scripts/_telemetry/ held 42 passing tests that NO root reached, so nothing
	@# kept them green. A new test directory must be added here, or it runs nowhere.
	@set -e; \
	outcomes=$$(mktemp); \
	trap 'rm -f "$$outcomes"' EXIT; \
	PYTHONPATH=scripts PYTEST_SHIM_OUTCOMES=$$outcomes python3 -m pytest scripts/_dispatch_runtime/tests scripts/_telemetry tests/validator tests/unit -q; \
	PYTHONPATH=scripts python3 scripts/_validators/check_guard_outcomes.py . "$$outcomes"

shell-tests:  ## every tests/*.sh must be green (no green-by-omission)
	@fail=0; for t in tests/*.sh; do \
	  echo "== $$t"; \
	  if ! bash "$$t"; then echo "FAIL $$t"; fail=1; fi; \
	done; exit $$fail

test: gate lint shell-tests scrub  ## everything: gate + lint + shell tests + scrub

lint:  ## asset hygiene + model-registry drift + spec-status drift (override LINTROOT=/path)
	@python3 $(LINTROOT)/scripts/lint-builder-assets.py \
	  --manifest $(LINTROOT)/asset-manifest.txt \
	  --check-frontmatter --check-references --check-manifest \
	  --check-status-source-of-truth --check-model-registry-drift \
	  $(LINTROOT)
	@# Declared-vs-artifact drift: a spec.yaml claiming a phase whose required artifact is not
	@# on disk, an unknown status, or no readable status at all. Advisory since it shipped; now
	@# load-bearing here because builder itself is at zero findings, so a new one is a real
	@# regression rather than pre-existing debt. This catches the shape that let a 90-byte stub
	@# sit in a backlog reading like real work -- it does NOT catch shipped-vs-declared drift,
	@# which is `isanna verify --spec`'s job.
	@PYTHONPATH=$(LINTROOT)/scripts python3 $(LINTROOT)/scripts/list-specs.py --root $(LINTROOT) --strict
