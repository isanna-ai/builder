#!/bin/bash
# test_validator.sh — RED/GREEN test for extended validate-spec.py
# Asserts that --strict mode:
#   - rejects spec-bad-status (invalid status enum value)
#   - rejects spec-bad-evidence (RED exit_code == 0)
#   - accepts spec-good
set -o pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
VALIDATOR="python3 $ROOT/scripts/validate-spec.py"
FIXTURES="$ROOT/tests/fixtures"
TMP_DIR=$(mktemp -d "${TMPDIR:-/tmp}/isanna-validator.XXXXXX")
trap 'rm -rf "$TMP_DIR"' EXIT

fail() {
  echo "FAIL: $1"
  exit 1
}

# Assertion 1: spec-bad-status must exit non-zero under --strict
set +e
output=$($VALIDATOR --strict "$FIXTURES/spec-bad-status/" 2>&1)
code=$?
set -e
if [ "$code" = "0" ]; then
  fail "spec-bad-status should exit non-zero under --strict, got exit 0. Output: $output"
fi
echo "PASS (1): spec-bad-status exits non-zero ($code)"

# Assertion 2: spec-bad-evidence must exit non-zero under --strict
set +e
output=$($VALIDATOR --strict "$FIXTURES/spec-bad-evidence/" 2>&1)
code=$?
set -e
if [ "$code" = "0" ]; then
  fail "spec-bad-evidence should exit non-zero under --strict, got exit 0. Output: $output"
fi
echo "PASS (2): spec-bad-evidence exits non-zero ($code)"

# Assertion 3: a legacy Phase 6 evidence-only shape must exit non-zero under --strict
TMP_BAD_PHASE6="$TMP_DIR/spec-bad-phase6-old-shape"
rm -rf "$TMP_BAD_PHASE6"
mkdir -p "$TMP_BAD_PHASE6"
cat > "$TMP_BAD_PHASE6/spec.yaml" <<'EOF'
name: spec-bad-phase6-old-shape
created: 2026-01-01T00:00:00Z
status: verified
current_phase: 6-verify
next_action: "none"
EOF
cat > "$TMP_BAD_PHASE6/phase-log.yaml" <<'EOF'
phases:
  - phase: 6-verify
    completed: 2026-01-01T03:00:00Z
    used_model: "test-model-2"
    outcome: pass
    evidence:
      - command: "bash /path/to/project/tests/test_validator.sh"
        exit_code: 0
        output_summary: "legacy evidence-only shape"
EOF
set +e
output=$($VALIDATOR --strict "$TMP_BAD_PHASE6/" 2>&1)
code=$?
set -e
if [ "$code" = "0" ]; then
  fail "legacy 6-verify evidence-only shape should exit non-zero under --strict, got exit 0. Output: $output"
fi
echo "PASS (3): legacy 6-verify evidence-only shape exits non-zero ($code)"

# Assertion 4: missing system-model.yaml must exit non-zero under --strict
TMP_MISSING_SYSTEM_MODEL="$TMP_DIR/spec-missing-system-model"
rm -rf "$TMP_MISSING_SYSTEM_MODEL"
mkdir -p "$TMP_MISSING_SYSTEM_MODEL"
cat > "$TMP_MISSING_SYSTEM_MODEL/spec.yaml" <<'EOF'
name: spec-missing-system-model
created: 2026-01-01T00:00:00Z
status: specified
current_phase: 1-specify
next_action: "Run /isanna-2-design spec-missing-system-model"
EOF
set +e
output=$($VALIDATOR --strict "$TMP_MISSING_SYSTEM_MODEL/" 2>&1)
code=$?
set -e
if [ "$code" = "0" ]; then
  fail "missing system-model.yaml should exit non-zero under --strict, got exit 0. Output: $output"
fi
echo "PASS (4): missing system-model.yaml exits non-zero ($code)"

# Assertion 5: invalid system-model.yaml must exit non-zero under --strict
TMP_BAD_SYSTEM_MODEL="$TMP_DIR/spec-bad-system-model"
rm -rf "$TMP_BAD_SYSTEM_MODEL"
mkdir -p "$TMP_BAD_SYSTEM_MODEL"
cat > "$TMP_BAD_SYSTEM_MODEL/spec.yaml" <<'EOF'
name: spec-bad-system-model
created: 2026-01-01T00:00:00Z
status: specified
current_phase: 1-specify
next_action: "Run /isanna-2-design spec-bad-system-model"
EOF
cat > "$TMP_BAD_SYSTEM_MODEL/system-model.yaml" <<'EOF'
version: 1
what:
  entities:
    - id: ENT1
      name: Ticket
  capabilities:
    - id: CAP1
      name: create_ticket
who:
  actors:
    - id: ACT1
      name: Customer
      capabilities: [CAP2]
when:
  events: []
where:
  boundaries: []
why:
  rules: []
how:
  behaviors: []
upstream:
  sources: []
downstream:
  sinks: []
EOF
set +e
output=$($VALIDATOR --strict "$TMP_BAD_SYSTEM_MODEL/" 2>&1)
code=$?
set -e
if [ "$code" = "0" ]; then
  fail "spec-bad-system-model should exit non-zero under --strict, got exit 0. Output: $output"
fi
echo "PASS (5): spec-bad-system-model exits non-zero ($code)"

# Assertion 6: spec-good must exit 0 under --strict
set +e
output=$($VALIDATOR --strict "$FIXTURES/spec-good-root/.builder/specs/spec-good/" 2>&1)
code=$?
set -e
if [ "$code" != "0" ]; then
  fail "spec-good should exit 0 under --strict, got exit $code. Output: $output"
fi
echo "PASS (6): spec-good exits 0"

# Assertion 7: the shipped /isanna-6-verify example documents the evidence-completeness contract
# (task_id itself left the verify prompt intentionally at 1d673c8; the seven-categories evidence
# contract is enforced by the evidence VALIDATOR, not by prompt literals like task_id/evidence/task-)
grep -q 'Evidence completeness' "$ROOT/prompts/isanna-6-verify.prompt.md" \
  || fail "/isanna-6-verify prompt should document the Evidence completeness contract"
echo "PASS (7): /isanna-6-verify prompt documents the Evidence completeness contract"

# Assertion 8: /isanna-5-implement documents that RED evidence must be genuinely captured
grep -q 'RED evidence MUST' "$ROOT/prompts/isanna-5-implement.prompt.md" \
  || fail "/isanna-5-implement prompt should document that RED evidence MUST be genuinely captured"
echo "PASS (8): /isanna-5-implement prompt documents genuine RED evidence capture"

echo "ALL PASS: test_validator.sh"
