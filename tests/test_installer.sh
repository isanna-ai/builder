#!/bin/bash
# test_installer.sh — RED/GREEN test for updated install.sh
# Assertions:
#   1. install.sh with AGENTS.md containing a stale count (99 prompts) exits non-zero
#      and output mentions AGENTS.md
#   2. A clean install exits 0 and creates <target>/.builder/install-state.json
set -o pipefail

BUILDER_SRC=$(cd "$(dirname "$0")/.." && pwd)
INSTALL_SH="$BUILDER_SRC/install.sh"
TMP_DIR=$(mktemp -d "${TMPDIR:-/tmp}/test-installer.XXXXXX")

cleanup() {
  rm -rf "$TMP_DIR"
}
trap cleanup EXIT

mkdir -p "$TMP_DIR"

fail() {
  echo "FAIL: $1"
  exit 1
}

# ── Assertion 1: stale-count triggers non-zero + AGENTS.md mention ────────────
stale_src="$TMP_DIR/builder-stale"
cp -r "$BUILDER_SRC" "$stale_src"
printf '\nThis release ships 99 prompts.\n' >> "$stale_src/AGENTS.md"

stale_target="$TMP_DIR/stale-install-root"
mkdir -p "$stale_target"
# create a minimal .git to satisfy the .git check
mkdir -p "$stale_target/.git"

set +e
stale_output=$(/bin/sh "$stale_src/install.sh" --target "$stale_target" --yes 2>&1)
stale_code=$?
set -e

if [ "$stale_code" = "0" ]; then
  fail "stale-count install should exit non-zero, got 0. Output:\n$stale_output"
fi
echo "$stale_output" | grep -q 'AGENTS.md' || \
  fail "stale-count output should mention AGENTS.md. Got:\n$stale_output"
echo "PASS (1): stale-count install exits $stale_code and mentions AGENTS.md"

# ── Assertion 2: clean install exits 0 and creates install-state.json ─────────
clean_target="$TMP_DIR/clean-install-root"
mkdir -p "$clean_target/.git"

set +e
clean_output=$(/bin/sh "$INSTALL_SH" --target "$clean_target" --yes 2>&1)
clean_code=$?
set -e

if [ "$clean_code" != "0" ]; then
  fail "clean install should exit 0, got $clean_code. Output:\n$clean_output"
fi
test -f "$clean_target/.builder/install-state.json" || \
  fail "clean install did not create .builder/install-state.json. Output:\n$clean_output"

set +e
validator_checks=$(python3 "$clean_target/.builder/scripts/validate-spec.py" --list-checks 2>&1)
validator_code=$?
set -e

if [ "$validator_code" != "0" ]; then
  fail "installed validator should import cleanly, got $validator_code. Output:\n$validator_checks"
fi
echo "$validator_checks" | grep -q 'setup-decisions' || \
  fail "installed validator should list canonical checks including setup-decisions. Output:\n$validator_checks"
echo "$validator_checks" | grep -q 'utility-report' || \
  fail "installed validator should list canonical checks including utility-report. Output:\n$validator_checks"
test -f "$clean_target/.builder/schemas/tasks.schema.yaml" || \
  fail "clean install did not copy schemas/tasks.schema.yaml. Output:\n$clean_output"
test -f "$clean_target/.builder/scripts/render-spec-artifacts.py" || \
  fail "clean install did not copy scripts/render-spec-artifacts.py. Output:\n$clean_output"
echo "PASS (2): clean install creates install-state.json and ships validator dependencies"

# ── Assertion 3: standalone env stamps provenance + pinned ref ───────────────
standalone_target="$TMP_DIR/standalone-install-root"
mkdir -p "$standalone_target/.git"

set +e
standalone_output=$(BUILDER_INSTALL_PROVENANCE=standalone BUILDER_REF=v0.3.0 \
  /bin/sh "$INSTALL_SH" --target "$standalone_target" --yes 2>&1)
standalone_code=$?
set -e

if [ "$standalone_code" != "0" ]; then
  fail "standalone-mode install should exit 0, got $standalone_code. Output:\n$standalone_output"
fi

python3 - "$standalone_target/.builder/install-state.json" <<'PY' || \
  fail "standalone-mode install-state.json missing provenance or pinned ref. Output:\n$standalone_output"
import json
import sys

state = json.load(open(sys.argv[1], 'r', encoding='utf-8'))
assert state["provenance"] == "standalone"
assert state["builder_ref"] == "v0.3.0"
PY
echo "PASS (3): standalone-mode install writes provenance and pinned ref"

# ── Assertion 4: Codex install creates global skill bundle, not prompt dir ───
codex_target="$TMP_DIR/codex-install-root"
codex_home="$TMP_DIR/codex-home"
mkdir -p "$codex_target/.git"

set +e
codex_output=$(/bin/sh "$INSTALL_SH" --target "$codex_target" --ai codex --codex-home "$codex_home" --yes 2>&1)
codex_code=$?
set -e

if [ "$codex_code" != "0" ]; then
  fail "codex install should exit 0, got $codex_code. Output:\n$codex_output"
fi
test -f "$codex_home/skills/builder/SKILL.md" || \
  fail "codex install did not create Builder SKILL.md. Output:\n$codex_output"
test -f "$codex_home/skills/builder/prompts/isanna-5-implement.prompt.md" || \
  fail "codex install did not bundle phase prompts. Output:\n$codex_output"
test -f "$codex_home/skills/builder/standards/builder-workflow.md" || \
  fail "codex install did not bundle workflow standards. Output:\n$codex_output"
test -f "$codex_home/skills/builder/references/planning-skill.md" || \
  fail "codex install did not bundle planning reference. Output:\n$codex_output"
if [ -d "$codex_target/.github/prompts" ]; then
  fail "codex install should not create .github/prompts. Output:\n$codex_output"
fi
grep -q '"codex_skill_dir"' "$codex_target/.builder/install-state.json" || \
  fail "codex install-state.json should record codex_skill_dir. Output:\n$codex_output"
echo "PASS (4): codex install creates the global skill bundle"

echo "ALL PASS: test_installer.sh"
