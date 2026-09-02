#!/bin/bash
# test_model_stale.sh — read-only advisory: `model.py stale` compares the published
# system model against a fresh in-memory regen, without ever writing the published file.
# Assertions:
#   1. A freshly-built model reports fresh + exit 0
#   2. Adding a spec (published model unchanged) reports it under `added`, exit 0
#   3. `--check` exits 3 when stale (opt-in only; plain `stale` never fails the gate)
set -o pipefail

BUILDER_SRC=$(cd "$(dirname "$0")/.." && pwd)
MODEL="python3 $BUILDER_SRC/scripts/model.py"
ROOT=$(mktemp -d "${TMPDIR:-/tmp}/model-stale-test.XXXXXX")
trap 'rm -rf "$ROOT"' EXIT

fail() {
  echo "FAIL: $1"
  exit 1
}

mkdir -p "$ROOT/.builder/specs/spec-one"
cat > "$ROOT/.builder/specs/spec-one/spec.yaml" <<'EOF'
name: spec-one
status: verified
EOF

set +e
build_out=$($MODEL build --root "$ROOT" 2>&1)
build_code=$?
set -e
[ "$build_code" = "0" ] || fail "build should exit 0, got $build_code. Output: $build_out"
[ -f "$ROOT/.builder/model/system-model.yaml" ] || fail "expected published model at .builder/model/system-model.yaml"

# ── Assertion 1: fresh regen matches the just-published model ────────────────
before_hash=$(shasum "$ROOT/.builder/model/system-model.yaml")
set +e
stale_out=$($MODEL stale --root "$ROOT" 2>&1)
stale_code=$?
set -e
after_hash=$(shasum "$ROOT/.builder/model/system-model.yaml")
[ "$stale_code" = "0" ] || fail "stale on a freshly-built model should exit 0, got $stale_code. Output: $stale_out"
echo "$stale_out" | grep -qi 'fresh' || fail "expected 'fresh' in output. Got: $stale_out"
[ "$before_hash" = "$after_hash" ] || fail "stale must never rewrite the published model"
echo "PASS (1): freshly-built model reports fresh, exit 0, published model untouched"

# ── Assertion 2: a new spec (published model stale) reports it under 'added' ─
mkdir -p "$ROOT/.builder/specs/spec-two"
cat > "$ROOT/.builder/specs/spec-two/spec.yaml" <<'EOF'
name: spec-two
status: verified
EOF

set +e
stale_out=$($MODEL stale --root "$ROOT" 2>&1)
stale_code=$?
set -e
[ "$stale_code" = "0" ] || fail "plain 'stale' is advisory-only and must exit 0 even when stale, got $stale_code. Output: $stale_out"
echo "$stale_out" | grep -q 'cap:spec-two' || fail "expected cap:spec-two named in the report. Got: $stale_out"
echo "$stale_out" | grep -q 'ADDED.*cap:spec-two' || fail "expected cap:spec-two under ADDED. Got: $stale_out"
echo "PASS (2): a new uncollected spec reports under 'added', exit 0"

# ── Assertion 3: --check is opt-in and exits 3 when stale ────────────────────
set +e
check_out=$($MODEL stale --root "$ROOT" --check 2>&1)
check_code=$?
set -e
[ "$check_code" = "3" ] || fail "stale --check should exit 3 when stale, got $check_code. Output: $check_out"
echo "PASS (3): --check exits 3 when stale"

# ── Assertion 4: --json is valid JSON and reflects the same facts ────────────
set +e
json_out=$($MODEL stale --root "$ROOT" --json 2>&1)
json_code=$?
set -e
[ "$json_code" = "0" ] || fail "stale --json should exit 0, got $json_code. Output: $json_out"
echo "$json_out" | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d["fresh"] is False; assert "cap:spec-two" in d["added"]' \
  || fail "expected --json output to report fresh:false and cap:spec-two under added. Got: $json_out"
echo "PASS (4): --json output is well-formed and reports the same facts"

echo "ALL PASS: test_model_stale.sh"
