#!/bin/bash
set -o pipefail

BUILDER_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
GENERATOR="$BUILDER_ROOT/scripts/build-standalone-installer.sh"
TMP_DIR="${TMPDIR:-/tmp}/test-standalone-installer-$$"
OUTPUT_FILE="$TMP_DIR/standalone-unit.sh"
COMMITTED_INSTALLER="$BUILDER_ROOT/standalone-installer.sh.txt"
BUILD_TAG="v0.3.1"
export BUILDER_SKIP_UPDATE_CHECK=1

cleanup() {
  rm -rf "$TMP_DIR"
}
trap cleanup EXIT

mkdir -p "$TMP_DIR"

fail() {
  echo "FAIL: $1"
  exit 1
}

set +e
generator_output=$(bash "$GENERATOR" --tag "$BUILD_TAG" --output "$OUTPUT_FILE" 2>&1)
generator_code=$?
set -e

if [ "$generator_code" != "0" ]; then
  fail "generator should exit 0, got $generator_code. Output:\n$generator_output"
fi

test -f "$OUTPUT_FILE" || \
  fail "generator did not create $OUTPUT_FILE. Output:\n$generator_output"

if LC_ALL=C grep -q '[^[:print:][:space:]]' "$OUTPUT_FILE"; then
  fail "generated installer should be printable shell text. Output:\n$generator_output"
fi

if grep -q '@@VERSION@@\|@@PAYLOAD_SHA256@@\|@@MANIFEST_COUNT@@' "$OUTPUT_FILE"; then
  fail "generated installer still contains unreplaced sentinel tokens. Output:\n$generator_output"
fi

sh -n "$OUTPUT_FILE" || \
  fail "generated installer failed sh -n. Output:\n$generator_output"

echo "$generator_output" | grep -q '^Built standalone-installer.sh$' || \
  fail "generator summary missing build banner. Output:\n$generator_output"
echo "$generator_output" | grep -q "^  release    : $BUILD_TAG\$" || \
  fail "generator summary missing release line. Output:\n$generator_output"
echo "$generator_output" | grep -q '^  payload    : ' || \
  fail "generator summary missing payload line. Output:\n$generator_output"
echo "$generator_output" | grep -q '^  sha256     : ' || \
  fail "generator summary missing sha256 line. Output:\n$generator_output"
echo "$generator_output" | grep -q '^  manifest   : ' || \
  fail "generator summary missing manifest line. Output:\n$generator_output"

# ── Assertion 2: end-to-end standalone install succeeds on empty target ─────
# A REAL target, because that is the only kind this installer accepts. This used to be a bare
# mkdir and it passed only because the installer fabricated a `.code-workspace` marker to get
# past install.sh's guard -- so the test was asserting the bypass, not the product.
e2e_target="$TMP_DIR/e2e-target"
mkdir -p "$e2e_target"
git -C "$e2e_target" init -q

set +e
e2e_output=$(/bin/sh "$COMMITTED_INSTALLER" --target "$e2e_target" --yes 2>&1)
e2e_code=$?
set -e

if [ "$e2e_code" != "0" ]; then
  fail "standalone end-to-end install should exit 0, got $e2e_code. Output:\n$e2e_output"
fi

python3 - "$e2e_target/.builder/install-state.json" <<'PY' || \
  fail "standalone end-to-end install-state.json missing provenance or pinned ref. Output:\n$e2e_output"
import json
import sys

state = json.load(open(sys.argv[1], 'r', encoding='utf-8'))
assert state["provenance"] == "standalone"
assert state["builder_ref"].startswith("v")
PY
echo "PASS (2): standalone installer completes end-to-end on an empty target"

# ── Assertion 3: standalone Codex install creates global skill bundle ───────
codex_target="$TMP_DIR/codex-target"
codex_home="$TMP_DIR/codex-home"
mkdir -p "$codex_target"
git -C "$codex_target" init -q

set +e
codex_output=$(/bin/sh "$COMMITTED_INSTALLER" --target "$codex_target" --ai codex --codex-home "$codex_home" --yes 2>&1)
codex_code=$?
set -e

if [ "$codex_code" != "0" ]; then
  fail "standalone codex install should exit 0, got $codex_code. Output:\n$codex_output"
fi
test -f "$codex_home/skills/builder/SKILL.md" || \
  fail "standalone codex install did not create Builder SKILL.md. Output:\n$codex_output"
test -f "$codex_home/skills/builder/prompts/isanna-5-implement.prompt.md" || \
  fail "standalone codex install did not bundle phase prompts. Output:\n$codex_output"
echo "PASS (3): standalone codex install creates the global skill bundle"

# ── Assertion 4: drift gate regenerates committed artifact byte-identically ──
regen_file="$TMP_DIR/standalone-regen.sh"
bash "$GENERATOR" --tag "$BUILD_TAG" --output "$regen_file" >/dev/null || \
  fail "generator drift check failed to regenerate installer"
cmp "$regen_file" "$COMMITTED_INSTALLER" || \
  fail "regenerated installer differs from committed standalone-installer.sh.txt"
echo "PASS (4): committed installer matches a fresh regeneration"

# ── Assertion 5: idempotency check produces byte-identical output ───────────
idempotent_a="$TMP_DIR/standalone-r1.sh"
idempotent_b="$TMP_DIR/standalone-r2.sh"
bash "$GENERATOR" --tag "$BUILD_TAG" --output "$idempotent_a" >/dev/null || \
  fail "generator idempotency build 1 failed"
