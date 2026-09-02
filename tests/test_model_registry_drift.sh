#!/bin/bash
# test_model_registry_drift.sh — RED/GREEN for the §4 table <-> model_registry.py gate.
set -o pipefail

REPO=$(cd "$(dirname "$0")/.." && pwd)
LINTER="python3 $REPO/scripts/lint-builder-assets.py"

# GREEN: the canonical repo must be in sync (doc §4 table == registry).
set +e
green_out=$($LINTER --check-model-registry-drift "$REPO" 2>&1)
green_rc=$?
set -e
if [ "$green_rc" != "0" ]; then
  echo "FAIL(GREEN): expected exit 0 on the in-sync repo, got $green_rc"
  echo "Output: $green_out"
  exit 1
fi

# RED: mutate the §4 table (registry unchanged) and expect a drift failure.
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
mkdir -p "$tmp/standards" "$tmp/scripts"
cp "$REPO/standards/builder-workflow.md" "$tmp/standards/"
ln -s "$REPO/scripts/_dispatch_runtime" "$tmp/scripts/_dispatch_runtime"
# Break the deep_reasoner Claude model in the doc only.
sed -i '/`deep_reasoner`/ s/`opus-4.8`/`sonnet-4.6`/' "$tmp/standards/builder-workflow.md"

set +e
red_out=$($LINTER --check-model-registry-drift "$tmp" 2>&1)
red_rc=$?
set -e
if [ "$red_rc" != "1" ]; then
  echo "FAIL(RED): expected exit 1 on the mutated table, got $red_rc"
  echo "Output: $red_out"
  exit 1
fi
if ! echo "$red_out" | grep -q 'deep_reasoner'; then
  echo "FAIL(RED): expected the drift report to name deep_reasoner"
  echo "Output: $red_out"
  exit 1
fi

echo "PASS: drift gate GREEN on the in-sync repo and RED on a mutated §4 table"
