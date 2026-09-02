#!/bin/bash
# test_lint.sh — RED/GREEN test for lint-builder-assets.py
set -o pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
LINTER="python3 $ROOT/scripts/lint-builder-assets.py"
FIXTURES="$ROOT/tests/fixtures"

# Run linter with --check-frontmatter against fixtures root.
# The linter scans *.prompt.md files directly in FIXTURES (no prompts/ subdir).
set +e
output=$($LINTER --check-frontmatter "$FIXTURES" 2>&1)
exit_code=$?
set -e

if [ "$exit_code" != "1" ]; then
  echo "FAIL: expected exit code 1, got $exit_code"
  echo "Output: $output"
  exit 1
fi

if ! echo "$output" | grep -q 'lint-broken-frontmatter.prompt.md'; then
  echo "FAIL: expected output to mention lint-broken-frontmatter.prompt.md"
  echo "Output: $output"
  exit 1
fi

echo "PASS: linter correctly reports broken frontmatter with exit code 1"

# --check-manifest: an unmanifested on-disk isanna-*.prompt.md must be flagged
# (regression for the post-rebrand sp-* -> isanna-* glob fix).
TMPROOT=$(mktemp -d)
trap 'rm -rf "$TMPROOT"' EXIT
mkdir -p "$TMPROOT/prompts"
cp "$ROOT/prompts/isanna-1-specify.prompt.md" "$TMPROOT/prompts/isanna-1-specify.prompt.md"
cp "$ROOT/prompts/isanna-1-specify.prompt.md" "$TMPROOT/prompts/isanna-unplanned.prompt.md"
MANIFEST="$TMPROOT/asset-manifest.txt"
echo "prompt isanna-1-specify.prompt.md" > "$MANIFEST"

set +e
output=$($LINTER --check-manifest --manifest "$MANIFEST" "$TMPROOT" 2>&1)
exit_code=$?
set -e

if [ "$exit_code" != "1" ]; then
  echo "FAIL: expected exit code 1 for unmanifested isanna-*.prompt.md, got $exit_code"
  echo "Output: $output"
  exit 1
fi

if ! echo "$output" | grep -q 'isanna-unplanned.prompt.md'; then
  echo "FAIL: expected output to mention isanna-unplanned.prompt.md"
  echo "Output: $output"
  exit 1
fi

echo "PASS: linter correctly reports unmanifested isanna-*.prompt.md with exit code 1"

# --check-status-source-of-truth: a status literal not in the contract enum
# must be flagged (regression for the previously-always-empty enum, which
# made this check a silent no-op).
STATUSROOT=$(mktemp -d)
mkdir -p "$STATUSROOT/standards" "$STATUSROOT/scripts"
cp "$ROOT/standards/builder-contract.md" "$STATUSROOT/standards/builder-contract.md"
cat > "$STATUSROOT/scripts/example.py" <<'PYEOF'
def f():
    status: bogus_value
PYEOF

set +e
output=$($LINTER --check-status-source-of-truth "$STATUSROOT" 2>&1)
exit_code=$?
set -e

if [ "$exit_code" != "1" ]; then
  echo "FAIL: expected exit code 1 for unknown status literal, got $exit_code"
  echo "Output: $output"
  rm -rf "$STATUSROOT"
  exit 1
fi

if ! echo "$output" | grep -q "unknown status value 'bogus_value'"; then
  echo "FAIL: expected output to mention unknown status value 'bogus_value'"
  echo "Output: $output"
  rm -rf "$STATUSROOT"
  exit 1
fi

echo "PASS: linter correctly reports a status literal not in the contract enum"
rm -rf "$STATUSROOT"

# --check-status-source-of-truth: a contract with no status-enum block must be
# a loud error, not a silent skip (this is the exact bug that made the check
# always a no-op: builder-contract.md never had the block).
NOBLOCKROOT=$(mktemp -d)
mkdir -p "$NOBLOCKROOT/standards" "$NOBLOCKROOT/scripts"
cat > "$NOBLOCKROOT/standards/builder-contract.md" <<'MDEOF'
# Builder Contract
No machine-readable appendix here.
MDEOF

set +e
output=$($LINTER --check-status-source-of-truth "$NOBLOCKROOT" 2>&1)
exit_code=$?
set -e

if [ "$exit_code" != "1" ]; then
  echo "FAIL: expected exit code 1 when the contract has no status-enum block, got $exit_code"
  echo "Output: $output"
  rm -rf "$NOBLOCKROOT"
  exit 1
fi

if ! echo "$output" | grep -q 'status-source-of-truth'; then
  echo "FAIL: expected output to mention status-source-of-truth"
  echo "Output: $output"
  rm -rf "$NOBLOCKROOT"
  exit 1
fi

echo "PASS: linter loudly errors when the contract has no status-enum block"
rm -rf "$NOBLOCKROOT"

rm -rf "$TMPROOT"
trap - EXIT