bash "$GENERATOR" --tag "$BUILD_TAG" --output "$idempotent_b" >/dev/null || \
  fail "generator idempotency build 2 failed"
cmp "$idempotent_a" "$idempotent_b" || \
  fail "generator output is not byte-identical across repeated runs"
echo "PASS (5): generator output is byte-identical across repeated runs"

# ── Assertion 6: corrupted payload aborts before touching install target ─────
corrupt_file="$TMP_DIR/standalone-corrupt.sh"
awk '
  BEGIN { payload = 0; changed = 0 }
  /^cat > .*__BUILDER_PAYLOAD__.$/ { payload = 1; print; next }
  payload && !changed && $0 !~ /^__BUILDER_PAYLOAD__$/ {
    first = substr($0, 1, 1)
    rest = substr($0, 2)
    if (first == "A") {
      print "B" rest
    } else {
      print "A" rest
    }
    changed = 1
    next
  }
  /^__BUILDER_PAYLOAD__$/ { payload = 0; print; next }
  { print }
' "$COMMITTED_INSTALLER" > "$corrupt_file" || \
  fail "failed to create corrupted standalone installer"

corrupt_target="$TMP_DIR/corrupt-target"
mkdir -p "$corrupt_target"

set +e
corrupt_output=$(/bin/sh "$corrupt_file" --target "$corrupt_target" --yes 2>&1)
corrupt_code=$?
set -e

if [ "$corrupt_code" = "0" ]; then
  fail "corrupted payload should exit non-zero. Output:\n$corrupt_output"
fi
echo "$corrupt_output" | grep -q 'ERROR: payload integrity mismatch' || \
  fail "corrupted payload output should report an integrity mismatch. Output:\n$corrupt_output"
if [ -e "$corrupt_target/.builder/install-state.json" ]; then
  fail "corrupted payload should not create install-state.json"
fi
echo "PASS (6): corrupted payload aborts before install target is touched"

# ── Assertion 7: missing sha utility aborts with clear error ─────────────────
path_bin="$TMP_DIR/path-bin"
mkdir -p "$path_bin"
for tool_name in sh base64 gzip tar mkdir mv rm grep awk cat find head ls cp sed pwd wc; do
  tool_path=$(command -v "$tool_name")
  [ -n "$tool_path" ] || fail "required test helper tool missing: $tool_name"
  ln -s "$tool_path" "$path_bin/$tool_name"
done

missing_target="$TMP_DIR/missing-sha-target"
mkdir -p "$missing_target"

set +e
missing_output=$(PATH="$path_bin" /bin/sh "$COMMITTED_INSTALLER" --target "$missing_target" --yes 2>&1)
missing_code=$?
set -e

if [ "$missing_code" = "0" ]; then
  fail "missing-sha utility run should exit non-zero. Output:\n$missing_output"
fi
echo "$missing_output" | grep -q 'ERROR: required tool not found:' || \
  fail "missing-sha utility output should mention the missing tool. Output:\n$missing_output"
echo "PASS (7): missing sha utility aborts with a clear error"

# ── Assertion 8: --builder-ref is rejected because build is pinned ─────────
reject_target="$TMP_DIR/reject-target"
mkdir -p "$reject_target"

set +e
reject_output=$(/bin/sh "$COMMITTED_INSTALLER" --target "$reject_target" --builder-ref v9.9.9 --yes 2>&1)
reject_code=$?
set -e

if [ "$reject_code" = "0" ]; then
  fail "--builder-ref should exit non-zero. Output:\n$reject_output"
fi
echo "$reject_output" | grep -q -- '--builder-ref is not supported' || \
  fail "--builder-ref rejection output is missing. Output:\n$reject_output"
echo "PASS (8): standalone installer rejects --builder-ref"

# ── Assertion 9: the target guard is ENFORCED, not bypassed ───────────────
# This installer used to fabricate a throwaway `.code-workspace` marker in the target so that
# install.sh's "must be a repo or a workspace" guard would pass downstream. That made the
# documented safety property -- README calls the guard intentional, "isanna-builder installs
# into projects, not home directories" -- true on the curl path and FALSE on the standalone
# path the README recommends to proxy-blocked users. Both paths must refuse the same target.
plain_dir="$TMP_DIR/not-a-repo"
mkdir -p "$plain_dir"

set +e
plain_output=$(/bin/sh "$COMMITTED_INSTALLER" --target "$plain_dir" --yes 2>&1)
plain_code=$?
set -e

if [ "$plain_code" = "0" ]; then
  fail "standalone install into a plain directory should exit non-zero. Output:\n$plain_output"
fi
echo "$plain_output" | grep -q 'must contain .git/ or a .code-workspace' || \
  fail "standalone rejection should name the same guard install.sh names. Output:\n$plain_output"
if [ -d "$plain_dir/.builder" ] || [ -d "$plain_dir/.github" ]; then
  fail "standalone installer wrote into a target it should have refused"
fi
if ls "$plain_dir"/*.code-workspace >/dev/null 2>&1; then
  fail "standalone installer fabricated a workspace marker in the target"
fi
echo "PASS (9): standalone installer enforces the same target guard as install.sh"

echo "ALL PASS: test_standalone_installer.sh"
