#!/bin/bash
set -o pipefail

BUILDER_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP_DIR="${TMPDIR:-/tmp}/test-standalone-installer-scrub-$$"

cleanup() {
  rm -rf "$TMP_DIR"
}
trap cleanup EXIT

fail() {
  echo "FAIL: $1"
  exit 1
}

mkdir -p "$TMP_DIR"

# ── Assertion 1: a clean scratch clone builds fine (scrub gate passes) ──────
clean_clone="$TMP_DIR/clean-clone"
mkdir -p "$clean_clone"
cp -a "$BUILDER_ROOT/." "$clean_clone/" || \
  fail "could not create scratch working copy"

set +e
clean_output=$(bash "$clean_clone/scripts/build-standalone-installer.sh" --tag v0.0.0-scrub-test --output "$TMP_DIR/clean-installer.sh" 2>&1)
clean_code=$?
set -e

if [ "$clean_code" != "0" ]; then
  fail "build should succeed on a clean scratch clone, got $clean_code. Output:\n$clean_output"
fi
echo "$clean_output" | grep -q 'Running pre-publish scrub gate' || \
  fail "build did not run the pre-publish scrub gate. Output:\n$clean_output"
echo "$clean_output" | grep -q 'pre-publish scan CLEAN' || \
  fail "scrub gate did not report clean on a clean clone. Output:\n$clean_output"
test -f "$TMP_DIR/clean-installer.sh" || \
  fail "build did not produce an installer on a clean clone"
echo "PASS (1): standalone installer build succeeds and runs the scrub gate on a clean clone"

# ── Assertion 2: a planted secret in a publishable script refuses the build ──
dirty_clone="$TMP_DIR/dirty-clone"
mkdir -p "$dirty_clone"
cp -a "$BUILDER_ROOT/." "$dirty_clone/" || \
  fail "could not create dirty scratch working copy"

echo '# leaked token: ghp_abcdefghijklmnopqrstuvwxyz0123456789'  >> "$dirty_clone/scripts/pre-publish-scan.py"  # publish-ok: fake token, RED-path fixture for the scrub gate test itself
( cd "$dirty_clone" && git add scripts/pre-publish-scan.py )

set +e
dirty_output=$(bash "$dirty_clone/scripts/build-standalone-installer.sh" --tag v0.0.0-scrub-test --output "$TMP_DIR/dirty-installer.sh" 2>&1)
dirty_code=$?
set -e

if [ "$dirty_code" = "0" ]; then
  fail "build should refuse when the scrub gate finds a planted secret. Output:\n$dirty_output"
fi
echo "$dirty_output" | grep -q 'pre-publish scrub gate failed - refusing to build the standalone installer' || \
  fail "build did not report the scrub-gate refusal message. Output:\n$dirty_output"
test -f "$TMP_DIR/dirty-installer.sh" && \
  fail "build must not produce an installer when the scrub gate fails"
echo "PASS (2): a planted secret in a publishable script aborts the build with a clear refusal"

echo "ALL PASS: standalone-installer-scrub.sh"
